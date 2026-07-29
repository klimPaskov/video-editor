from __future__ import annotations

from pathlib import Path

from videoedit.services.final_qa import (
    _cached_report_matches_payload,
    _visual_evidence_check,
)


def test_visual_evidence_requires_non_empty_image_or_video(tmp_path: Path) -> None:
    contact_sheet = tmp_path / "contact-sheet.PNG"
    contact_sheet.write_bytes(b"png fixture")

    valid, evidence = _visual_evidence_check([contact_sheet])

    assert valid is True
    assert evidence["path_count"] == 1
    assert evidence["missing"] == []
    assert evidence["empty"] == []
    assert evidence["unsupported"] == []


def test_visual_evidence_rejects_empty_unsupported_and_missing_files(tmp_path: Path) -> None:
    empty_video = tmp_path / "preview.mp4"
    empty_video.touch()
    metadata = tmp_path / "diagnostics.json"
    metadata.write_text("{}\n", encoding="utf-8")
    missing = tmp_path / "contact-sheet.png"

    valid, evidence = _visual_evidence_check([empty_video, metadata, missing])

    assert valid is False
    assert evidence["empty"] == [str(empty_video)]
    assert evidence["unsupported"] == [str(metadata)]
    assert evidence["missing"] == [str(missing)]


def test_visual_evidence_without_paths_fails_closed() -> None:
    valid, evidence = _visual_evidence_check([])

    assert valid is False
    assert evidence["path_count"] == 0


def test_final_qa_cache_requires_exact_current_report_contents() -> None:
    expected = {
        "created_at": "2026-07-26T00:00:00Z",
        "project_id": "project_fixture",
        "revision_id": "rev_001",
        "inputs": [{"artifact_id": "art_candidate", "sha256": "a" * 64}],
        "candidate": {"path": "candidate.mp4", "sha256": "b" * 64},
        "final_ready": True,
    }

    current = dict(expected)
    current["created_at"] = "2026-07-25T23:00:00Z"
    assert _cached_report_matches_payload(current, expected) is True

    tampered = dict(current)
    tampered["final_ready"] = False
    assert _cached_report_matches_payload(tampered, expected) is False

    missing_field = dict(current)
    del missing_field["candidate"]
    assert _cached_report_matches_payload(missing_field, expected) is False
