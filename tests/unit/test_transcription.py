from __future__ import annotations

import errno
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import videoedit.adapters.transcription as transcription_adapter
from videoedit.adapters.transcription import (
    FixtureTranscriptionAdapter,
    WhisperAdapter,
    _load_whisper_model,
)
from videoedit.errors import DependencyUnavailableError, TranscriptionOutputError
from videoedit.services.transcription import (
    normalize_whisper_result,
    transcript_markdown,
    validate_transcript_timing,
)


def _result() -> dict[str, object]:
    return {
        "language": "en",
        "text": "before after",
        "segments": [
            {
                "start": 0.5,
                "end": 1.2,
                "text": "before",
                "speaker": "A",
                "words": [
                    {
                        "word": "before",
                        "start": 0.5,
                        "end": 1.2,
                        "probability": 0.99,
                        "speaker": "A",
                    }
                ],
                "avg_logprob": -0.1,
                "no_speech_prob": 0.01,
            },
            {
                "start": 4.5,
                "end": 5.2,
                "text": "after",
                "speaker": "B",
                "words": [
                    {
                        "word": "after",
                        "start": 4.5,
                        "end": 5.2,
                        "probability": 0.98,
                        "speaker": "B",
                    }
                ],
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
            },
        ],
    }


def test_fixture_adapter_is_deterministic_and_metadata_bound(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture audio")
    adapter = FixtureTranscriptionAdapter(_result())

    first = adapter.transcribe(audio, "fixture")
    second = adapter.transcribe(audio, "fixture")

    assert first == second
    assert first.model_identifier == "fixture-fixture"
    assert first.device == "cpu"
    assert first.adapter_version == "1.0.0"
    assert first.model_sha256 is None


def test_normalization_records_and_validates_model_hash() -> None:
    payload = normalize_whisper_result(
        _result(),
        "hash_project",
        "rev_001",
        6_000_000,
        "fixture",
        {"artifact_id": "art_source", "sha256": "a" * 64},
        "b" * 64,
        model_sha256="c" * 64,
    )
    assert payload["model_sha256"] == "c" * 64

    with pytest.raises(TranscriptionOutputError, match="lowercase SHA-256"):
        normalize_whisper_result(
            _result(),
            "hash_project",
            "rev_001",
            6_000_000,
            "fixture",
            {"artifact_id": "art_source", "sha256": "a" * 64},
            "b" * 64,
            model_sha256="C" * 64,
        )


def test_normalization_assigns_stable_ids_and_persists_confidence_metadata() -> None:
    payload = normalize_whisper_result(
        _result(),
        "transcript_project",
        "rev_001",
        6_000_000,
        "fixture",
        {"artifact_id": "art_source", "sha256": "a" * 64},
        "b" * 64,
        model_identifier="fixture-fixture",
        device="cpu",
        adapter_id="fixture-transcription",
        adapter_version="1.0.0",
    )

    validate_transcript_timing(payload)
    assert [word["word_id"] for word in payload["words"]] == ["wrd_000001", "wrd_000002"]
    assert payload["confidence_summary"]["word_count"] == 2
    assert payload["confidence_summary"]["speaker_count"] == 2
    assert "multiple_speakers_detected" in payload["warnings"]
    assert payload["raw_result"] == _result()
    assert payload["status"] == "warning"

    markdown = transcript_markdown(payload)
    assert "# Transcript" in markdown
    assert "00:00:00.500" in markdown
    assert "wrd_000001" in markdown


def test_normalization_surfaces_bad_bounds_overlap_and_low_confidence() -> None:
    payload = normalize_whisper_result(
        {
            "language": "en",
            "segments": [
                {
                    "start": -1,
                    "end": 4,
                    "words": [
                        {"word": "one", "start": -0.2, "end": 0.5, "probability": 0.2},
                        {"word": "two", "start": 0.4, "end": 9, "probability": 0.9},
                        {"word": "three", "start": 1.5, "end": 1.0, "probability": 0.9},
                    ],
                }
            ],
        },
        "timing_project",
        "rev_001",
        2_000_000,
        "fixture",
        {"artifact_id": "art_source", "sha256": "a" * 64},
        "b" * 64,
    )

    assert all(0 <= word["start_us"] < word["end_us"] <= 2_000_000 for word in payload["words"])
    assert "negative_start_time:seg_000001" in payload["warnings"]
    assert "out_of_bounds_time:wrd_000002" in payload["warnings"]
    assert "reversed_or_zero_duration:wrd_000003" in payload["warnings"]
    assert "overlapping_word_timing:wrd_000002" in payload["warnings"]
    assert "low_confidence_words:1" in payload["warnings"]
    assert payload["confidence_summary"]["low_confidence_word_ids"] == ["wrd_000001"]


def test_no_speech_is_a_warning_and_invalid_payload_is_rejected() -> None:
    payload = normalize_whisper_result(
        {"language": "en", "text": "", "segments": []},
        "silent_project",
        "rev_001",
        1_000_000,
        "fixture",
        {"artifact_id": "art_source", "sha256": "a" * 64},
        "b" * 64,
    )
    assert payload["words"] == []
    assert "no_speech_detected" in payload["warnings"]
    assert payload["status"] == "warning"

    malformed = dict(payload)
    malformed["words"] = [
        {
            "word_id": "wrd_000001",
            "segment_id": "seg_missing",
            "text": "bad",
            "start_us": 0,
            "end_us": 1,
            "probability": None,
            "timing_status": "uncertain",
        }
    ]
    with pytest.raises(TranscriptionOutputError, match="unknown segment"):
        validate_transcript_timing(malformed)


def test_noisy_audio_confidence_evidence_is_a_warning() -> None:
    payload = normalize_whisper_result(
        {
            "language": "en",
            "segments": [
                {
                    "start": 0.1,
                    "end": 0.9,
                    "text": "unclear",
                    "no_speech_prob": 0.85,
                    "words": [
                        {
                            "word": "unclear",
                            "start": 0.1,
                            "end": 0.9,
                            "probability": 0.25,
                        }
                    ],
                }
            ],
        },
        "noisy_project",
        "rev_001",
        1_000_000,
        "fixture",
        {"artifact_id": "art_source", "sha256": "a" * 64},
        "b" * 64,
    )

    assert "high_no_speech_probability:seg_000001" in payload["warnings"]
    assert "low_confidence_words:1" in payload["warnings"]
    assert payload["confidence_summary"]["low_confidence_word_ids"] == ["wrd_000001"]
    assert payload["status"] == "warning"


def test_whisper_adapter_does_not_download_or_use_credentials(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture audio")
    with pytest.raises(DependencyUnavailableError, match="network model downloads are disabled"):
        WhisperAdapter().transcribe(audio, "small")


def test_whisper_adapter_reuses_one_loaded_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture audio")
    model_path = tmp_path / "small.pt"
    model_path.write_bytes(b"fixture model")
    calls: list[tuple[str, str]] = []

    class FakeModel:
        device = "cpu"

        def transcribe(self, path: str, **_kwargs: object) -> dict[str, object]:
            assert Path(path) == audio
            return {"text": "ok", "segments": []}

    def fake_load_model(path: str, *, device: str) -> FakeModel:
        calls.append((path, device))
        return FakeModel()

    monkeypatch.setitem(
        sys.modules,
        "whisper",
        SimpleNamespace(load_model=fake_load_model, __version__="test"),
    )
    _load_whisper_model.cache_clear()
    adapter = WhisperAdapter(model_path=model_path)

    adapter.transcribe(audio, "small")
    adapter.transcribe(audio, "small")

    assert calls == [(str(model_path.resolve()), "cpu")]
    _load_whisper_model.cache_clear()


def test_whisper_adapter_retries_transient_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture audio")
    model_path = tmp_path / "small.pt"
    model_path.write_bytes(b"fixture model")
    calls = 0
    sleeps: list[float] = []

    class FlakyModel:
        device = "cpu"

        def transcribe(self, _path: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EINVAL, "transient invalid argument")
            return {"text": "ok", "segments": []}

    def fake_load_model(_path: str, *, device: str) -> FlakyModel:
        assert device == "cpu"
        return FlakyModel()

    monkeypatch.setitem(
        sys.modules,
        "whisper",
        SimpleNamespace(load_model=fake_load_model, __version__="test"),
    )
    monkeypatch.setattr(transcription_adapter.time, "sleep", sleeps.append)
    _load_whisper_model.cache_clear()
    try:
        result = WhisperAdapter(model_path=model_path).transcribe(audio, "small")
    finally:
        _load_whisper_model.cache_clear()

    assert result.raw_result == {"text": "ok", "segments": []}
    assert calls == 2
    assert sleeps == [transcription_adapter.WHISPER_TRANSIENT_DELAY_SECONDS]


def test_whisper_adapter_does_not_retry_non_transient_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture audio")
    model_path = tmp_path / "small.pt"
    model_path.write_bytes(b"fixture model")
    calls = 0

    class BrokenModel:
        device = "cpu"

        def transcribe(self, _path: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise OSError(errno.EIO, "permanent decoder failure")

    monkeypatch.setitem(
        sys.modules,
        "whisper",
        SimpleNamespace(load_model=lambda _path, device: BrokenModel(), __version__="test"),
    )
    _load_whisper_model.cache_clear()
    try:
        with pytest.raises(TranscriptionOutputError, match="permanent decoder failure"):
            WhisperAdapter(model_path=model_path).transcribe(audio, "small")
    finally:
        _load_whisper_model.cache_clear()

    assert calls == 1
