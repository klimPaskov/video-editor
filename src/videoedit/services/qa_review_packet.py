from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.source_candidate_qa import _join_preview_evidence

IMPLEMENTATION_VERSION = "p11-qa-review-packet-3"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    selected = path.expanduser().resolve()
    try:
        selected.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return selected


def _candidate_path(layout: ProjectLayout, path: Path) -> Path:
    selected = path.expanduser().resolve()
    workspace = layout.root.parent.parent.resolve()
    allowed_roots = (layout.root.resolve(), (workspace / "outputs").resolve())
    if not any(selected == root or root in selected.parents for root in allowed_roots):
        raise PlanningValidationError(
            "QA review packet candidate must be inside the project or workspace outputs directory"
        )
    if not selected.is_file():
        raise PlanningValidationError(f"QA review packet candidate does not exist: {selected}")
    return selected


def _validated_input(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    schema_name: str,
    description: str,
) -> tuple[Path, dict[str, Any]]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    value = _read_object(selected, description)
    validate_artifact(package_root, schema_name, value)
    if value.get("project_id") != layout.root.name:
        raise PlanningValidationError(f"{description} belongs to another project")
    return selected, value


def _file_ref(path: Path, artifact_id: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise PlanningValidationError(f"review packet evidence is missing or empty: {path}")
    return {
        "artifact_id": artifact_id,
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _bound_input(report: Mapping[str, Any], path: Path, *artifact_ids: str) -> bool:
    expected_sha = sha256_file(path)
    inputs = report.get("inputs")
    return isinstance(inputs, list) and any(
        isinstance(item, Mapping)
        and item.get("sha256") == expected_sha
        and (not artifact_ids or item.get("artifact_id") in artifact_ids)
        for item in inputs
    )


def _range(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    start_us = value.get("start_us")
    end_us = value.get("end_us")
    if (
        not isinstance(start_us, int)
        or not isinstance(end_us, int)
        or start_us < 0
        or end_us <= start_us
    ):
        return None
    return {"start_us": start_us, "end_us": end_us}


def _join_categories(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    categories: list[dict[str, Any]] = []
    transcript = entry.get("transcript_check")
    if isinstance(transcript, Mapping) and any(
        transcript.get(key) for key in ("missing_words", "unexpected_words", "duplicate_words")
    ):
        categories.append(
            {
                "check_code": "TRANSCRIPT_SEQUENCE",
                "severity": "high",
                "recommendation": (
                    "Inspect the join preview and rendered re-transcription; repair the join "
                    "or retain the current range if the difference is ASR-only."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    audio = entry.get("audio_check")
    if isinstance(audio, Mapping) and audio.get("clipped_syllable") is True:
        categories.append(
            {
                "check_code": "AUDIO_CLIPPING",
                "severity": "medium",
                "recommendation": (
                    "Inspect at least 250 ms on both sides of the join for clipping, clicks, "
                    "dropouts, and room-tone discontinuity before choosing repair or override."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    elif isinstance(audio, Mapping) and (
        audio.get("click_or_pop") is True or audio.get("room_tone_jump") is True
    ):
        categories.append(
            {
                "check_code": "AUDIO_CONTINUITY",
                "severity": "medium",
                "recommendation": (
                    "Inspect at least 250 ms on both sides of the join for clicks, dropouts, "
                    "or a room-tone discontinuity."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    if isinstance(audio, Mapping) and audio.get("status") in {"warning", "fail"}:
        categories.append(
            {
                "check_code": "AUDIO_JOIN_REVIEW",
                "severity": "medium",
                "recommendation": (
                    "Review the audio evidence around this join even when no concrete "
                    "clipping or click signal is confirmed."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    visual = entry.get("visual_check")
    if isinstance(visual, Mapping) and visual.get("freeze") is True:
        categories.append(
            {
                "check_code": "VISUAL_FREEZE",
                "severity": "medium",
                "recommendation": (
                    "Inspect the join preview around the boundary and classify the freeze "
                    "as intentional static screen state or a visual defect."
                ),
                "decision_options": [
                    "repair",
                    "intentional_static",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    elif isinstance(visual, Mapping) and (
        visual.get("black_flash") is True
        or visual.get("duplicate_frame") is True
        or visual.get("face_or_body_jump") == "distracting"
        or visual.get("screen_state_jump") == "unexplained"
    ):
        categories.append(
            {
                "check_code": "VISUAL_CONTINUITY",
                "severity": "medium",
                "recommendation": (
                    "Inspect the join preview for a black flash, duplicate frame, face jump, "
                    "or unexplained screen-state change."
                ),
                "decision_options": [
                    "repair",
                    "intentional_static",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    if isinstance(visual, Mapping) and visual.get("status") in {"warning", "fail"}:
        categories.append(
            {
                "check_code": "VISUAL_JOIN_REVIEW",
                "severity": "medium",
                "recommendation": (
                    "Review the visual evidence around this join even when no concrete "
                    "freeze or continuity defect is confirmed."
                ),
                "decision_options": [
                    "repair",
                    "intentional_static",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    pacing = entry.get("pacing_check")
    if isinstance(pacing, Mapping) and pacing.get("status") in {"warning", "fail"}:
        categories.append(
            {
                "check_code": "PACING",
                "severity": "medium",
                "recommendation": (
                    "Listen and watch through the join context for rushed speech, clipped "
                    "cadence, or an unnatural fragment before approving a repair or override."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    if not categories and entry.get("status") in {"warning", "fail"}:
        categories.append(
            {
                "check_code": "JOIN_REVIEW",
                "severity": "high",
                "recommendation": (
                    "Inspect the complete rendered join evidence and repair or reject the join."
                ),
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
            }
        )
    return categories


def _max_severity(categories: Sequence[Mapping[str, Any]]) -> str:
    order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    return max((str(item["severity"]) for item in categories), key=lambda value: order[value])


def _join_item(
    entry: Mapping[str, Any],
    index: int,
    preview_ref: Mapping[str, Any],
) -> dict[str, Any] | None:
    categories = _join_categories(entry)
    if not categories:
        return None
    join_id = str(entry.get("join_id", f"join_{index:06d}"))
    options: list[str] = []
    for category in categories:
        for option in category["decision_options"]:
            if option not in options:
                options.append(option)
    return {
        "item_id": f"qa_join_{join_id}",
        "scope": "join",
        "check_code": "JOIN_REVIEW",
        "severity": _max_severity(categories),
        "source_finding_id": f"qa_join_warning_{join_id}",
        "status": "warning",
        "title": f"{join_id}: {', '.join(str(item['check_code']) for item in categories)}",
        "recommendation": " ".join(str(item["recommendation"]) for item in categories),
        "decision_options": options,
        "decision": "pending",
        "time_range": _range(entry.get("preview_range")),
        "evidence": [dict(preview_ref)],
        "details": {
            "join_id": join_id,
            "output_join_us": entry.get("output_join_us"),
            "preview_range": _range(entry.get("preview_range")),
            "diagnostic_window": _range(entry.get("diagnostic_window")),
            "categories": categories,
            "transcript_check": dict(entry.get("transcript_check", {}))
            if isinstance(entry.get("transcript_check"), Mapping)
            else {},
            "audio_check": dict(entry.get("audio_check", {}))
            if isinstance(entry.get("audio_check"), Mapping)
            else {},
            "visual_check": dict(entry.get("visual_check", {}))
            if isinstance(entry.get("visual_check"), Mapping)
            else {},
            "pacing_check": dict(entry.get("pacing_check", {}))
            if isinstance(entry.get("pacing_check"), Mapping)
            else {},
            "review_context_ms": 250,
        },
    }


def _compact_finding_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key.endswith("_tail"):
            continue
        if isinstance(item, list):
            compact[key] = {
                "count": len(item),
                "values": item[:25],
                "truncated": len(item) > 25,
            }
        elif isinstance(item, Mapping):
            compact[key] = {"keys": sorted(str(child) for child in item)}
        else:
            compact[key] = item
    return compact


def _segment_item(
    finding: Mapping[str, Any],
    segment_ref: Mapping[str, Any],
) -> dict[str, Any]:
    finding_id = str(finding["finding_id"])
    check_code = str(finding["check_code"])
    options = ["repair", "false_positive", "accepted_risk", "reject_candidate"]
    if check_code == "FREEZE_FRAMES":
        options.insert(1, "intentional_static")
    return {
        "item_id": f"qa_segment_{check_code.casefold()}",
        "scope": "segment",
        "check_code": check_code,
        "severity": str(finding["severity"]),
        "source_finding_id": finding_id,
        "status": "warning",
        "title": str(finding["message"]),
        "recommendation": str(
            finding.get("repair_hint")
            or "Inspect the retained segment evidence and repair or document a current override."
        ),
        "decision_options": options,
        "decision": "pending",
        "time_range": _range(finding.get("time_range")),
        "evidence": [dict(segment_ref)],
        "details": {
            "finding_message": str(finding["message"]),
            "repair_hint": finding.get("repair_hint"),
            "finding_evidence": _compact_finding_evidence(
                finding.get("evidence", {}) if isinstance(finding.get("evidence"), Mapping) else {}
            ),
        },
    }


def _final_item(
    finding: Mapping[str, Any],
    final_ref: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "item_id": f"qa_final_{str(finding['check_code']).casefold()}",
        "scope": "final",
        "check_code": str(finding["check_code"]),
        "severity": str(finding["severity"]),
        "source_finding_id": str(finding["finding_id"]),
        "status": "warning",
        "title": str(finding["message"]),
        "recommendation": (
            "Compare the intended and rendered transcript around the retained evidence, "
            "then repair, classify the difference, or reject the candidate."
        ),
        "decision_options": ["repair", "false_positive", "accepted_risk", "reject_candidate"],
        "decision": "pending",
        "time_range": _range(finding.get("time_range")),
        "evidence": [dict(final_ref)],
        "details": {
            "finding_evidence": _compact_finding_evidence(
                finding.get("evidence", {}) if isinstance(finding.get("evidence"), Mapping) else {}
            )
        },
    }


def _cached_payload_matches(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return set(current) == set(expected) and all(
        key == "created_at" or current.get(key) == value for key, value in expected.items()
    )


def qa_review_packet_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# QA operator review packet",
        "",
        f"- Project: `{payload['project_id']}`",
        f"- Revision: `{payload['revision_id']}`",
        f"- Review gate: `{payload['review_gate']}`",
        "- Status: `review_required` (no decisions recorded)",
        f"- Candidate: `{payload['candidate']['sha256']}`",
        f"- Items: {summary['total_items']} ({summary['join_item_count']} joins, "
        f"{summary['segment_item_count']} segment, {summary['final_item_count']} final)",
        f"- High-severity items: {summary['high_severity_count']}",
        "",
        "Each item remains pending. Inspect the retained evidence, then repair the candidate "
        "or create a current human QA override. This packet does not approve Gate 2, Gate 3, "
        "delivery, or cleanup.",
        "",
        "## Warning counts",
        "",
        f"- Join warnings: `{json.dumps(summary['join_warning_by_code'], sort_keys=True)}`",
        f"- Segment warnings: `{json.dumps(summary['segment_warning_by_code'], sort_keys=True)}`",
        "",
    ]
    for scope in ("final", "segment", "join"):
        scoped = [item for item in payload["items"] if item["scope"] == scope]
        if not scoped:
            continue
        lines.extend([f"## {scope.title()} review items", ""])
        for item in scoped:
            lines.extend(
                [
                    f"### {item['item_id']} | {item['check_code']} | {item['severity']}",
                    "",
                    f"- Title: {item['title']}",
                    f"- Range: {item['time_range'] or 'not bounded'}",
                    f"- Recommendation: {item['recommendation']}",
                    f"- Decisions: {', '.join(item['decision_options'])}",
                    "- Current decision: `pending`",
                    f"- Evidence: {', '.join(str(ref['path']) for ref in item['evidence'])}",
                    "",
                ]
            )
    return "\n".join(lines)


def write_qa_review_packet(
    package_root: Path,
    layout: ProjectLayout,
    candidate_path: Path,
    *,
    final_qa_path: Path,
    join_qa_path: Path,
    segment_qa_path: Path,
    revision_id: str = "rev_002",
    review_gate: str = "gate3",
) -> Path:
    """Persist a current, pending-only review packet for unresolved QA warnings."""

    if review_gate not in {"gate2", "gate3"}:
        raise PlanningValidationError("review gate must be gate2 or gate3")
    candidate = _candidate_path(layout, candidate_path)
    final_path, final = _validated_input(
        package_root, layout, final_qa_path, "final_qa_report", "final QA report"
    )
    join_path, joins = _validated_input(
        package_root, layout, join_qa_path, "join_qa_report", "join QA report"
    )
    segment_path, segment = _validated_input(
        package_root, layout, segment_qa_path, "segment_qa_report", "segment QA report"
    )
    if final.get("revision_id") != revision_id or segment.get("revision_id") != revision_id:
        raise PlanningValidationError(
            "final and segment QA reports must match the requested revision"
        )
    candidate_hash = sha256_file(candidate)
    candidate_ref = _file_ref(candidate, "art_source_candidate")
    if final.get("candidate", {}).get("sha256") != candidate_hash:
        raise PlanningValidationError("final QA report is stale for the candidate")
    if not _bound_input(final, join_path, str(joins["artifact_id"]), "art_join_qa_report"):
        raise PlanningValidationError("final QA report is not bound to the join QA report")
    if not _bound_input(final, segment_path, str(segment["artifact_id"]), "art_segment_qa"):
        raise PlanningValidationError("final QA report is not bound to the segment QA report")

    join_preview_status, join_preview_evidence_value, preview_inputs = _join_preview_evidence(
        layout, joins
    )
    if join_preview_status != "pass":
        raise PlanningValidationError(
            "QA review packet cannot consume incomplete join preview evidence: "
            f"{join_preview_evidence_value.get('failures', [])}"
        )
    items: list[dict[str, Any]] = []
    join_warning_by_code: dict[str, int] = {}
    entries = joins.get("joins")
    if not isinstance(entries, list):
        raise PlanningValidationError("join QA report has no joins array")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise PlanningValidationError("join QA report contains a non-object join")
        join_id = str(entry.get("join_id", f"join_{index:06d}"))
        preview = entry.get("preview")
        preview_file = preview.get("file") if isinstance(preview, Mapping) else None
        if not isinstance(preview_file, Mapping):
            continue
        raw_preview_path = preview_file.get("path")
        if not isinstance(raw_preview_path, str) or not raw_preview_path:
            continue
        preview_path = _owned_path(layout, Path(raw_preview_path), f"join preview {join_id}")
        preview_ref = _file_ref(preview_path, f"art_join_preview_{index:06d}")
        item = _join_item(entry, index, preview_ref)
        if item is None:
            continue
        items.append(item)
        for category in item["details"]["categories"]:
            code = str(category["check_code"])
            join_warning_by_code[code] = join_warning_by_code.get(code, 0) + 1

    segment_ref = _file_ref(segment_path, str(segment["artifact_id"]))
    segment_warning_by_code: dict[str, int] = {}
    segment_findings = segment.get("findings")
    if not isinstance(segment_findings, list):
        raise PlanningValidationError("segment QA report has no findings array")
    for finding in segment_findings:
        if not isinstance(finding, Mapping) or finding.get("status") != "warning":
            continue
        item = _segment_item(finding, segment_ref)
        items.append(item)
        code = str(finding["check_code"])
        segment_warning_by_code[code] = segment_warning_by_code.get(code, 0) + 1

    final_ref = _file_ref(final_path, str(final["artifact_id"]))
    source_warning_findings: list[Mapping[str, Any]] = []
    final_findings = final.get("findings")
    if not isinstance(final_findings, list):
        raise PlanningValidationError("final QA report has no findings array")
    for finding in final_findings:
        if isinstance(finding, Mapping) and finding.get("status") == "warning":
            source_warning_findings.append(finding)
    if not source_warning_findings:
        raise PlanningValidationError("final QA report has no warning findings to packet")
    covered_final_codes = {"JOIN_REVIEW", "SEGMENT_QA"}
    for finding in source_warning_findings:
        if str(finding.get("check_code")) not in covered_final_codes:
            items.append(_final_item(finding, final_ref))
    if not items:
        raise PlanningValidationError("current QA reports contain no warning items to review")

    input_paths: list[tuple[str, Path]] = [
        (str(final["artifact_id"]), final_path),
        (str(joins["artifact_id"]), join_path),
        (str(segment["artifact_id"]), segment_path),
        *preview_inputs,
    ]
    stage_key = make_stage_key(
        "qa-review-packet",
        IMPLEMENTATION_VERSION,
        [sha256_file(path) for _artifact_id, path in input_paths],
        {
            "revision_id": revision_id,
            "review_gate": review_gate,
            "candidate_sha256": candidate_hash,
            "join_warning_by_code": join_warning_by_code,
            "segment_warning_by_code": segment_warning_by_code,
        },
    )
    output = layout.review / f"qa-review-packet-{stage_key[:16]}.json"
    summary = {
        "total_items": len(items),
        "join_item_count": sum(1 for item in items if item["scope"] == "join"),
        "segment_item_count": sum(1 for item in items if item["scope"] == "segment"),
        "final_item_count": sum(1 for item in items if item["scope"] == "final"),
        "high_severity_count": sum(1 for item in items if item["severity"] == "high"),
        "pending_item_count": len(items),
        "join_preview_count": len(preview_inputs),
        "join_warning_by_code": join_warning_by_code,
        "segment_warning_by_code": segment_warning_by_code,
        "source_warning_finding_ids": [str(item["finding_id"]) for item in source_warning_findings],
    }
    payload: dict[str, Any] = {
        "schema_name": "qa_review_packet",
        "schema_version": "1.0.0",
        "artifact_id": "art_qa_review_packet",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("qa-review-packet", "deterministic-evidence", __version__),
        "inputs": [artifact_input(artifact_id, path) for artifact_id, path in input_paths],
        "candidate": candidate_ref,
        "review_gate": review_gate,
        "status": "review_required",
        "summary": summary,
        "items": items,
    }
    with ProjectLock(layout, stage="qa_review_packet", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "QA review packet")
            validate_artifact(package_root, "qa_review_packet", current)
            if not _cached_payload_matches(current, payload):
                raise StateConflictError("QA review packet exists with stale contents")
            return output
        write_validated_artifact(package_root, "qa_review_packet", output, payload)
        write_text_atomically(output.with_suffix(".md"), qa_review_packet_markdown(payload))
    return output


__all__ = [
    "IMPLEMENTATION_VERSION",
    "qa_review_packet_markdown",
    "write_qa_review_packet",
]
