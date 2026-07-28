from __future__ import annotations

import json
from pathlib import Path
from shutil import copyfile

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.focus_pacing import classify_speedup_candidate
from videoedit.services.media import seconds_to_us
from videoedit.services.project import initialize_project
from videoedit.services.retiming import (
    compile_retimed_timeline,
    render_retimed_timeline,
    validate_retimed_timeline,
    write_retimed_timeline,
)


@pytest.mark.integration
def test_retimed_fixture_preserves_picture_audio_duration_and_decodes(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    adapter = FFmpegAdapter()
    source = tmp_path / "source.mp4"
    output = tmp_path / "retimed.mp4"
    adapter.generate_edit_demo_source(source)
    speedup = classify_speedup_candidate(
        {
            "speedup_id": "speed_fixture",
            "action_type": "prompt_writing",
            "source_range": {"start_us": 2_000_000, "end_us": 4_000_000},
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
            "playback_rate": 2.0,
            "audio_mode": "audible_pitch_preserved",
            "audio_exception_explicit": False,
            "forbidden_content_check": {},
            "start_evidence_frame": "start.png",
            "end_evidence_frame": "end.png",
            "action_visibility_confidence": 0.99,
            "boundary_confidence": 0.99,
            "overall_confidence": 0.99,
            "reason": "Fixture action is visible and bounded.",
        },
        operator_request={
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
    )
    timeline_payload = compile_retimed_timeline(
        package_root=package_root,
        project_id="p21_media",
        revision_id="rev_001",
        source_duration_us=6_000_000,
        keep_ranges=[(0, 6_000_000)],
        speedups=[speedup],
        edit_decision_list_sha256="a" * 64,
        focus_pacing_plan_sha256="b" * 64,
        config_hash="c" * 64,
    )
    timeline = validate_retimed_timeline(package_root, timeline_payload)
    adapter.render_retimed_segments(
        source,
        [item.model_dump(mode="json") for item in timeline.segments],
        output,
    )
    assert output.is_file()
    probe = adapter.probe(output)
    video = next(item for item in probe["streams"] if item["codec_type"] == "video")
    audio = next(item for item in probe["streams"] if item["codec_type"] == "audio")
    video_duration = seconds_to_us(video.get("duration"))
    audio_duration = seconds_to_us(audio.get("duration"))
    container_duration = seconds_to_us(probe["format"].get("duration"))
    assert video_duration is not None
    assert audio_duration is not None
    assert container_duration is not None
    assert abs(video_duration - timeline.output_duration_us) <= 100_000
    assert abs(audio_duration - timeline.output_duration_us) <= 100_000
    assert abs(video_duration - audio_duration) <= 100_000
    assert adapter.full_decode_check(output).exit_code == 0

    layout = initialize_project(tmp_path, "p21_media")
    project_source = layout.raw / "source.mp4"
    copyfile(source, project_source)
    timeline_path = write_retimed_timeline(package_root, layout, timeline_payload)
    manifest_path = render_retimed_timeline(
        package_root,
        layout,
        project_source,
        timeline_path,
        adapter=adapter,
    )
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["validation"] == {
        "full_decode": "pass",
        "duration": "pass",
        "av_sync": "pass",
    }
    assert Path(manifest["output"]["path"]).is_file()
