from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

QA_OVERRIDE_IMPLEMENTATION_VERSION = "p10-06f"
_TARGET_SCHEMAS = frozenset(
    {
        "qa_report",
        "segment_qa_report",
        "segment_visual_qa_report",
        "final_qa_report",
        "focus_pacing_qa",
    }
)


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = layout.root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _file_ref(artifact_id: str, path: Path) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _target_report(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
) -> tuple[Path, dict[str, Any]]:
    selected = _owned_path(layout, path, "QA report")
    if not selected.is_file():
        raise PlanningValidationError(f"QA report does not exist: {selected}")
    report = _read_object(selected, "QA report")
    schema_name = str(report.get("schema_name", ""))
    if schema_name not in _TARGET_SCHEMAS:
        raise PlanningValidationError(
            "QA override target must be a supported QA report, not " + schema_name
        )
    validate_artifact(package_root, schema_name, report)
    if report.get("project_id") != layout.root.name:
        raise PlanningValidationError("QA report belongs to another project")
    if not isinstance(report.get("findings"), list):
        raise PlanningValidationError("QA report has no findings array")
    return selected, report


def _finding_index(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise PlanningValidationError("QA report findings must be an array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in findings:
        if not isinstance(item, Mapping):
            raise PlanningValidationError("QA report finding must be an object")
        finding_id = str(item.get("finding_id", ""))
        if not finding_id or finding_id in indexed:
            raise PlanningValidationError("QA report finding IDs must be unique")
        indexed[finding_id] = item
    return indexed


def _validate_evidence(
    layout: ProjectLayout,
    finding_id: str,
    paths: Sequence[Path],
) -> list[dict[str, Any]]:
    if not paths:
        raise PlanningValidationError(f"QA override {finding_id} requires retained evidence")
    refs: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for index, raw_path in enumerate(paths, start=1):
        selected = _owned_path(layout, raw_path, f"QA override evidence for {finding_id}")
        if not selected.is_file():
            raise PlanningValidationError(f"QA override evidence does not exist: {selected}")
        if selected in seen:
            raise PlanningValidationError(
                f"QA override evidence is duplicated for {finding_id}: {selected}"
            )
        seen.add(selected)
        refs.append(_file_ref(f"art_qa_override_{finding_id}_{index:03d}", selected))
    return refs


def _binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("created_at", None)
    reviewer = value.get("reviewer")
    if isinstance(reviewer, Mapping):
        reviewer_value = dict(reviewer)
        reviewer_value.pop("reviewed_at", None)
        value["reviewer"] = reviewer_value
    return value


def create_qa_override(
    package_root: Path,
    layout: ProjectLayout,
    qa_report_path: Path,
    evidence_by_finding: Mapping[str, Sequence[Path]],
    *,
    actor: str,
    role: str,
    reason: str,
    classification: str = "reviewed_non_defect",
    notes: str = "",
    expires_at: str | None = None,
    output_path: Path | None = None,
    revision_id: str | None = None,
) -> Path:
    """Write a human-only override for current warning findings.

    The report remains unchanged. Only warnings can be selected; failed or skipped
    findings must be repaired or handled by their own approval gate.
    """

    if not actor.strip() or not role.strip():
        raise PlanningValidationError("QA override actor and role are required")
    if not reason.strip():
        raise PlanningValidationError("QA override reason is required")
    if classification not in {
        "reviewed_non_defect",
        "intentional_static",
        "false_positive",
        "accepted_risk",
    }:
        raise PlanningValidationError("QA override classification is invalid")
    if not evidence_by_finding:
        raise PlanningValidationError("QA override requires at least one finding")

    selected_report_path, report = _target_report(package_root, layout, qa_report_path)
    target_revision = str(report.get("revision_id", ""))
    if revision_id is not None and target_revision != revision_id:
        raise PlanningValidationError("QA report belongs to another revision")
    indexed = _finding_index(report)
    selected_ids = list(evidence_by_finding)
    if len(selected_ids) != len(set(selected_ids)):
        raise PlanningValidationError("QA override finding IDs must be unique")

    findings: list[dict[str, Any]] = []
    evidence_inputs: list[tuple[str, Path]] = []
    for finding_id in selected_ids:
        finding = indexed.get(finding_id)
        if finding is None:
            raise PlanningValidationError(f"QA finding does not exist: {finding_id}")
        status = str(finding.get("status"))
        if status != "warning":
            raise PlanningValidationError(
                f"QA override may cover warnings only; {finding_id} is {status}"
            )
        evidence = _validate_evidence(layout, finding_id, evidence_by_finding[finding_id])
        findings.append(
            {
                "finding_id": finding_id,
                "check_code": str(finding.get("check_code", "")),
                "status": "warning",
                "severity": str(finding.get("severity", "medium")),
                "classification": classification,
                "reason": reason.strip(),
                "evidence": evidence,
            }
        )
        evidence_inputs.extend(
            (str(ref["artifact_id"]), Path(str(ref["path"]))) for ref in evidence
        )

    input_paths: list[tuple[str, Path]] = [(str(report["artifact_id"]), selected_report_path)]
    input_paths.extend(evidence_inputs)
    input_hashes = [sha256_file(path) for _artifact_id, path in input_paths]
    config = {
        "project_id": layout.root.name,
        "revision_id": target_revision,
        "actor": actor,
        "role": role,
        "reason": reason.strip(),
        "classification": classification,
        "notes": notes,
        "expires_at": expires_at,
        "findings": findings,
    }
    stage_key = make_stage_key(
        "qa-override",
        QA_OVERRIDE_IMPLEMENTATION_VERSION,
        input_hashes,
        config,
    )
    selected_output = (
        _owned_path(layout, output_path, "QA override output")
        if output_path is not None
        else (layout.review / "qa-overrides" / f"{stage_key[:16]}-qa-override.json").resolve()
    )
    evidence_paths = {Path(str(ref["path"])) for finding in findings for ref in finding["evidence"]}
    if selected_output in {selected_report_path, *evidence_paths}:
        raise PlanningValidationError("QA override output cannot overwrite its target or evidence")
    artifact_id = f"art_qa_override_{stage_key[:16]}"
    payload: dict[str, Any] = {
        "schema_name": "qa_override",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "project_id": layout.root.name,
        "revision_id": target_revision,
        "created_at": now_iso(),
        "producer": producer("qa-override", "human-review", QA_OVERRIDE_IMPLEMENTATION_VERSION),
        "inputs": [
            artifact_input(artifact_id_value, path) for artifact_id_value, path in input_paths
        ],
        "target_report": {
            "schema_name": str(report["schema_name"]),
            **_file_ref(str(report["artifact_id"]), selected_report_path),
        },
        "findings": findings,
        "decision": "overridden",
        "reviewer": {"actor": actor, "role": role, "reviewed_at": now_iso()},
        "reason": reason.strip(),
        "notes": notes,
        "expires_at": expires_at,
    }
    with ProjectLock(layout, stage="qa_override", revision_id=target_revision):
        if selected_output.is_file():
            current = _read_object(selected_output, "QA override")
            validate_artifact(package_root, "qa_override", current)
            if _binding(current) == _binding(payload):
                return selected_output
            raise StateConflictError("QA override output exists with different bindings")
        write_validated_artifact(package_root, "qa_override", selected_output, payload)
    return selected_output


def evaluate_qa_override(
    package_root: Path,
    layout: ProjectLayout,
    qa_report_path: Path,
    override_path: Path,
) -> dict[str, Any]:
    """Validate an override and report remaining required findings.

    This is intentionally an assessment, not a mutation. A caller must still
    bind the returned override hash into its human approval artifact.
    """

    selected_report_path, report = _target_report(package_root, layout, qa_report_path)
    selected_override = _owned_path(layout, override_path, "QA override")
    override = _read_object(selected_override, "QA override")
    validate_artifact(package_root, "qa_override", override)
    if override["project_id"] != layout.root.name:
        raise PlanningValidationError("QA override belongs to another project")
    if override["revision_id"] != report.get("revision_id"):
        raise PlanningValidationError("QA override and QA report revisions differ")
    expires_at = override.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PlanningValidationError("QA override expiry is invalid") from exc
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise PlanningValidationError("QA override has expired")
    target = override["target_report"]
    if (
        target["schema_name"] != report["schema_name"]
        or target["artifact_id"] != report["artifact_id"]
        or target["path"] != str(selected_report_path)
        or target["sha256"] != sha256_file(selected_report_path)
        or target["size_bytes"] != selected_report_path.stat().st_size
    ):
        raise PlanningValidationError("QA override target report is stale")

    indexed = _finding_index(report)
    inputs = override.get("inputs")
    if not isinstance(inputs, list):
        raise PlanningValidationError("QA override inputs are invalid")
    input_hashes = {
        str(item["artifact_id"]): str(item["sha256"])
        for item in inputs
        if isinstance(item, Mapping)
    }
    if len(input_hashes) != len(inputs):
        raise PlanningValidationError("QA override input IDs must be unique")
    if input_hashes.get(str(target["artifact_id"])) != target["sha256"]:
        raise PlanningValidationError("QA override target input binding is stale")
    overridden: set[str] = set()
    for item in override["findings"]:
        finding_id = str(item["finding_id"])
        if finding_id in overridden:
            raise PlanningValidationError(f"QA override finding is duplicated: {finding_id}")
        current = indexed.get(finding_id)
        if current is None:
            raise PlanningValidationError(f"QA override finding is stale: {finding_id}")
        if current.get("check_code") != item["check_code"] or current.get("status") != "warning":
            raise PlanningValidationError(f"QA override finding changed: {finding_id}")
        for evidence in item["evidence"]:
            evidence_path = _owned_path(layout, Path(str(evidence["path"])), "QA override evidence")
            if (
                not evidence_path.is_file()
                or evidence["sha256"] != sha256_file(evidence_path)
                or evidence["size_bytes"] != evidence_path.stat().st_size
            ):
                raise PlanningValidationError("QA override evidence is stale")
            if input_hashes.get(str(evidence["artifact_id"])) != evidence["sha256"]:
                raise PlanningValidationError("QA override evidence input binding is stale")
        overridden.add(finding_id)

    unresolved = [
        str(item["finding_id"])
        for item in report["findings"]
        if isinstance(item, Mapping)
        and item.get("required") is True
        and item.get("status") != "pass"
        and str(item.get("finding_id")) not in overridden
    ]
    return {
        "status": "ready" if not unresolved else "incomplete",
        "qa_report": str(selected_report_path),
        "qa_report_sha256": sha256_file(selected_report_path),
        "override": str(selected_override),
        "override_sha256": sha256_file(selected_override),
        "overridden_finding_ids": sorted(overridden),
        "unresolved_required_finding_ids": unresolved,
        "report_final_ready": bool(report.get("final_ready", False)),
        "operator_approval_still_required": True,
    }


__all__ = [
    "QA_OVERRIDE_IMPLEMENTATION_VERSION",
    "create_qa_override",
    "evaluate_qa_override",
]
