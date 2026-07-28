from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.adapters.transcription import FixtureTranscriptionAdapter, TranscriptionResult
from videoedit.errors import StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.segment_transcription import (
    build_segment_transcript_comparison,
    compare_transcripts,
    retranscribe_revision,
)

ROOT = Path(__file__).resolve().parents[2]


class _FakeSpeechAdapter:
    def __init__(self) -> None:
        self.proxy_calls = 0

    def create_speech_proxy(self, _source: Path, output: Path) -> ProcessResult:
        self.proxy_calls += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"speech-fixture")
        return ProcessResult(("ffmpeg",), 0, "", "", 1)


class _HashingFixtureTranscriber:
    adapter_id = "fixture-transcription"
    adapter_version = "1.0.0"
    device = "cpu"

    def __init__(self, fixture_result: dict[str, object], model_sha256: str) -> None:
        self.fixture_result = fixture_result
        self.model_sha256 = model_sha256

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult:
        result = FixtureTranscriptionAdapter(self.fixture_result).transcribe(audio_path, model_name)
        return TranscriptionResult(
            raw_result=result.raw_result,
            model_identifier=result.model_identifier,
            device=result.device,
            adapter_id=result.adapter_id,
            adapter_version=result.adapter_version,
            model_sha256=self.model_sha256,
        )


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path, Path]:
    layout = initialize_project(tmp_path, "segment_transcription_fixture")
    source = layout.work / "source.mp4"
    output = layout.revision_root("rev_002") / "outputs" / "recut.mp4"
    source.write_bytes(b"source")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"output")
    marker_path = layout.artifacts / "markers.json"
    marker_path.write_bytes(b"markers")
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
            "path": str(marker_path),
            "sha256": sha256_file(marker_path),
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
        "removed_ranges": [{"start_us": 4_800_000, "end_us": 5_000_000}],
        "keep_ranges": [
            {"start_us": 0, "end_us": 4_800_000},
            {"start_us": 5_000_000, "end_us": 6_000_000},
        ],
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": 4_800_000,
                "output_start_us": 0,
                "output_end_us": 4_800_000,
            },
            {
                "source_start_us": 5_000_000,
                "source_end_us": 6_000_000,
                "output_start_us": 4_800_000,
                "output_end_us": 5_800_000,
            },
        ],
        "warnings": [],
        "status": "complete",
    }
    write_validated_artifact(ROOT, "revision_media_manifest", manifest_path, manifest)
    transcript = json.loads(
        (ROOT / "tests" / "fixtures" / "segment-transcript.json").read_text(encoding="utf-8")
    )
    transcript["project_id"] = layout.root.name
    transcript_path = layout.work / "intended-transcript.json"
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)
    return manifest_path, transcript_path, layout.root


def test_compare_transcripts_reports_duplicate_phrase() -> None:
    intended = {
        "words": [
            {"word_id": "wrd_before", "text": "before", "start_us": 0, "end_us": 100},
            {"word_id": "wrd_go", "text": "go", "start_us": 200, "end_us": 300},
        ]
    }
    rendered = {
        "words": [
            {"word_id": "wrd_1", "text": "before", "start_us": 0, "end_us": 100},
            {"word_id": "wrd_2", "text": "go", "start_us": 200, "end_us": 300},
            {"word_id": "wrd_3", "text": "go", "start_us": 350, "end_us": 450},
        ]
    }
    media = {
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": 1_000,
                "output_start_us": 0,
                "output_end_us": 1_000,
            }
        ],
        "removed_ranges": [],
    }
    result = compare_transcripts(intended, rendered, media)
    assert result[3] == ["before", "go", "go"]
    assert result[6] == ["go"]
    assert result[7] is False


def test_build_segment_transcript_comparison_slices_absolute_output_clock() -> None:
    global_comparison = {
        "intended_transcript": {
            "artifact_id": "intended_transcript",
            "path": "C:/project/intended.json",
            "sha256": "1" * 64,
        },
        "rendered_transcript": {
            "artifact_id": "rendered_transcript",
            "path": "C:/project/rendered.json",
            "sha256": "2" * 64,
        },
        "revision_media": {
            "artifact_id": "revision_media",
            "path": "C:/project/media.json",
            "sha256": "3" * 64,
        },
        "expected_words": [
            {
                "source_word_id": "wrd_before",
                "text": "before",
                "source_start_us": 900,
                "source_end_us": 1_100,
                "output_start_us": 900,
                "output_end_us": 1_100,
            },
            {
                "source_word_id": "wrd_after",
                "text": "after",
                "source_start_us": 2_000,
                "source_end_us": 2_200,
                "output_start_us": 2_000,
                "output_end_us": 2_200,
            },
        ],
        "rendered_words": [
            {"word_id": "wrd_rendered_before", "text": "before", "start_us": 900, "end_us": 1_100},
            {"word_id": "wrd_rendered_after", "text": "after", "start_us": 2_000, "end_us": 2_200},
        ],
    }
    result = build_segment_transcript_comparison(
        global_comparison,
        project_id="project",
        revision_id="rev_002",
        segment_id="segment_000001",
        start_us=1_000,
        end_us=2_100,
        producer_value={
            "application_version": "0.2.0",
            "stage": "segment-transcript-slice",
            "adapter": "revision-whisper-comparison",
            "adapter_version": "p10-05d",
        },
    )
    assert result["scope"] == {
        "segment_id": "segment_000001",
        "source_range": {"start_us": 1_000, "end_us": 2_100},
    }
    assert result["expected_sequence"] == ["before", "after"]
    assert result["rendered_sequence"] == ["before", "after"]
    assert result["sequence_status"] == "pass"
    assert result["status"] == "complete"
    assert result["warnings"] == []


def test_retranscribe_revision_compares_rebased_words_and_reuses_outputs(tmp_path: Path) -> None:
    manifest_path, transcript_path, root = _fixture_manifest(tmp_path)
    layout = initialize_project(tmp_path, "segment_transcription_fixture")
    adapter = _FakeSpeechAdapter()
    transcriber = _HashingFixtureTranscriber(
        {
            "language": "en",
            "text": "before go buy",
            "segments": [
                {
                    "start": 0.5,
                    "end": 1.2,
                    "text": "before",
                    "words": [{"word": "before", "start": 0.5, "end": 1.2}],
                },
                {
                    "start": 4.5,
                    "end": 5.2,
                    "text": "go buy",
                    "words": [
                        {"word": "go", "start": 4.5, "end": 4.7},
                        {"word": "buy", "start": 4.9, "end": 5.2},
                    ],
                },
            ],
        },
        model_sha256="d" * 64,
    )
    comparison_path = retranscribe_revision(
        ROOT,
        layout,
        manifest_path,
        transcript_path,
        model_name="fixture",
        adapter=adapter,  # type: ignore[arg-type]
        transcriber=transcriber,
    )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_transcript_comparison", comparison)
    assert comparison["expected_sequence"] == ["before", "go", "buy"]
    assert comparison["rendered_sequence"] == ["before", "go", "buy"]
    assert comparison["sequence_status"] == "pass"
    assert adapter.proxy_calls == 1

    rendered_path = root / "revisions" / "rev_002" / "rendered-transcript.json"
    assert rendered_path.is_file()
    rendered = json.loads(rendered_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "transcript", rendered)
    assert rendered["model_sha256"] == "d" * 64

    second = retranscribe_revision(
        ROOT,
        layout,
        manifest_path,
        transcript_path,
        model_name="fixture",
        adapter=adapter,  # type: ignore[arg-type]
        transcriber=transcriber,
    )
    assert second == comparison_path
    assert adapter.proxy_calls == 1

    changed_model = _HashingFixtureTranscriber(
        {
            "language": "en",
            "text": "before go buy",
            "segments": [],
        },
        model_sha256="e" * 64,
    )
    with pytest.raises(StateConflictError, match="stale"):
        retranscribe_revision(
            ROOT,
            layout,
            manifest_path,
            transcript_path,
            model_name="fixture",
            adapter=adapter,  # type: ignore[arg-type]
            transcriber=changed_model,
        )
    assert adapter.proxy_calls == 1

    with pytest.raises(StateConflictError, match="stale"):
        retranscribe_revision(
            ROOT,
            layout,
            manifest_path,
            transcript_path,
            model_name="different-model",
            adapter=adapter,  # type: ignore[arg-type]
            transcriber=transcriber,
        )
    assert adapter.proxy_calls == 1


def test_retranscribe_revision_accepts_parent_output_clock_transcript(tmp_path: Path) -> None:
    manifest_path, transcript_path, _root = _fixture_manifest(tmp_path)
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript["source_duration_us"] = 9_000_000
    transcript["output_duration_us"] = 6_000_000
    transcript["source_to_output_mapping"] = [
        {
            "source_start_us": 0,
            "source_end_us": 9_000_000,
            "output_start_us": 0,
            "output_end_us": 6_000_000,
        }
    ]
    write_validated_artifact(ROOT, "transcript", transcript_path, transcript)
    comparison_path = retranscribe_revision(
        ROOT,
        initialize_project(tmp_path, "segment_transcription_fixture"),
        manifest_path,
        transcript_path,
        model_name="fixture",
        adapter=_FakeSpeechAdapter(),  # type: ignore[arg-type]
        transcriber=_HashingFixtureTranscriber(
            {
                "language": "en",
                "text": "before go buy",
                "segments": [
                    {
                        "start": 0.5,
                        "end": 1.2,
                        "text": "before",
                        "words": [{"word": "before", "start": 0.5, "end": 1.2}],
                    },
                    {
                        "start": 4.5,
                        "end": 5.2,
                        "text": "go buy",
                        "words": [
                            {"word": "go", "start": 4.5, "end": 4.7},
                            {"word": "buy", "start": 4.9, "end": 5.2},
                        ],
                    },
                ],
            },
            model_sha256="d" * 64,
        ),
    )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert comparison["expected_sequence"] == ["before", "go", "buy"]
    assert comparison["sequence_status"] == "pass"
