from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file
from videoedit.services.qa_review_visual_evidence import (
    sample_join_frames,
    write_qa_review_visual_evidence,
)


class _FakeReviewAdapter:
    def __init__(self) -> None:
        self.contact_sheet_calls = 0

    def probe_frame_count(self, path: Path) -> int | None:
        assert path.suffix == ".mp4"
        return 240

    def make_contact_sheet(
        self,
        source: Path,
        output: Path,
        frame_indices: tuple[int, ...] | list[int],
        *,
        scale_width: int = 320,
        tile_columns: int | None = None,
        filter_prefix: str | None = None,
        input_start_number: int | None = None,
    ) -> object:
        assert source.suffix == ".mp4"
        assert list(frame_indices) == [0, 105, 120, 134, 239]
        assert scale_width == 360
        assert tile_columns == 5
        assert filter_prefix is None
        assert input_start_number is None
        self.contact_sheet_calls += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"PNG review fixture")
        return object()


def _packet(package_root: Path, project_root: Path) -> Path:
    layout = initialize_project(project_root, "visual_evidence_demo")
    candidate = layout.output / "candidate.mp4"
    candidate.write_bytes(b"candidate")
    preview = layout.review / "join-previews" / "join_000001.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"preview")
    payload = {
        "schema_name": "qa_review_packet",
        "schema_version": "1.0.0",
        "artifact_id": "art_qa_review_packet_demo",
        "project_id": layout.root.name,
        "revision_id": "rev_002",
        "created_at": "2026-01-01T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "qa-review-packet",
            "adapter": "deterministic-evidence",
            "adapter_version": "1",
        },
        "inputs": [
            {"artifact_id": "art_final_qa", "sha256": "a" * 64},
            {"artifact_id": "art_join_qa", "sha256": "b" * 64},
        ],
        "candidate": {
            "artifact_id": "art_candidate",
            "path": str(candidate),
            "sha256": sha256_file(candidate),
            "size_bytes": candidate.stat().st_size,
        },
        "review_gate": "gate3",
        "status": "review_required",
        "summary": {
            "total_items": 1,
            "join_item_count": 1,
            "segment_item_count": 0,
            "final_item_count": 0,
            "high_severity_count": 1,
            "pending_item_count": 1,
            "join_preview_count": 1,
            "join_warning_by_code": {"TRANSCRIPT_SEQUENCE": 1},
            "segment_warning_by_code": {},
            "source_warning_finding_ids": ["finding_source_join_review"],
        },
        "items": [
            {
                "item_id": "qa_join_join_000001",
                "scope": "join",
                "check_code": "JOIN_REVIEW",
                "severity": "high",
                "source_finding_id": "qa_join_warning_join_000001",
                "status": "warning",
                "title": "join_000001: TRANSCRIPT_SEQUENCE",
                "recommendation": "Inspect the rendered join preview before deciding.",
                "decision_options": [
                    "repair",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
                "decision": "pending",
                "time_range": {"start_us": 0, "end_us": 4_000_000},
                "evidence": [
                    {
                        "artifact_id": "art_join_preview_000001",
                        "path": str(preview),
                        "sha256": sha256_file(preview),
                        "size_bytes": preview.stat().st_size,
                    }
                ],
                "details": {
                    "join_id": "join_000001",
                    "output_join_us": 2_000_000,
                },
            }
        ],
    }
    packet_path = layout.review / "qa-review-packet.json"
    write_validated_artifact(package_root, "qa_review_packet", packet_path, payload)
    return packet_path


def test_sample_join_frames_uses_explicit_boundary_rounding() -> None:
    samples = sample_join_frames(0, 4_000_000, 2_000_000, 240)

    assert [item["label"] for item in samples] == [
        "preview_start",
        "before_join",
        "at_join",
        "after_join",
        "preview_end",
    ]
    assert [item["frame_index"] for item in samples] == [0, 105, 120, 134, 239]
    assert samples[1]["offset_us"] == -250_000
    assert samples[3]["preview_time_us"] == 2_250_000


def test_sample_join_frames_does_not_fabricate_after_join_at_preview_end() -> None:
    samples = sample_join_frames(0, 4_000_000, 4_000_000, 240)

    assert [item["label"] for item in samples] == [
        "preview_start",
        "before_join",
        "at_join",
    ]
    assert len({item["frame_index"] for item in samples}) == len(samples)


def test_visual_evidence_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    packet_path = _packet(package_root, tmp_path)
    layout = ProjectLayout(packet_path.parents[1])
    adapter = _FakeReviewAdapter()

    output = write_qa_review_visual_evidence(
        package_root,
        layout,
        packet_path,
        adapter=adapter,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["contact_sheet_count"] == 1
    assert Path(payload["items"][0]["contact_sheet"]["path"]).is_file()
    assert adapter.contact_sheet_calls == 1
    second = write_qa_review_visual_evidence(
        package_root,
        layout,
        packet_path,
        adapter=adapter,
    )
    assert second == output
    assert adapter.contact_sheet_calls == 1


def test_visual_evidence_uses_current_project_and_rejects_stale_preview(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    packet_path = _packet(package_root, tmp_path)
    layout = ProjectLayout(packet_path.parents[1])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    adapter = _FakeReviewAdapter()

    output = write_qa_review_visual_evidence(
        package_root,
        layout,
        packet_path,
        adapter=adapter,
    )
    assert output.is_file()
    assert adapter.contact_sheet_calls == 1
    second = write_qa_review_visual_evidence(
        package_root,
        layout,
        packet_path,
        adapter=adapter,
    )
    assert second == output
    assert adapter.contact_sheet_calls == 1

    preview = Path(packet["items"][0]["evidence"][0]["path"])
    preview.write_bytes(b"changed preview")
    with pytest.raises(PlanningValidationError, match="hash is stale"):
        write_qa_review_visual_evidence(
            package_root,
            layout,
            packet_path,
            adapter=adapter,
        )
