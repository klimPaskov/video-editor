from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.edit_metrics import (
    EditMetricsPolicy,
    measure_edit_metrics,
    write_edit_metrics_qa,
)
from videoedit.services.project import ProjectLayout

ROOT = Path(__file__).resolve().parents[2]


def _fixture_payloads() -> tuple[
    dict[str, object], dict[str, object], dict[str, object], dict[str, object]
]:
    edl = json.loads(
        (ROOT / "examples" / "edit_decision_list.example.json").read_text(encoding="utf-8")
    )
    proposals = json.loads(
        (ROOT / "examples" / "edit_proposals.example.json").read_text(encoding="utf-8")
    )
    transcript = json.loads(
        (ROOT / "examples" / "transcript.example.json").read_text(encoding="utf-8")
    )
    transitions = json.loads(
        (ROOT / "examples" / "transition_plan.example.json").read_text(encoding="utf-8")
    )
    return edl, proposals, transcript, transitions


def test_edit_metrics_are_diagnostic_and_high_cut_density_does_not_fail() -> None:
    edl, proposals, transcript, transitions = _fixture_payloads()
    result = measure_edit_metrics(edl, proposals, transcript, transition_plan=transitions)

    assert result["blocking"] is False
    assert result["cut_density"]["status"] == "warning"
    assert result["transition_frequency"]["status"] == "warning"
    assert result["repetition"]["candidate_count"] == 1
    assert result["overall_status"] == "warning"
    assert "cut_density_above_warning_threshold" in result["warnings"]


def test_metrics_measure_cadence_and_repetition_signals() -> None:
    edl, proposals, transcript, _transitions = _fixture_payloads()
    proposals["proposals"] = [
        {
            "proposal_id": "prp_exact",
            "proposal_type": "exact_repetition",
            "policy_result": "review_required",
        },
        {
            "proposal_id": "prp_semantic",
            "proposal_type": "semantic_repetition",
            "policy_result": "review_required",
        },
    ]
    edl["deletions"] = [
        {
            **edl["deletions"][0],
            "proposal_ids": ["prp_exact"],
        }
    ]
    result = measure_edit_metrics(edl, proposals, transcript)

    assert result["cadence"]["source_word_count"] == 5
    assert result["cadence"]["output_word_count"] == 5
    assert result["cadence"]["status"] == "warning"
    assert result["repetition"]["candidate_counts"] == {
        "exact_repetition": 1,
        "semantic_repetition": 1,
    }
    assert result["repetition"]["applied_counts"] == {"exact_repetition": 1}
    assert result["repetition"]["review_required_count"] == 2


def test_missing_optional_transition_plan_is_visible_but_non_blocking() -> None:
    edl, proposals, transcript, _transitions = _fixture_payloads()
    result = measure_edit_metrics(edl, proposals, transcript)

    assert result["transition_frequency"]["status"] == "not_measured"
    assert "transition_frequency_not_measured" in result["warnings"]
    assert result["blocking"] is False


def test_metrics_writer_emits_schema_valid_report(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "metrics_demo")
    edl, proposals, transcript, transitions = _fixture_payloads()
    for payload in (edl, proposals, transcript, transitions):
        payload["project_id"] = "metrics_demo"
    edl_path = layout.artifacts / "edit-decision-list.json"
    proposals_path = layout.artifacts / "edit-proposals.json"
    transcript_path = layout.artifacts / "transcript.json"
    transition_path = layout.artifacts / "transition-plan.json"
    write_validated_artifact(ROOT, "edit_decision_list", edl_path, edl)
    write_validated_artifact(ROOT, "edit_proposals", proposals_path, proposals)
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)
    write_validated_artifact(ROOT, "transition_plan", transition_path, transitions)

    report_path = write_edit_metrics_qa(
        ROOT,
        layout,
        edl_path,
        proposals_path,
        transcript_path,
        transition_plan_path=transition_path,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "edit_metrics_qa", report)
    assert report["blocking"] is False
    assert report["transition_frequency"]["motion_transition_count"] == 1


def test_metrics_policy_reads_explicit_thresholds() -> None:
    policy = EditMetricsPolicy.from_yaml(
        ROOT / "config" / "editing-policy.example.yaml",
        ROOT / "config" / "transitions.example.yaml",
    )
    assert policy.warning_cuts_per_minute == 30
    assert policy.warning_average_kept_fragment_us == 700_000
    assert policy.warning_minimum_kept_fragment_us == 360_000
    assert policy.minimum_motion_transition_spacing_us == 12_000_000
