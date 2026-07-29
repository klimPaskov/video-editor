from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit import __version__
from videoedit.adapters.process import ProcessResult
from videoedit.errors import StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout, initialize_project
from videoedit.services.segment_preview import write_segment_preview_plan
from videoedit.services.segment_review_package import (
    _effect_summary,
    _review_artifacts,
    build_segment_review_packages,
)
from videoedit.services.segment_visual_qa import qa_visual_segment

ROOT = Path(__file__).resolve().parents[2]


class _ReviewAdapter:
    def __init__(self) -> None:
        self.rendered_durations: dict[Path, int] = {}
        self.contact_calls = 0

    def probe(self, path: Path) -> dict[str, object]:
        duration_us = self.rendered_durations.get(path.resolve(), 12_000_000)
        duration = f"{duration_us / 1_000_000:.6f}"
        return {
            "format": {"duration": duration},
            "streams": [
                {
                    "codec_type": "video",
                    "avg_frame_rate": "30/1",
                    "nb_frames": "360",
                    "duration": duration,
                },
                {"codec_type": "audio", "duration": duration},
            ],
        }

    def render_keep_ranges(
        self,
        source: Path,
        keep_ranges: list[tuple[int, int]],
        output: Path,
        *,
        crf: int,
        preset: str,
    ) -> ProcessResult:
        del source, crf, preset
        start_us, end_us = keep_ranges[0]
        self.rendered_durations[output.resolve()] = end_us - start_us
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"preview:{start_us}:{end_us}".encode("ascii"))
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def full_decode_check(self, _path: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def make_contact_sheet(
        self,
        source: Path,
        output: Path,
        frame_indices: list[int],
        *,
        scale_width: int,
        tile_columns: int,
    ) -> ProcessResult:
        del source, frame_indices, scale_width, tile_columns
        self.contact_calls += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"jpeg-fixture")
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def version(self, _executable: str | None = None) -> str:
        return "fixture"


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path]:
    layout = initialize_project(tmp_path, "segment_review_fixture")
    source = layout.work / "source.mp4"
    source.write_bytes(b"source")
    transcript = json.loads(
        (ROOT / "examples" / "transcript.example.json").read_text(encoding="utf-8")
    )
    transcript["project_id"] = layout.root.name
    transcript["source_duration_us"] = 12_000_000
    transcript_path = layout.artifacts / "transcript.json"
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)
    return layout, source, transcript_path


def test_review_package_contains_contact_sheet_excerpt_effects_and_diagnostics(
    tmp_path: Path,
) -> None:
    layout, source, transcript = _fixture(tmp_path)
    adapter = _ReviewAdapter()
    preview_plan = write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript,
        max_segment_duration_us=10_000_000,
        adapter=adapter,  # type: ignore[arg-type]
    )

    outputs = build_segment_review_packages(
        ROOT,
        layout,
        preview_plan,
        adapter=adapter,  # type: ignore[arg-type]
    )
    assert len(outputs) == 2
    assert adapter.contact_calls == 2
    for package_path in outputs:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        validate_artifact(ROOT, "segment_review_package", package)
        for schema_name, ref_key in (
            ("segment_transcript_excerpt", "transcript_excerpt"),
            ("segment_effect_summary", "effect_summary"),
            ("segment_diagnostics", "diagnostics"),
        ):
            child_path = Path(package[ref_key]["path"])
            child = json.loads(child_path.read_text(encoding="utf-8"))
            validate_artifact(ROOT, schema_name, child)
        assert Path(package["contact_sheet"]["path"]).is_file()
        assert Path(package["transcript_markdown"]["path"]).read_text(encoding="utf-8")
        effect_summary = json.loads(
            Path(package["effect_summary"]["path"]).read_text(encoding="utf-8")
        )
        assert "effect_plan_not_present" in effect_summary["warnings"]
        diagnostics = json.loads(Path(package["diagnostics"]["path"]).read_text(encoding="utf-8"))
        assert "no_mask_or_matte_diagnostics" in diagnostics["warnings"]

    rerun = build_segment_review_packages(
        ROOT,
        layout,
        preview_plan,
        adapter=adapter,  # type: ignore[arg-type]
    )
    assert rerun == outputs
    assert adapter.contact_calls == 2


def test_visual_qa_cache_rejects_report_from_an_older_implementation(tmp_path: Path) -> None:
    layout, source, transcript = _fixture(tmp_path)
    adapter = _ReviewAdapter()
    preview_plan = write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript,
        max_segment_duration_us=10_000_000,
        adapter=adapter,  # type: ignore[arg-type]
    )
    package_path = build_segment_review_packages(
        ROOT,
        layout,
        preview_plan,
        adapter=adapter,  # type: ignore[arg-type]
    )[0]

    report_path = qa_visual_segment(ROOT, layout, package_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_visual_qa_report", report)
    assert report["producer"]["adapter_version"] == f"{__version__}:p10-07c"

    report["producer"]["adapter_version"] = __version__
    write_validated_artifact(ROOT, "segment_visual_qa_report", report_path, report)
    with pytest.raises(StateConflictError, match="stale"):
        qa_visual_segment(ROOT, layout, package_path)


def test_revision_review_skips_stale_default_artifacts(tmp_path: Path) -> None:
    layout, _source, _transcript = _fixture(tmp_path)
    stale = layout.artifacts / "focus-pacing-plan.json"
    stale.write_text(
        json.dumps(
            {
                "schema_name": "focus_pacing_plan",
                "project_id": layout.root.name,
                "revision_id": "rev_001",
            }
        ),
        encoding="utf-8",
    )

    assert _review_artifacts(layout, None, revision_id="rev_002") == []


def test_effect_summary_clips_overlapping_effect_to_review_segment(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "segment_effect_fixture")
    focus_plan = json.loads(
        (ROOT / "examples" / "focus_pacing_plan.example.json").read_text(encoding="utf-8")
    )
    focus_plan["project_id"] = layout.root.name
    focus_plan["revision_id"] = "rev_001"
    focus_path = layout.artifacts / "focus-pacing-plan.json"
    write_validated_artifact(ROOT, "focus_pacing_plan", focus_path, focus_plan)

    summary = _effect_summary(
        ROOT,
        layout,
        {
            "segment_id": "segment_000001",
            "source_range": {"start_us": 10_000_000, "end_us": 12_000_000},
        },
        "planning-key",
        [focus_path],
        "rev_001",
    )

    zoom = next(item for item in summary["effects"] if item["effect_id"] == "zoom_prompt_box")
    assert zoom["source_range"] == {"start_us": 10_000_000, "end_us": 12_000_000}
    assert zoom["overlap_us"] == 2_000_000
