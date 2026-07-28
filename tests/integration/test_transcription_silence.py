from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.transcription import FixtureTranscriptionAdapter
from videoedit.errors import TranscriptionOutputError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.media import ingest_and_probe
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.silence import detect_project_silence
from videoedit.services.stage_state import load_stage_state
from videoedit.services.transcription import transcribe_project


class FailingTranscriptionAdapter:
    adapter_id = "fixture-failing"
    adapter_version = "1.0.0"
    device = "cpu"

    def transcribe(self, _audio_path: Path, _model_name: str) -> object:
        raise TranscriptionOutputError("fixture transcription interruption")


@pytest.mark.integration
def test_fixture_transcription_and_silence_are_persisted_and_idempotent(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "p2_fixture_project")
    source = tmp_path / "recording.mp4"
    ffmpeg = FFmpegAdapter()
    ffmpeg.generate_edit_demo_source(source)
    ingest_and_probe(package_root, layout, source, adapter=ffmpeg)

    fixture_adapter = FixtureTranscriptionAdapter(
        {
            "language": "en",
            "text": "before after",
            "segments": [
                {
                    "start": 0.5,
                    "end": 1.2,
                    "text": "before",
                    "words": [{"word": "before", "start": 0.5, "end": 1.2, "probability": 1.0}],
                    "avg_logprob": 0.0,
                    "no_speech_prob": 0.0,
                },
                {
                    "start": 4.5,
                    "end": 5.2,
                    "text": "after",
                    "words": [{"word": "after", "start": 4.5, "end": 5.2, "probability": 1.0}],
                    "avg_logprob": 0.0,
                    "no_speech_prob": 0.0,
                },
            ],
        }
    )
    transcript_path = transcribe_project(
        package_root,
        layout,
        "fixture",
        adapter=fixture_adapter,
    )
    cached_transcript_path = transcribe_project(
        package_root,
        layout,
        "fixture",
        adapter=fixture_adapter,
    )
    assert cached_transcript_path == transcript_path
    assert (layout.review / "transcript.md").is_file()

    silence_path = detect_project_silence(
        package_root,
        layout,
        threshold_db=-45,
        minimum_duration_us=500_000,
        adapter=ffmpeg,
    )
    cached_silence_path = detect_project_silence(
        package_root,
        layout,
        threshold_db=-45,
        minimum_duration_us=500_000,
        adapter=ffmpeg,
    )
    assert cached_silence_path == silence_path

    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    silence = json.loads(silence_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "transcript", transcript)
    validate_artifact(package_root, "silence_intervals", silence)
    assert all(
        0 <= word["start_us"] < word["end_us"] <= transcript["source_duration_us"]
        for word in transcript["words"]
    )
    assert any(item["classification"] == "inter_word" for item in silence["intervals"])
    assert silence["detector_log"]["size_bytes"] > 0
    assert silence["detector_command"]["exit_code"] == 0
    assert silence["inputs"][1]["artifact_id"] == "art_proxy_speech"

    transcript_state = load_stage_state(package_root, layout, "transcribe", "rev_001")
    silence_state = load_stage_state(package_root, layout, "silence", "rev_001")
    assert transcript_state is not None and transcript_state["status"] == "complete"
    assert silence_state is not None and silence_state["status"] == "complete"
    assert transcript_state["attempt"] == 1
    assert silence_state["attempt"] == 1

    with pytest.raises(TranscriptionOutputError, match="interruption"):
        transcribe_project(
            package_root,
            layout,
            "broken",
            adapter=FailingTranscriptionAdapter(),  # type: ignore[arg-type]
        )
    failed_state = load_stage_state(package_root, layout, "transcribe", "rev_001")
    assert failed_state is not None
    assert failed_state["status"] == "failed"
    assert failed_state["error"]["code"] == "transcription_output_invalid"


@pytest.mark.integration
def test_transcription_persists_supplied_local_model_hash(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "p2_model_identity")
    source = tmp_path / "recording.mp4"
    model = tmp_path / "whisper-small.pt"
    ffmpeg = FFmpegAdapter()
    ffmpeg.generate_edit_demo_source(source)
    model.write_bytes(b"operator-supplied local model fixture")
    ingest_and_probe(package_root, layout, source, adapter=ffmpeg)

    transcript_path = transcribe_project(
        package_root,
        layout,
        "fixture",
        adapter=FixtureTranscriptionAdapter(
            {
                "language": "en",
                "text": "model identity",
                "segments": [
                    {
                        "start": 0.5,
                        "end": 1.2,
                        "text": "model identity",
                        "words": [
                            {"word": "model", "start": 0.5, "end": 0.8, "probability": 1.0},
                            {"word": "identity", "start": 0.85, "end": 1.2, "probability": 1.0},
                        ],
                    }
                ],
            },
            model_path=model,
        ),
    )
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert transcript["model_sha256"] == sha256_file(model)
    validate_artifact(package_root, "transcript", transcript)
