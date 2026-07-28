from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from videoedit.errors import BudgetBlockedError, DependencyUnavailableError, ProviderTransientError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.project import ProjectLayout, initialize_project
from videoedit.services.provider_jobs import plan_provider_job, submit_provider_job

ROOT = Path(__file__).resolve().parents[2]


class _FakeProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def submit(self, job_path: Path, idempotency_key: str) -> dict[str, str]:
        del job_path, idempotency_key
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture provider interruption")
        return {"remote_job_id": "remote_fixture_001", "status": "submitted"}


def _approval(
    layout: ProjectLayout,
    filename: str,
    approval_id: str,
    approval_type: str,
    request_sha256: str,
    maximum_amount: str | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": f"art_{approval_id}",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-24T10:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "provider-approval-fixture",
            "adapter": "human-review",
            "adapter_version": "1",
        },
        "inputs": [{"artifact_id": "art_provider_job", "sha256": request_sha256}],
        "config_sha256": "0" * 64,
        "approval_id": approval_id,
        "approval_type": approval_type,
        "actor": "fixture-reviewer",
        "role": "editor",
        "decision": "approved",
        "reason": "Provider boundary fixture only",
        "approved_item_type": "provider_request",
        "approved_item_sha256": request_sha256,
        "expires_at": None,
        "budget": (
            {"currency": "USD", "maximum_amount": maximum_amount}
            if approval_type == "spend" and maximum_amount is not None
            else None
        ),
    }
    path = layout.artifacts / filename
    write_validated_artifact(ROOT, "approval_record", path, payload)
    return path


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path, Path, Path]:
    layout = initialize_project(tmp_path, "provider_job_fixture")
    plan = json.loads((ROOT / "examples" / "broll_plan.example.json").read_text(encoding="utf-8"))
    plan["project_id"] = layout.root.name
    plan["artifact_id"] = "art_provider_request_plan"
    request = plan["requests"][0]
    request.update(
        {
            "request_id": "brr_provider_001",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "estimated_cost": {
                "currency": "USD",
                "amount": "2.00",
                "estimate_id": "estimate_fixture_001",
            },
        }
    )
    plan_path = layout.work / "provider-broll-plan.json"
    write_validated_artifact(ROOT, "broll_plan", plan_path, plan)
    job_path = plan_provider_job(
        ROOT,
        layout,
        plan_path,
        "brr_provider_001",
    )
    request_sha256 = json.loads(job_path.read_text(encoding="utf-8"))["request_sha256"]
    effect = _approval(
        layout,
        "effect-approval.json",
        "apr_provider_effect",
        "edit",
        request_sha256,
    )
    spend = _approval(
        layout,
        "spend-approval.json",
        "apr_provider_spend",
        "spend",
        request_sha256,
        maximum_amount="2.50",
    )
    return layout, job_path, effect, spend, plan_path


def test_provider_job_is_disabled_until_approval_and_network_opt_in(tmp_path: Path) -> None:
    layout, job, effect, spend, _plan = _fixture(tmp_path)
    fake = _FakeProvider()
    with pytest.raises(DependencyUnavailableError, match="network is disabled"):
        submit_provider_job(
            ROOT,
            layout,
            job,
            effect,
            spend,
            adapter=fake,
        )
    assert fake.calls == 0
    assert json.loads(job.read_text(encoding="utf-8"))["status"] == "planned"

    submit_provider_job(
        ROOT,
        layout,
        job,
        effect,
        spend,
        network_enabled=True,
        adapter=fake,
    )
    assert fake.calls == 1
    submitted = json.loads(job.read_text(encoding="utf-8"))
    assert submitted["status"] == "submitted"
    assert submitted["remote_job_id"] == "remote_fixture_001"

    submit_provider_job(
        ROOT,
        layout,
        job,
        effect,
        spend,
        network_enabled=True,
        adapter=fake,
    )
    assert fake.calls == 1


def test_provider_job_enforces_spend_and_preserves_unresolved_submission(tmp_path: Path) -> None:
    layout, job, effect, spend, _plan = _fixture(tmp_path)
    spend_payload = json.loads(spend.read_text(encoding="utf-8"))
    spend_payload["budget"]["maximum_amount"] = "1.00"
    spend.write_text(json.dumps(spend_payload), encoding="utf-8")
    with pytest.raises(BudgetBlockedError, match="below"):
        submit_provider_job(
            ROOT,
            layout,
            job,
            effect,
            spend,
            network_enabled=True,
            adapter=_FakeProvider(),
        )

    _layout, retry_job, retry_effect, retry_spend, _retry_plan = _fixture(tmp_path / "retry")
    failing = _FakeProvider(fail=True)
    with pytest.raises(ProviderTransientError, match="unresolved"):
        submit_provider_job(
            ROOT,
            _layout,
            retry_job,
            retry_effect,
            retry_spend,
            network_enabled=True,
            adapter=failing,
        )
    assert failing.calls == 1
    assert json.loads(retry_job.read_text(encoding="utf-8"))["status"] == "submitting"
    with pytest.raises(ProviderTransientError, match="unresolved"):
        submit_provider_job(
            ROOT,
            _layout,
            retry_job,
            retry_effect,
            retry_spend,
            network_enabled=True,
            adapter=_FakeProvider(),
        )
