from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.errors import StateConflictError
from videoedit.services.artifacts import config_sha256, validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.segment_qa import qa_segment_revision

ROOT = Path(__file__).resolve().parents[2]


class _FakeQAAAdapter:
    def __init__(self, *, silence_stderr: str = "", adapter_version: str = "fixture") -> None:
        self.call_count = 0
        self.silence_stderr = silence_stderr
        self.adapter_version = adapter_version

    def version(self, _executable: str | None = None) -> str:
        return self.adapter_version

    def probe(self, _path: Path) -> dict[str, object]:
        return {
            "format": {"duration": "5.800000"},
            "streams": [
                {"codec_type": "video", "duration": "5.800000"},
                {"codec_type": "audio", "duration": "5.800000"},
            ],
        }

    def full_decode_check(self, _path: Path) -> ProcessResult:
        self.call_count += 1
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def detect_black_frames(self, _path: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def detect_freeze_frames(self, _path: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def measure_clipping(self, _path: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "Number of clipped samples: 0", 1)

    def detect_silence_result(
        self, _path: Path, *, threshold_db: float, minimum_duration_us: int
    ) -> ProcessResult:
        del threshold_db, minimum_duration_us
        return ProcessResult(("ffmpeg",), 0, "", self.silence_stderr, 1)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    layout = initialize_project(tmp_path, "segment_qa_fixture")
    source = layout.work / "source.mp4"
    output = layout.revision_root("rev_002") / "outputs" / "recut.mp4"
    marker = layout.artifacts / "markers.json"
    source.write_bytes(b"source")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"output")
    marker.write_bytes(b"markers")
    manifest_path = layout.revision_root("rev_002") / "revision-media.json"
    manifest = {
        "schema_name": "revision_media_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_revision_media_rev_002",
        "project_id": layout.root.name,
        "revision_id": "rev_002",
        "parent_revision_id": "rev_001",
        "created_at": "2026-07-24T12:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "revision-recut",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "source_markers": {
            "artifact_id": "markers_artifact",
            "path": str(marker),
            "sha256": sha256_file(marker),
        },
        "source": {
            "artifact_id": "source_media",
            "path": str(source),
            "sha256": sha256_file(source),
        },
        "output": {
            "artifact_id": "revision_recut_media",
            "path": str(output),
            "sha256": sha256_file(output),
        },
        "source_duration_us": 6_000_000,
        "output_duration_us": 5_800_000,
        "removed_ranges": [],
        "keep_ranges": [{"start_us": 0, "end_us": 6_000_000}],
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": 6_000_000,
                "output_start_us": 0,
                "output_end_us": 5_800_000,
            }
        ],
        "warnings": [],
        "status": "complete",
    }
    write_validated_artifact(ROOT, "revision_media_manifest", manifest_path, manifest)
    comparison = json.loads(
        (ROOT / "examples" / "segment_transcript_comparison.example.json").read_text(
            encoding="utf-8"
        )
    )
    comparison["project_id"] = layout.root.name
    comparison["revision_id"] = "rev_002"
    comparison_path = layout.revision_root("rev_002") / "transcript-comparison.json"
    write_validated_artifact(ROOT, "segment_transcript_comparison", comparison_path, comparison)
    return manifest_path, comparison_path, layout.root


def test_segment_qa_collects_required_checks_and_reuses_report(tmp_path: Path) -> None:
    manifest_path, comparison_path, root = _fixture(tmp_path)
    layout = initialize_project(tmp_path, "segment_qa_fixture")
    join_report = json.loads(
        (ROOT / "examples" / "join_qa_report.example.json").read_text(encoding="utf-8")
    )
    join_report.update(
        {
            "project_id": layout.root.name,
            "revision_id": "rev_002",
            "config_sha256": config_sha256(layout),
        }
    )
    join_report_path = layout.revision_root("rev_002") / "join-qa-report.json"
    write_validated_artifact(ROOT, "join_qa_report", join_report_path, join_report)
    adapter = _FakeQAAAdapter()
    report_path = qa_segment_revision(
        ROOT,
        layout,
        manifest_path,
        comparison_path=comparison_path,
        join_report_path=join_report_path,
        adapter=adapter,  # type: ignore[arg-type]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_qa_report", report)
    assert report["final_ready"] is True
    assert report["overall_status"] == "pass"
    assert {item["check_code"] for item in report["findings"]} >= {
        "MEDIA_DECODE",
        "AV_SYNC",
        "BLACK_FRAMES",
        "FREEZE_FRAMES",
        "CLIPPING",
        "DEAD_AIR",
        "TRANSCRIPT_SEQUENCE",
        "DUPLICATE_PHRASES",
        "JOIN_BOUNDARIES",
    }
    assert adapter.call_count == 1

    second = qa_segment_revision(
        ROOT,
        layout,
        manifest_path,
        comparison_path=comparison_path,
        join_report_path=join_report_path,
        adapter=adapter,  # type: ignore[arg-type]
    )
    assert second == report_path
    assert adapter.call_count == 1
    assert root.joinpath("revisions", "rev_002", "segment-qa.json").is_file()

    changed_adapter = _FakeQAAAdapter(adapter_version="fixture-v2")
    with pytest.raises(StateConflictError, match="stale"):
        qa_segment_revision(
            ROOT,
            layout,
            manifest_path,
            comparison_path=comparison_path,
            join_report_path=join_report_path,
            adapter=changed_adapter,  # type: ignore[arg-type]
        )
    assert changed_adapter.call_count == 0

    join_report["overall_status"] = "fail"
    write_validated_artifact(ROOT, "join_qa_report", join_report_path, join_report)
    with pytest.raises(StateConflictError, match="stale"):
        qa_segment_revision(
            ROOT,
            layout,
            manifest_path,
            comparison_path=comparison_path,
            join_report_path=join_report_path,
            adapter=adapter,  # type: ignore[arg-type]
        )
    assert adapter.call_count == 1


def test_segment_qa_exposes_dead_air_interval_and_repair_route(tmp_path: Path) -> None:
    manifest_path, comparison_path, root = _fixture(tmp_path)
    layout = initialize_project(tmp_path, "segment_qa_fixture")
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    words = [
        {
            "source_word_id": "wrd_000001",
            "text": "before",
            "source_start_us": 500_000,
            "source_end_us": 1_200_000,
            "output_start_us": 500_000,
            "output_end_us": 1_200_000,
        },
        {
            "source_word_id": "wrd_000002",
            "text": "after",
            "source_start_us": 4_500_000,
            "source_end_us": 5_200_000,
            "output_start_us": 4_500_000,
            "output_end_us": 5_200_000,
        },
    ]
    comparison.update(
        {
            "expected_words": words,
            "rendered_words": [
                {
                    "word_id": "wrd_000001",
                    "text": "before",
                    "start_us": 500_000,
                    "end_us": 1_200_000,
                },
                {
                    "word_id": "wrd_000002",
                    "text": "after",
                    "start_us": 4_500_000,
                    "end_us": 5_200_000,
                },
            ],
            "expected_sequence": ["before", "after"],
            "rendered_sequence": ["before", "after"],
            "missing_words": [],
            "unexpected_words": [],
            "duplicate_words": [],
            "ordering_match": True,
            "sequence_status": "pass",
            "warnings": [],
            "status": "complete",
        }
    )
    write_validated_artifact(ROOT, "segment_transcript_comparison", comparison_path, comparison)
    adapter = _FakeQAAAdapter(
        silence_stderr=(
            "[Parsed_silencedetect_0] silence_start: 1.500000\n"
            "[Parsed_silencedetect_0] silence_end: 3.000000 | silence_duration: 1.500000\n"
        )
    )

    report_path = qa_segment_revision(
        ROOT,
        layout,
        manifest_path,
        comparison_path=comparison_path,
        adapter=adapter,  # type: ignore[arg-type]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_qa_report", report)
    dead_air = next(item for item in report["findings"] if item["check_code"] == "DEAD_AIR")
    assert dead_air["status"] == "warning"
    assert dead_air["time_range"] == {"start_us": 1_500_000, "end_us": 3_000_000}
    assert dead_air["evidence"]["interior_intervals"] == [
        {"start_us": 1_500_000, "end_us": 3_000_000}
    ]
    assert "bounded fix marker" in dead_air["repair_hint"]
    assert report["final_ready"] is False
    assert root.joinpath("revisions", "rev_002", "segment-qa.json").is_file()
