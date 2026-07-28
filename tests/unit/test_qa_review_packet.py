from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.qa_review_packet import _join_categories, write_qa_review_packet


def _example(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / "examples" / name).read_text(encoding="utf-8"))


def test_join_categories_separate_review_status_from_concrete_signals() -> None:
    categories = _join_categories(
        {
            "status": "warning",
            "transcript_check": {
                "missing_words": [],
                "unexpected_words": [],
                "duplicate_words": [],
            },
            "audio_check": {
                "clipped_syllable": True,
                "click_or_pop": False,
                "room_tone_jump": False,
                "status": "warning",
            },
            "visual_check": {
                "black_flash": False,
                "freeze": True,
                "duplicate_frame": False,
                "face_or_body_jump": "none",
                "screen_state_jump": "none",
                "status": "warning",
            },
            "pacing_check": {"status": "pass"},
        }
    )

    assert [item["check_code"] for item in categories] == [
        "AUDIO_CLIPPING",
        "AUDIO_JOIN_REVIEW",
        "VISUAL_FREEZE",
        "VISUAL_JOIN_REVIEW",
    ]


def test_write_qa_review_packet_is_hash_bound_and_pending_only(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "qa_packet_demo")
    candidate = layout.output / "candidate.mp4"
    candidate.write_bytes(b"candidate media")
    preview = layout.review / "join-previews" / "join_001.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"join preview")

    join = _example(package_root, "join_qa_report.example.json")
    join["project_id"] = layout.root.name
    join["summary"].update({"passed": 0, "warnings": 1})
    join["overall_status"] = "warning"
    join_entry = join["joins"][0]
    join_entry["preview"]["file"] = {
        "path": str(preview),
        "sha256": sha256_file(preview),
        "size_bytes": preview.stat().st_size,
    }
    join_entry.update(
        {
            "status": "warning",
            "review_required": True,
            "transcript_check": {
                **join_entry["transcript_check"],
                "missing_words": ["result"],
            },
        }
    )
    join_path = layout.artifacts / "join-qa.json"
    write_validated_artifact(package_root, "join_qa_report", join_path, join)

    segment = _example(package_root, "segment_qa_report.example.json")
    segment["project_id"] = layout.root.name
    segment["media"] = {
        "artifact_id": "revision_recut_media",
        "path": str(candidate),
        "sha256": sha256_file(candidate),
        "size_bytes": candidate.stat().st_size,
    }
    segment["findings"] = [
        {
            "finding_id": "finding_freeze_frames",
            "check_code": "FREEZE_FRAMES",
            "status": "warning",
            "severity": "medium",
            "message": "Classify the static frame interval.",
            "time_range": None,
            "evidence": {"freeze_starts": ["1.0"]},
            "required": True,
            "repair_hint": "Inspect the join context.",
        }
    ]
    segment["summary"] = {
        "total": 1,
        "passed": 0,
        "warnings": 1,
        "failed": 0,
        "skipped": 0,
        "required_failures": 1,
    }
    segment["overall_status"] = "warning"
    segment["final_ready"] = False
    segment_path = layout.revisions / "rev_002" / "segment-qa.json"
    write_validated_artifact(package_root, "segment_qa_report", segment_path, segment)

    final = _example(package_root, "final_qa_report.example.json")
    final["project_id"] = layout.root.name
    final["candidate"] = {
        "artifact_id": "art_source_candidate",
        "path": str(candidate),
        "sha256": sha256_file(candidate),
        "size_bytes": candidate.stat().st_size,
    }
    final["inputs"] = [
        {"artifact_id": join["artifact_id"], "sha256": sha256_file(join_path)},
        {"artifact_id": segment["artifact_id"], "sha256": sha256_file(segment_path)},
    ]
    final["overall_status"] = "warning"
    final["final_ready"] = False
    final["findings"] = [
        {
            "finding_id": "finding_source_join_review",
            "check_code": "JOIN_REVIEW",
            "status": "warning",
            "severity": "medium",
            "message": "Rendered join needs review.",
            "required": True,
            "evidence": {},
        }
    ]
    final["required_failures"] = 0
    final["warnings_count"] = 1
    final_path = layout.review / "final-qa.json"
    write_validated_artifact(package_root, "final_qa_report", final_path, final)

    packet_path = write_qa_review_packet(
        package_root,
        layout,
        candidate,
        final_qa_path=final_path,
        join_qa_path=join_path,
        segment_qa_path=segment_path,
        revision_id="rev_002",
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "qa_review_packet", packet)

    assert packet["status"] == "review_required"
    assert packet["summary"]["join_preview_count"] == 1
    assert packet["summary"]["segment_item_count"] == 1
    assert packet["summary"]["final_item_count"] == 0
    assert packet["summary"]["join_warning_by_code"] == {"TRANSCRIPT_SEQUENCE": 1}
    assert len(packet["items"]) == 2
    assert all(item["decision"] == "pending" for item in packet["items"])
    assert any(item["artifact_id"] == "art_join_preview_000001" for item in packet["inputs"])
    assert packet_path.with_suffix(".md").is_file()
