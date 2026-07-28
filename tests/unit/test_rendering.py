from __future__ import annotations

from pathlib import Path

from videoedit.adapters.process import ProcessResult
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.services.rendering import (
    _expected_frame_count,
    parse_clipped_samples,
    parse_loudness_measurement,
    rebase_transcript,
)


def test_parse_loudness_and_clipping_diagnostics() -> None:
    summary = """
    Integrated loudness:
        I:         -18.4 LUFS
        Threshold: -28.5 LUFS
    Loudness range:
        LRA:         4.2 LU
    True peak:
        Peak:      -2.1 dBFS
    """
    measurement = parse_loudness_measurement(summary)
    assert measurement == {
        "integrated_lufs": -18.4,
        "threshold_lufs": -28.5,
        "loudness_range_lu": 4.2,
        "true_peak_dbfs": -2.1,
    }
    clipped = ProcessResult(
        arguments=("ffmpeg",),
        exit_code=0,
        stdout="",
        stderr="Number of clipped samples: 7",
        elapsed_ms=1,
    )
    assert parse_clipped_samples(clipped) == 7


def test_expected_frame_count_uses_rational_boundaries() -> None:
    mapping = [
        {
            "source_start_us": 0,
            "source_end_us": 100_000,
            "output_start_us": 0,
            "output_end_us": 100_000,
        },
        {
            "source_start_us": 900_000,
            "source_end_us": 1_000_000,
            "output_start_us": 100_000,
            "output_end_us": 200_000,
        },
    ]
    assert _expected_frame_count(mapping, {"numerator": 30_000, "denominator": 1_001}) == (
        microseconds_to_frame(100_000, 30_000, 1_001)
        - microseconds_to_frame(0, 30_000, 1_001)
        + microseconds_to_frame(1_000_000, 30_000, 1_001)
        - microseconds_to_frame(900_000, 30_000, 1_001)
    )


def test_rebase_transcript_preserves_word_ids_at_first_and_final_boundaries(
    tmp_path: Path,
) -> None:
    transcript = {
        "source_duration_us": 1_000_000,
        "language": "en",
        "model": "whisper-fixture",
        "model_identifier": "fixture",
        "device": "cpu",
        "words": [
            {
                "word_id": "wrd_000001",
                "segment_id": "seg_000001",
                "text": "first",
                "start_us": 0,
                "end_us": 100_000,
                "probability": 1.0,
                "timing_status": "original",
            },
            {
                "word_id": "wrd_000002",
                "segment_id": "seg_000002",
                "text": "deleted",
                "start_us": 400_000,
                "end_us": 500_000,
                "probability": 1.0,
                "timing_status": "original",
            },
            {
                "word_id": "wrd_000003",
                "segment_id": "seg_000003",
                "text": "last",
                "start_us": 900_000,
                "end_us": 1_000_000,
                "probability": 1.0,
                "timing_status": "original",
            },
        ],
        "segments": [
            {
                "segment_id": "seg_000001",
                "text": "first",
                "start_us": 0,
                "end_us": 100_000,
                "word_ids": ["wrd_000001"],
                "average_log_probability": None,
                "no_speech_probability": None,
            },
            {
                "segment_id": "seg_000002",
                "text": "deleted",
                "start_us": 400_000,
                "end_us": 500_000,
                "word_ids": ["wrd_000002"],
                "average_log_probability": None,
                "no_speech_probability": None,
            },
            {
                "segment_id": "seg_000003",
                "text": "last",
                "start_us": 900_000,
                "end_us": 1_000_000,
                "word_ids": ["wrd_000003"],
                "average_log_probability": None,
                "no_speech_probability": None,
            },
        ],
        "warnings": [],
        "confidence_summary": {
            "word_count": 3,
            "mean_word_probability": 1.0,
            "minimum_word_probability": 1.0,
            "low_confidence_word_ids": [],
            "uncertain_word_count": 0,
            "speaker_count": 1,
        },
    }
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text("{}", encoding="utf-8")
    edl_path = tmp_path / "edit-decision-list.json"
    edl_path.write_text("{}", encoding="utf-8")
    mapping = [
        {
            "source_start_us": 0,
            "source_end_us": 300_000,
            "output_start_us": 0,
            "output_end_us": 300_000,
        },
        {
            "source_start_us": 700_000,
            "source_end_us": 1_000_000,
            "output_start_us": 300_000,
            "output_end_us": 600_000,
        },
    ]

    output = rebase_transcript(
        transcript,
        "a" * 64,
        edl_path,
        mapping,
        600_000,
        project_id="render_test",
        revision_id="rev_001",
        source_transcript_path=transcript_path,
    )

    assert [word["word_id"] for word in output["words"]] == ["wrd_000001", "wrd_000003"]
    assert output["words"][0]["start_us"] == 0
    assert output["words"][0]["source_start_us"] == 0
    assert output["words"][1]["start_us"] == 500_000
    assert output["words"][1]["end_us"] == 600_000
    assert "dropped_word_outside_keep_ranges:wrd_000002" in output["warnings"]
