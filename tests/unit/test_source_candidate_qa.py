from __future__ import annotations

from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.source_candidate_qa import (
    _cached_report_matches,
    _candidate_path,
    _caption_evidence,
    _command_has_pair,
    _join_preview_evidence,
    _join_warning_breakdown,
    _report_status,
    _visual_evidence,
)


def test_source_candidate_accepts_project_and_workspace_output_paths(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")
    project_candidate = layout.root / "output" / "candidate.mp4"
    project_candidate.parent.mkdir(parents=True)
    project_candidate.write_bytes(b"project candidate")
    workspace_candidate = tmp_path / "outputs" / "candidate.mp4"
    workspace_candidate.parent.mkdir(parents=True)
    workspace_candidate.write_bytes(b"workspace candidate")

    assert _candidate_path(layout, project_candidate) == project_candidate.resolve()
    assert _candidate_path(layout, workspace_candidate) == workspace_candidate.resolve()


def test_source_candidate_rejects_candidate_outside_allowed_roots(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    with pytest.raises(PlanningValidationError, match="inside the project"):
        _candidate_path(layout, outside)


def test_source_candidate_command_profile_checks_adjacent_arguments() -> None:
    command = {"arguments": ["ffmpeg", "-c:v", "libx264", "-qp", "0"]}

    assert _command_has_pair(command, "-c:v", "libx264") is True
    assert _command_has_pair(command, "-qp", "0") is True
    assert _command_has_pair(command, "-qp", "1") is False


def test_source_candidate_visual_evidence_requires_media_files(tmp_path: Path) -> None:
    image = tmp_path / "contact-sheet.png"
    image.write_bytes(b"png")
    status, evidence = _visual_evidence([image])

    assert status == "pass"
    assert evidence["operator_review_required"] is True

    metadata = tmp_path / "diagnostics.json"
    metadata.write_text("{}\n", encoding="utf-8")
    status, evidence = _visual_evidence([metadata])

    assert status == "warning"
    assert evidence["unsupported"] == [str(metadata)]


def test_source_candidate_status_keeps_warnings_and_skipped_review() -> None:
    assert _report_status({"overall_status": "pass", "final_ready": True}) == "pass"
    assert _report_status({"overall_status": "pass", "final_ready": False}) == "warning"
    assert _report_status({"overall_status": "warning", "final_ready": False}) == "warning"
    assert _report_status({"overall_status": "fail", "final_ready": False}) == "fail"


def test_join_warning_breakdown_is_diagnostic_only() -> None:
    breakdown = _join_warning_breakdown(
        {
            "joins": [
                {
                    "status": "warning",
                    "transcript_check": {
                        "missing_words": ["one"],
                        "unexpected_words": [],
                        "duplicate_words": [],
                    },
                    "audio_check": {"clipped_syllable": True},
                    "visual_check": {"freeze": True},
                    "pacing_check": {"status": "warning"},
                    "preview": {"full_decode_status": "pass"},
                },
                {
                    "status": "fail",
                    "transcript_check": {
                        "missing_words": [],
                        "unexpected_words": [],
                        "duplicate_words": [],
                    },
                    "audio_check": {"clipped_syllable": False},
                    "visual_check": {"freeze": False},
                    "pacing_check": {"status": "pass"},
                    "preview": {"full_decode_status": "fail"},
                },
            ]
        }
    )

    assert breakdown == {
        "total": 2,
        "transcript_mismatch": 1,
        "freeze_evidence": 1,
        "clipped_syllable_evidence": 1,
        "pacing_warning": 1,
        "preview_decode_failure": 1,
        "hard_failure": 1,
    }


def test_join_preview_evidence_binds_each_current_child_file(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")
    preview_path = layout.review / "join-previews" / "join_000001.mp4"
    preview_path.parent.mkdir(parents=True)
    preview_path.write_bytes(b"join preview")

    status, evidence, inputs = _join_preview_evidence(
        layout,
        {
            "summary": {"total_joins": 1},
            "joins": [
                {
                    "join_id": "join_000001",
                    "preview": {
                        "file": {
                            "path": str(preview_path),
                            "sha256": sha256_file(preview_path),
                            "size_bytes": preview_path.stat().st_size,
                        },
                        "full_decode_status": "pass",
                    },
                }
            ],
        },
    )

    assert status == "pass"
    assert evidence["verified_count"] == 1
    assert evidence["failures"] == []
    assert inputs == [("art_join_preview_000001", preview_path.resolve())]


def test_join_preview_evidence_rejects_stale_or_non_decoded_children(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")
    preview_path = layout.review / "join-previews" / "join_000001.mp4"
    preview_path.parent.mkdir(parents=True)
    preview_path.write_bytes(b"join preview")

    status, evidence, _inputs = _join_preview_evidence(
        layout,
        {
            "summary": {"total_joins": 1},
            "joins": [
                {
                    "join_id": "join_000001",
                    "preview": {
                        "file": {
                            "path": str(preview_path),
                            "sha256": "0" * 64,
                            "size_bytes": preview_path.stat().st_size + 1,
                        },
                        "full_decode_status": "fail",
                    },
                }
            ],
        },
    )

    assert status == "fail"
    assert evidence["verified_count"] == 0
    assert evidence["failures"][0]["issues"] == [
        "preview_hash_stale",
        "preview_size_stale",
        "preview_full_decode_not_pass",
    ]


def test_join_preview_evidence_rejects_missing_preview_and_count_mismatch(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")

    status, evidence, inputs = _join_preview_evidence(
        layout,
        {
            "summary": {"total_joins": 2},
            "joins": [{"join_id": "join_000001"}],
        },
    )

    assert status == "fail"
    assert inputs == []
    assert evidence["failures"] == [
        {"join_id": "join_000001", "issues": ["preview_missing"]},
        {
            "join_id": None,
            "issues": ["summary_join_count_mismatch"],
            "expected_count": 2,
            "actual_count": 1,
        },
    ]


def test_source_candidate_cache_rejects_changed_bound_inputs() -> None:
    expected = {"created_at": "new", "candidate": {"sha256": "a" * 64}}
    current = {"created_at": "old", "candidate": {"sha256": "a" * 64}}

    assert _cached_report_matches(current, expected) is True
    assert (
        _cached_report_matches({"created_at": "old", "candidate": {"sha256": "b" * 64}}, expected)
        is False
    )


def test_source_candidate_caption_evidence_validates_sidecars_and_timing(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "projects" / "candidate")
    layout.root.mkdir(parents=True)
    outputs = layout.artifacts / "captions"
    outputs.mkdir(parents=True)
    references = {}
    for name in ("ass", "webvtt", "text"):
        path = outputs / f"captions.{name}"
        path.write_text(name, encoding="utf-8")
        references[name] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    caption_path = layout.artifacts / "caption-plan.json"
    caption_path.write_text("{}", encoding="utf-8")
    status, evidence = _caption_evidence(
        Path(__file__).resolve().parents[2],
        layout,
        caption_path,
        {
            "project_id": layout.root.name,
            "revision_id": "rev_002",
            "target_width": 1280,
            "target_height": 720,
            "outputs": references,
            "events": [
                {"start_us": 0, "end_us": 500_000},
                {"start_us": 500_000, "end_us": 1_000_000},
            ],
            "warnings": [],
        },
        revision_id="rev_002",
        duration_us=1_000_000,
        width=1280,
        height=720,
    )

    assert status == "pass"
    assert evidence["event_count"] == 2
    assert evidence["burn_in"] is False
