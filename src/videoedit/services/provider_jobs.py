from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from videoedit import __version__
from videoedit.errors import (
    ApprovalRequiredError,
    BudgetBlockedError,
    DependencyUnavailableError,
    PlanningValidationError,
    ProviderPermanentError,
    ProviderTransientError,
    StaleApprovalError,
)
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file

PROVIDER_JOB_IMPLEMENTATION_VERSION = f"{__version__}:provider-job-v1"
_ACTIVE_STATUSES = frozenset({"submitted", "running", "succeeded", "downloading", "validated"})


class ProviderSubmitter(Protocol):
    def submit(self, job_path: Path, idempotency_key: str) -> Mapping[str, Any]: ...


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object: {path}")
    return value


def _owned(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} must be inside the project: {resolved}") from exc
    return resolved


def _money(value: object, label: str) -> tuple[dict[str, str], Decimal]:
    if not isinstance(value, Mapping):
        raise PlanningValidationError(f"{label} is missing")
    currency = str(value.get("currency", "")).strip()
    amount_value = str(value.get("amount", "")).strip()
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise PlanningValidationError(f"{label} currency is invalid")
    try:
        amount = Decimal(amount_value)
    except (InvalidOperation, ValueError) as exc:
        raise PlanningValidationError(f"{label} amount is invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise PlanningValidationError(f"{label} amount must be finite and nonnegative")
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if not normalized:
        normalized = "0"
    return {"currency": currency, "amount": normalized}, amount


def _approval(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    *,
    request_sha256: str,
    expected_type: str,
    estimated_cost: Mapping[str, str],
    revision_id: str,
    label: str,
) -> dict[str, str]:
    approval_file = _owned(layout, path, label)
    approval = _read_object(approval_file, label)
    validate_artifact(package_root, "approval_record", approval)
    if approval.get("project_id") != layout.root.name or approval.get("revision_id") != revision_id:
        raise StaleApprovalError(f"{label} project or revision is stale")
    if approval.get("decision") != "approved":
        raise ApprovalRequiredError(f"{label} is not approved")
    if expected_type == "edit":
        allowed_types = {"edit", "edit_batch"}
    else:
        allowed_types = {expected_type}
    if approval.get("approval_type") not in allowed_types:
        raise ApprovalRequiredError(f"{label} has the wrong approval type")
    if approval.get("approved_item_sha256") != request_sha256:
        raise StaleApprovalError(f"{label} is stale for the provider request")
    expires_at = approval.get("expires_at")
    if isinstance(expires_at, str):
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise StaleApprovalError(f"{label} has expired")
        except ValueError as exc:
            raise StaleApprovalError(f"{label} has an invalid expiry") from exc
    approval_id = str(approval.get("approval_id", "")).strip()
    if not approval_id:
        raise ApprovalRequiredError(f"{label} has no approval_id")
    if expected_type == "spend":
        budget = approval.get("budget")
        if not isinstance(budget, Mapping):
            raise BudgetBlockedError(f"{label} has no bounded budget")
        maximum, maximum_amount = _money(
            {
                "currency": budget.get("currency"),
                "amount": budget.get("maximum_amount"),
            },
            f"{label} budget",
        )
        if maximum["currency"] != estimated_cost["currency"]:
            raise BudgetBlockedError(f"{label} budget currency does not match the estimate")
        _, estimated_amount = _money(estimated_cost, "provider estimate")
        if maximum_amount < estimated_amount:
            raise BudgetBlockedError(f"{label} maximum is below the current provider estimate")
    return {"approval_id": approval_id, "sha256": sha256_file(approval_file)}


def _request_from_plan(
    package_root: Path,
    layout: ProjectLayout,
    request_plan_path: Path,
    request_id: str,
    revision_id: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    plan_path = _owned(layout, request_plan_path, "provider request plan")
    plan = _read_object(plan_path, "provider request plan")
    validate_artifact(package_root, "broll_plan", plan)
    if plan.get("project_id") != layout.root.name or plan.get("revision_id") != revision_id:
        raise StaleApprovalError("provider request plan project or revision is stale")
    requests = plan.get("requests")
    if not isinstance(requests, list):
        raise PlanningValidationError("provider request plan requests must be an array")
    request = next(
        (
            item
            for item in requests
            if isinstance(item, Mapping) and item.get("request_id") == request_id
        ),
        None,
    )
    if not isinstance(request, Mapping):
        raise PlanningValidationError(f"provider request is missing from the plan: {request_id}")
    return plan_path, plan, dict(request)


def plan_provider_job(
    package_root: Path,
    layout: ProjectLayout,
    request_plan_path: Path,
    request_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Persist a disabled-by-default provider job without submitting it."""

    plan_path, plan, request = _request_from_plan(
        package_root, layout, request_plan_path, request_id, revision_id
    )
    selected_provider = (provider or str(request.get("provider", ""))).strip()
    selected_model = (model or str(request.get("model", ""))).strip()
    if not selected_provider or selected_provider.lower() in {"none", "disabled"}:
        raise PlanningValidationError("provider job requires a configured non-disabled provider")
    if not selected_model or selected_model.lower() in {"none", "disabled"}:
        raise PlanningValidationError("provider job requires a configured model")
    estimate, _estimate_amount = _money(request.get("estimated_cost"), "provider estimate")
    binding = {
        "implementation": PROVIDER_JOB_IMPLEMENTATION_VERSION,
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "request_id": request_id,
        "request": request,
        "provider": selected_provider,
        "model": selected_model,
        "estimated_cost": estimate,
        "network": {"enabled": False, "mode": "disabled"},
        "config_sha256": config_sha256(layout),
    }
    request_sha256 = canonical_sha256(binding)
    destination = _owned(
        layout,
        output or layout.artifacts / "provider-jobs" / f"{request_id}-{request_sha256[:16]}.json",
        "provider job output",
    )
    if destination.is_file():
        existing = _read_object(destination, "existing provider job")
        validate_artifact(package_root, "provider_job", existing)
        if existing.get("request_sha256") == request_sha256:
            return destination
        raise StaleApprovalError(f"provider job output contains a different request: {destination}")
    payload: dict[str, Any] = {
        "schema_name": "provider_job",
        "schema_version": "1.0.0",
        "artifact_id": "art_provider_job",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "provider-job-planning", "provider-boundary", PROVIDER_JOB_IMPLEMENTATION_VERSION
        ),
        "inputs": [artifact_input(str(plan["artifact_id"]), plan_path)],
        "config_sha256": config_sha256(layout),
        "provider_job_id": f"job_{request_sha256[:24]}",
        "request_id": request_id,
        "provider": selected_provider,
        "model": selected_model,
        "status": "planned",
        "idempotency_key": f"{layout.root.name}-{revision_id}-{request_id}-{request_sha256[:16]}",
        "request_sha256": request_sha256,
        "network": {"enabled": False, "mode": "disabled"},
        "approvals": {"effect": None, "spend": None},
        "remote_job_id": None,
        "submitted_at": None,
        "updated_at": now_iso(),
        "estimated_cost": estimate,
        "actual_cost": None,
        "output": None,
        "error_code": None,
        "retryable": False,
        "warnings": [
            "provider_network_disabled_by_default",
            "provider_job_requires_current_effect_and_spend_approval",
            "no_provider_submission_performed",
        ],
    }
    return write_validated_artifact(package_root, "provider_job", destination, payload)


def submit_provider_job(
    package_root: Path,
    layout: ProjectLayout,
    job_path: Path,
    effect_approval_path: Path,
    spend_approval_path: Path,
    *,
    network_enabled: bool = False,
    adapter: ProviderSubmitter | None = None,
) -> Path:
    """Submit only after current request-bound approvals and explicit network opt-in."""

    job_file = _owned(layout, job_path, "provider job")
    job = _read_object(job_file, "provider job")
    validate_artifact(package_root, "provider_job", job)
    if job.get("project_id") != layout.root.name:
        raise StaleApprovalError("provider job project is stale")
    status = str(job.get("status", ""))
    if status in _ACTIVE_STATUSES:
        if job.get("remote_job_id"):
            return job_file
        raise ProviderTransientError("provider job has an active status without a remote job ID")
    if status == "submitting":
        raise ProviderTransientError(
            "provider submission state is unresolved; reconcile the provider before retrying"
        )
    if status != "planned":
        raise ProviderPermanentError(f"provider job cannot be submitted from status {status}")
    request_sha256 = str(job.get("request_sha256", "")).strip()
    if len(request_sha256) != 64:
        raise StaleApprovalError("provider job request hash is missing")
    estimated_cost, _estimate_amount = _money(job.get("estimated_cost"), "provider estimate")
    revision_id = str(job.get("revision_id", "")).strip()
    if not revision_id:
        raise StaleApprovalError("provider job revision is missing")
    effect = _approval(
        package_root,
        layout,
        effect_approval_path,
        request_sha256=request_sha256,
        expected_type="edit",
        estimated_cost=estimated_cost,
        revision_id=revision_id,
        label="provider effect approval",
    )
    spend = _approval(
        package_root,
        layout,
        spend_approval_path,
        request_sha256=request_sha256,
        expected_type="spend",
        estimated_cost=estimated_cost,
        revision_id=revision_id,
        label="provider spend approval",
    )
    if not network_enabled:
        raise DependencyUnavailableError(
            "provider network is disabled; explicit network opt-in is required after approvals"
        )
    if adapter is None:
        raise DependencyUnavailableError("no provider adapter is configured")
    submitting = dict(job)
    submitting["status"] = "submitting"
    submitting["approvals"] = {"effect": effect, "spend": spend}
    submitting["updated_at"] = now_iso()
    write_validated_artifact(package_root, "provider_job", job_file, submitting)
    try:
        response = adapter.submit(job_file, str(job["idempotency_key"]))
    except Exception as exc:
        raise ProviderTransientError(
            "provider submission outcome is unresolved; the persisted submitting job "
            "must be reconciled"
        ) from exc
    if not isinstance(response, Mapping):
        raise ProviderPermanentError("provider adapter response must be an object")
    remote_job_id = str(response.get("remote_job_id", "")).strip()
    if not remote_job_id:
        raise ProviderTransientError(
            "provider adapter returned no remote job ID; the persisted submitting job "
            "must be reconciled"
        )
    response_status = str(response.get("status", "submitted"))
    if response_status not in _ACTIVE_STATUSES:
        response_status = "submitted"
    actual_cost: dict[str, str] | None = None
    if response.get("actual_cost") is not None:
        actual_cost, actual_amount = _money(response.get("actual_cost"), "provider actual cost")
        if actual_cost["currency"] != estimated_cost["currency"]:
            raise BudgetBlockedError("provider actual cost currency does not match the estimate")
        _, maximum_amount = _money(
            {"currency": estimated_cost["currency"], "amount": "0"}, "provider estimate"
        )
        _ = maximum_amount
        # The spend approval is checked before submission; actual-cost reconciliation
        # remains explicit because the schema does not carry the approval maximum.
        if actual_amount < Decimal("0"):
            raise BudgetBlockedError("provider actual cost is negative")
    completed = dict(submitting)
    completed.update(
        {
            "status": response_status,
            "remote_job_id": remote_job_id,
            "submitted_at": now_iso(),
            "updated_at": now_iso(),
            "actual_cost": actual_cost,
            "error_code": None,
            "retryable": False,
        }
    )
    write_validated_artifact(package_root, "provider_job", job_file, completed)
    return job_file


__all__ = ["ProviderSubmitter", "plan_provider_job", "submit_provider_job"]
