from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.errors import StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file
from videoedit.services.segment_preview import write_segment_preview_plan

ROOT = Path(__file__).resolve().parents[2]


class _FakePreviewAdapter:
    def __init__(self) -> None:
        self.render_calls: list[tuple[int, int]] = []
        self.rendered_durations: dict[Path, int] = {}

    def probe(self, path: Path) -> dict[str, object]:
        duration_us = self.rendered_durations.get(path.resolve(), 6_000_000)
        duration = f"{duration_us / 1_000_000:.6f}"
        return {
            "format": {"duration": duration},
            "streams": [
                {"codec_type": "video", "avg_frame_rate": "30/1", "duration": duration},
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
        self.render_calls.append((start_us, end_us))
        self.rendered_durations[output.resolve()] = end_us - start_us
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"preview:{start_us}:{end_us}".encode("ascii"))
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def full_decode_check(self, _path: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def version(self, _executable: str | None = None) -> str:
        return "fixture"


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path]:
    layout = initialize_project(tmp_path, "segment_preview_fixture")
    source = layout.work / "source.mp4"
    source.write_bytes(b"immutable-source")
    transcript = json.loads(
        (ROOT / "examples" / "transcript.example.json").read_text(encoding="utf-8")
    )
    transcript["project_id"] = layout.root.name
    transcript["source_duration_us"] = 6_000_000
    transcript["segments"][0].update(
        {"start_us": 500_000, "end_us": 1_200_000, "segment_id": "seg_fixture_001"}
    )
    transcript["segments"][1].update(
        {"start_us": 4_500_000, "end_us": 5_600_000, "segment_id": "seg_fixture_002"}
    )
    transcript["words"] = []
    transcript["segments"][0]["word_ids"] = []
    transcript["segments"][1]["word_ids"] = []
    transcript_path = layout.artifacts / "transcript.json"
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)
    return layout, source, transcript_path


def test_segment_preview_plan_derives_ranges_and_reuses_outputs(tmp_path: Path) -> None:
    layout, source, transcript = _fixture(tmp_path)
    source_sha = sha256_file(source)
    adapter = _FakePreviewAdapter()
    output = layout.artifacts / "segment-preview.json"

    first = write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript,
        output=output,
        adapter=adapter,  # type: ignore[arg-type]
    )
    first_bytes = first.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_preview", payload)
    assert len(payload["segments"]) == 2
    assert payload["segments"][0]["source_range"] == {"start_us": 0, "end_us": 2_200_000}
    assert payload["segments"][1]["source_range"] == {"start_us": 3_500_000, "end_us": 6_000_000}
    assert all(Path(item["preview_path"]).is_file() for item in payload["segments"])
    assert all(item["diagnostics"]["decode"] == "pass" for item in payload["segments"])
    assert adapter.render_calls == [(0, 2_200_000), (3_500_000, 6_000_000)]
    assert sha256_file(source) == source_sha

    second = write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript,
        output=output,
        adapter=adapter,  # type: ignore[arg-type]
    )
    assert second == first
    assert first.read_bytes() == first_bytes
    assert adapter.render_calls == [(0, 2_200_000), (3_500_000, 6_000_000)]


def test_segment_preview_rejects_changed_input_at_explicit_output(tmp_path: Path) -> None:
    layout, source, transcript = _fixture(tmp_path)
    output = layout.artifacts / "segment-preview.json"
    write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript,
        output=output,
        adapter=_FakePreviewAdapter(),  # type: ignore[arg-type]
    )
    source.write_bytes(b"changed-source")
    with pytest.raises(StateConflictError, match="different planning key"):
        write_segment_preview_plan(
            ROOT,
            layout,
            source,
            transcript,
            output=output,
            adapter=_FakePreviewAdapter(),  # type: ignore[arg-type]
        )


def test_segment_preview_uses_rebased_output_duration(tmp_path: Path) -> None:
    layout, source, transcript_path = _fixture(tmp_path)
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript.update(
        {
            "revision_id": "rev_002",
            "source_duration_us": 9_000_000,
            "output_duration_us": 6_000_000,
        }
    )
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)

    output = layout.revisions / "rev_002" / "segment-preview.json"
    plan_path = write_segment_preview_plan(
        ROOT,
        layout,
        source,
        transcript_path,
        output=output,
        revision_id="rev_002",
        adapter=_FakePreviewAdapter(),  # type: ignore[arg-type]
    )

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_preview", payload)
    assert payload["duration_us"] == 6_000_000
    assert payload["revision_id"] == "rev_002"
