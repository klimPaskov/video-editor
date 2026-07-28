from __future__ import annotations

from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import ProcessRequest
from videoedit.errors import RenderOutputError
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    write_validated_artifact,
)
from videoedit.services.media import ingest_and_probe
from videoedit.services.project import initialize_project
from videoedit.services.rendering import render_base_timeline
from videoedit.services.transcription import normalize_whisper_result


@pytest.mark.integration
def test_base_render_rejects_video_only_source_before_synthetic_audio(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "p4_video_only")
    adapter = FFmpegAdapter()
    with_audio = tmp_path / "with-audio.mp4"
    source = tmp_path / "video-only.mp4"
    adapter.generate_demo_source(with_audio, duration_seconds=2)
    strip_result = adapter.runner.run(
        ProcessRequest(
            executable=adapter.ffmpeg_path,
            arguments=(
                "-y",
                "-i",
                str(with_audio.resolve()),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                str(source.resolve()),
            ),
            working_directory=tmp_path,
            timeout_seconds=120,
        )
    )
    assert strip_result.exit_code == 0
    source_manifest = ingest_and_probe(package_root, layout, source, adapter=adapter)
    source_manifest_path = layout.artifacts / "source-manifest.json"
    duration_us = int(source_manifest["media_duration_us"])
    transcript = normalize_whisper_result(
        {
            "language": "en",
            "text": "fixture",
            "segments": [
                {
                    "start": 0,
                    "end": 1,
                    "text": "fixture",
                    "words": [{"word": "fixture", "start": 0, "end": 1, "probability": 1.0}],
                }
            ],
        },
        project_id=layout.root.name,
        revision_id="rev_001",
        duration_us=duration_us,
        model_name="fixture",
        source_input=artifact_input("art_source", source_manifest_path),
        config_hash=config_sha256(layout),
    )
    write_validated_artifact(
        package_root, "transcript", layout.artifacts / "transcript.json", transcript
    )
    edl = {
        "schema_name": "edit_decision_list",
        "schema_version": "1.0.0",
        "artifact_id": "art_edl",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": now_iso(),
        "producer": producer("edl-compile", "test"),
        "inputs": [artifact_input("art_source", source_manifest_path)],
        "config_sha256": config_sha256(layout),
        "source_duration_us": duration_us,
        "expected_output_duration_us": duration_us,
        "keep_ranges": [
            {
                "segment_id": "keep_000001",
                "source_start_us": 0,
                "source_end_us": duration_us,
                "output_start_us": 0,
                "output_end_us": duration_us,
            }
        ],
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": duration_us,
                "output_start_us": 0,
                "output_end_us": duration_us,
            }
        ],
        "deletions": [],
        "approval_record_ids": ["art_edit_review"],
        "policy_id": "pol_conservative",
        "policy_version": 1,
    }
    write_validated_artifact(
        package_root,
        "edit_decision_list",
        layout.artifacts / "edit-decision-list.json",
        edl,
    )

    with pytest.raises(RenderOutputError, match="production audio"):
        render_base_timeline(package_root, layout, adapter=adapter)
