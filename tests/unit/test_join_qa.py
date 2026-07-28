from __future__ import annotations

import pytest

from videoedit.services.join_qa import (
    JoinQAPolicy,
    _audio_check,
    _rendered_transcript_text,
    _transcript_excerpt,
    _visual_check,
    compare_transcript_text,
    evaluate_join_qa,
)


def _join() -> dict[str, object]:
    return {
        "join_id": "join_001",
        "proposal_ids": ["prp_filler_001"],
        "source_cut_range": {"start_us": 2_000_000, "end_us": 2_200_000},
        "output_join_us": 2_000_000,
        "preview_range": {"start_us": 0, "end_us": 4_000_000},
        "join_strategy": "hard_cut_with_micro_audio_crossfade",
        "repair_order": [
            "hard_cut_with_micro_audio_crossfade",
            "adjusted_handles",
            "room_tone",
            "hard_cut",
        ],
        "fallback": "hard_cut",
        "handles": {"pre_handle_us": 80_000, "post_handle_us": 110_000},
        "reason": "Remove a clear filler word.",
        "status": "planned",
        "repair_action": None,
        "repair_attempt": 0,
        "review_required": False,
    }


def _audio_pass() -> dict[str, object]:
    return {
        "clipped_syllable": False,
        "click_or_pop": False,
        "room_tone_jump": False,
        "speech_rhythm": "natural",
        "status": "pass",
    }


def _visual_pass() -> dict[str, object]:
    return {
        "black_flash": False,
        "freeze": False,
        "duplicate_frame": False,
        "face_or_body_jump": "none",
        "screen_state_jump": "none",
        "status": "pass",
    }


def test_transcript_comparison_separates_duplicates_from_unexpected_words() -> None:
    comparison = compare_transcript_text(
        "This is the result",
        "This is is the result and",
    )

    assert comparison["missing_words"] == []
    assert comparison["duplicate_words"] == ["is"]
    assert comparison["unexpected_words"] == ["and"]
    assert comparison["meaning_status"] == "fail"


def test_join_excerpt_excludes_words_clipped_by_preview_boundary() -> None:
    transcript = {
        "words": [
            {"start_us": 100_000, "end_us": 400_000, "text": "before"},
            {"start_us": 900_000, "end_us": 1_100_000, "text": "clipped"},
            {"start_us": 1_200_000, "end_us": 1_500_000, "text": "after"},
        ]
    }

    assert _transcript_excerpt(transcript, 0, 1_000_000) == "before"


def test_join_excerpt_uses_disjoint_source_preview_ranges() -> None:
    transcript = {
        "words": [
            {"start_us": 100_000, "end_us": 200_000, "text": "before"},
            {"start_us": 300_000, "end_us": 400_000, "text": "removed"},
            {"start_us": 500_000, "end_us": 600_000, "text": "after"},
        ]
    }

    assert (
        _transcript_excerpt(
            transcript,
            0,
            700_000,
            source_ranges=[
                {"start_us": 0, "end_us": 250_000},
                {"start_us": 450_000, "end_us": 700_000},
            ],
        )
        == "before after"
    )


def test_passing_join_is_not_blocked_by_dense_cut_density() -> None:
    join = _join()
    joins = [
        {**join, "join_id": "join_001", "output_join_us": 2_000_000},
        {**join, "join_id": "join_002", "output_join_us": 3_000_000},
    ]

    result = evaluate_join_qa(
        join,
        joins,
        output_duration_us=10_000_000,
        approved_text="this is the result",
        rendered_text="THIS is the result.",
        audio_check=_audio_pass(),
        visual_check=_visual_pass(),
        policy=JoinQAPolicy(warning_cuts_per_minute=1.0),
    )

    assert result["status"] == "warning"
    assert result["review_required"] is True
    assert result["pacing_check"]["status"] == "warning"  # type: ignore[index]
    assert result["repair_action"] is None


def test_broken_transcript_routes_join_to_next_repair_strategy() -> None:
    result = evaluate_join_qa(
        _join(),
        [_join()],
        output_duration_us=10_000_000,
        approved_text="this is the result",
        rendered_text="this is is the result",
        audio_check=_audio_pass(),
        visual_check=_visual_pass(),
    )

    assert result["status"] == "fail"
    assert result["review_required"] is True
    assert result["transcript_check"]["duplicate_words"] == ["is"]  # type: ignore[index]
    assert result["repair_action"] == (
        "hard_cut_with_micro_audio_crossfade->adjusted_handles:"
        "duplicate_word,grammar_change,meaning_change,semantic_change"
    )


def test_unconfirmed_asr_mismatch_is_a_review_warning() -> None:
    result = evaluate_join_qa(
        _join(),
        [_join()],
        output_duration_us=10_000_000,
        approved_text="this is the result",
        rendered_text="this is a result",
        audio_check=_audio_pass(),
        visual_check=_visual_pass(),
    )

    assert result["status"] == "warning"
    assert result["review_required"] is True
    assert result["transcript_check"]["grammar_status"] == "warning"  # type: ignore[index]
    assert result["semantic_check"]["meaning_status"] == "warning"  # type: ignore[index]
    assert result["repair_action"] is None


def test_extreme_speech_rate_diagnostic_is_schema_bounded_and_still_fails() -> None:
    result = evaluate_join_qa(
        _join(),
        [_join()],
        output_duration_us=10_000_000,
        approved_text="one",
        rendered_text="one two three four five six seven",
        audio_check=_audio_pass(),
        visual_check=_visual_pass(),
    )

    assert result["status"] == "fail"
    pacing = result["pacing_check"]
    assert pacing["speech_rate_change_percent"] == 500.0  # type: ignore[index]
    assert pacing["status"] == "fail"  # type: ignore[index]


def test_automated_freeze_is_a_review_warning_until_operator_classifies_it() -> None:
    detected = _visual_check(
        {},
        black_flash=False,
        black_known=True,
        freeze=True,
        freeze_known=True,
        decode_ok=True,
    )
    assert detected["freeze"] is True
    assert detected["status"] == "warning"
    result = evaluate_join_qa(
        _join(),
        [_join()],
        output_duration_us=10_000_000,
        approved_text="this is the result",
        rendered_text="this is the result",
        audio_check=_audio_pass(),
        visual_check=detected,
    )
    assert result["status"] == "warning"
    assert result["visual_check"]["freeze"] is True  # type: ignore[index]


def test_preview_transcript_ignores_unapproved_boundary_words() -> None:
    rendered = _rendered_transcript_text(
        {
            "words": [
                {"text": "Hello", "start_us": 0, "end_us": 120_000},
                {"text": "and", "start_us": 220_000, "end_us": 420_000},
                {"text": "welcome", "start_us": 600_000, "end_us": 900_000},
                {"text": "a", "start_us": 3_850_000, "end_us": 4_000_000},
            ]
        },
        duration_us=4_000_000,
        approved_text="and welcome",
    )

    assert rendered == "and welcome"


def test_automated_clipping_is_a_warning_until_operator_classifies_it() -> None:
    check = _audio_check({}, clipping_known=True, clipped_samples=1, decode_ok=True)

    assert check["clipped_syllable"] is True
    assert check["status"] == "warning"


def test_join_audio_diagnostic_separates_context_from_boundary() -> None:
    check = _audio_check(
        {},
        clipping_known=True,
        clipped_samples=0,
        decode_ok=True,
        context_clipped_samples=4,
    )

    assert check["clipped_syllable"] is False
    assert check["context_clipped_samples"] == 4
    assert check["boundary_clipped_samples"] == 0
    assert check["boundary_clipping_known"] is True


def test_join_visual_diagnostic_separates_context_from_boundary() -> None:
    check = _visual_check(
        {
            "duplicate_frame": False,
            "face_or_body_jump": "none",
            "screen_state_jump": "none",
        },
        black_flash=False,
        black_known=True,
        freeze=False,
        freeze_known=True,
        decode_ok=True,
        context_black_flash=False,
        context_freeze=True,
    )

    assert check["freeze"] is False
    assert check["context_freeze"] is True
    assert check["status"] == "pass"


def test_join_policy_requires_a_positive_boundary_window() -> None:
    with pytest.raises(ValueError, match="boundary_check_window_us"):
        JoinQAPolicy(boundary_check_window_us=0)
