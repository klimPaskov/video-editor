from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.planning import (
    EditingPolicy,
    build_edit_proposals,
    compile_smart_dense_policy,
)
from videoedit.services.project import ProjectLayout


def test_smart_dense_scans_all_transcript_fillers_and_silence_without_a_cut_cap(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = ProjectLayout(tmp_path)
    source_manifest_path = tmp_path / "source-manifest.json"
    transcript_path = tmp_path / "transcript.json"
    silence_path = tmp_path / "silence.json"
    source_manifest_path.write_text("source manifest", encoding="utf-8")
    transcript_path.write_text("transcript", encoding="utf-8")
    silence_path.write_text("silence", encoding="utf-8")

    words: list[dict[str, object]] = []
    tokens = [
        ("hello", 500_000, 800_000),
        ("um", 850_000, 1_050_000),
        ("this", 1_100_000, 1_400_000),
        ("uh", 1_450_000, 1_650_000),
        ("works", 1_700_000, 2_000_000),
        ("er", 2_050_000, 2_250_000),
        ("and", 2_300_000, 2_600_000),
        ("erm", 2_650_000, 2_850_000),
        ("it", 2_900_000, 3_100_000),
        ("is", 3_150_000, 3_350_000),
        ("clear", 3_400_000, 3_750_000),
    ]
    for index, (text, start_us, end_us) in enumerate(tokens, start=1):
        words.append(
            {
                "word_id": f"wrd_{index:06d}",
                "text": text,
                "start_us": start_us,
                "end_us": end_us,
                "probability": 0.99,
                "timing_status": "certain",
            }
        )
    transcript = {
        "source_duration_us": 12_000_000,
        "words": words,
        "confidence_summary": {"speaker_count": 1},
    }
    silence = {
        "source_duration_us": 12_000_000,
        "intervals": [
            {
                "interval_id": "sil_000001",
                "start_us": 4_000_000,
                "end_us": 4_600_000,
                "classification": "inter_word",
                "nearest_word_before": "wrd_000011",
                "nearest_word_after": None,
            },
            {
                "interval_id": "sil_000002",
                "start_us": 5_000_000,
                "end_us": 5_600_000,
                "classification": "inter_word",
                "nearest_word_before": "wrd_000011",
                "nearest_word_after": None,
            },
            {
                "interval_id": "sil_000003",
                "start_us": 6_000_000,
                "end_us": 6_100_000,
                "classification": "inter_word",
                "nearest_word_before": "wrd_000011",
                "nearest_word_after": None,
            },
        ],
    }

    payload = build_edit_proposals(
        package_root=package_root,
        layout=layout,
        source_manifest_path=source_manifest_path,
        source_manifest={"media_duration_us": 12_000_000},
        transcript_path=transcript_path,
        transcript=transcript,
        silence_path=silence_path,
        silence=silence,
        policy=EditingPolicy.smart_dense(),
        revision_id="rev_001",
    )

    filler_proposals = [
        item for item in payload["proposals"] if item["proposal_type"] == "filler_word"
    ]
    pause_proposals = [
        item for item in payload["proposals"] if item["proposal_type"] == "long_pause"
    ]
    assert len(filler_proposals) == 4
    assert len(pause_proposals) == 2
    assert payload["policy_id"] == "pol_smart_dense"
    assert payload["policy_version"] == 3
    assert all(
        item["join_strategy"] == "hard_cut_with_micro_audio_crossfade"
        for item in payload["proposals"]
    )
    assert all(item["join_preview_required"] for item in payload["proposals"])
    assert json.loads(json.dumps(payload))["proposals"] == payload["proposals"]


def test_smart_dense_classifies_the_complete_speech_issue_taxonomy(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = ProjectLayout(tmp_path)
    source_manifest_path = tmp_path / "source-manifest.json"
    transcript_path = tmp_path / "transcript.json"
    silence_path = tmp_path / "silence.json"
    source_manifest_path.write_text("source manifest", encoding="utf-8")
    transcript_path.write_text("transcript", encoding="utf-8")
    silence_path.write_text("silence", encoding="utf-8")

    def word(
        index: int,
        text: str,
        start_us: int,
        end_us: int,
        probability: float = 0.99,
    ) -> dict[str, object]:
        return {
            "word_id": f"wrd_{index:06d}",
            "text": text,
            "start_us": start_us,
            "end_us": end_us,
            "probability": probability,
            "timing_status": "certain",
        }

    words = [
        word(1, "hello", 500_000, 800_000),
        word(2, "um", 850_000, 1_050_000),
        word(3, "you", 1_100_000, 1_300_000),
        word(4, "know", 1_320_000, 1_500_000),
        word(5, "good", 1_550_000, 1_800_000),
        word(6, "i", 1_850_000, 2_000_000),
        word(7, "i", 2_010_000, 2_160_000),
        word(8, "actually", 2_200_000, 2_400_000),
        word(9, "chose", 2_500_000, 2_800_000),
        word(10, "the", 3_000_000, 3_200_000),
        word(11, "we", 5_000_000, 5_200_000),
        word(12, "ship", 5_220_000, 5_500_000),
        word(13, "now", 5_520_000, 5_800_000),
        word(14, "we", 5_900_000, 6_100_000),
        word(15, "ship", 6_120_000, 6_400_000),
        word(16, "now", 6_420_000, 6_700_000),
        word(17, "fast", 7_000_000, 7_200_000),
        word(18, "local", 7_220_000, 7_400_000),
        word(19, "edit", 7_420_000, 7_650_000),
        word(20, "quickly", 7_670_000, 7_900_000),
        word(21, "fast", 8_200_000, 8_400_000),
        word(22, "local", 8_420_000, 8_600_000),
        word(23, "edits", 8_620_000, 8_850_000),
        word(24, "quickly", 8_870_000, 9_100_000),
        word(25, "reduce", 10_000_000, 10_200_000),
        word(26, "edit", 10_220_000, 10_400_000),
        word(27, "render", 10_420_000, 10_600_000),
        word(28, "time", 10_620_000, 10_800_000),
        word(29, "edit", 11_000_000, 11_200_000),
        word(30, "time", 11_220_000, 11_400_000),
        word(31, "improve", 11_420_000, 11_600_000),
        word(32, "render", 11_620_000, 11_800_000),
        word(33, "unclear", 12_000_000, 12_200_000, probability=0.3),
        word(34, "camera", 12_300_000, 12_600_000),
        word(35, "duplicate", 13_000_000, 13_200_000),
        word(36, "take", 13_220_000, 13_400_000),
        word(37, "here", 13_420_000, 13_600_000),
        word(38, "duplicate", 14_000_000, 14_200_000),
        word(39, "take", 14_220_000, 14_400_000),
        word(40, "here", 14_420_000, 14_600_000),
    ]
    transcript = {
        "source_duration_us": 22_000_000,
        "words": words,
        "segments": [
            {
                "segment_id": "seg_take_a",
                "text": "duplicate take here",
                "start_us": 13_000_000,
                "end_us": 13_600_000,
                "word_ids": ["wrd_000035", "wrd_000036", "wrd_000037"],
                "average_log_probability": -0.2,
            },
            {
                "segment_id": "seg_take_b",
                "text": "duplicate take here",
                "start_us": 14_000_000,
                "end_us": 14_600_000,
                "word_ids": ["wrd_000038", "wrd_000039", "wrd_000040"],
                "average_log_probability": -0.8,
            },
        ],
        "confidence_summary": {"speaker_count": 1},
    }
    silence = {
        "source_duration_us": 22_000_000,
        "intervals": [
            {
                "interval_id": "sil_abandoned",
                "start_us": 3_200_000,
                "end_us": 4_800_000,
                "classification": "inter_word",
                "nearest_word_before": "wrd_000010",
                "nearest_word_after": "wrd_000011",
            },
            {
                "interval_id": "sil_dead_air",
                "start_us": 15_000_000,
                "end_us": 17_000_000,
                "classification": "inter_word",
                "nearest_word_before": "wrd_000040",
                "nearest_word_after": None,
            },
        ],
        "noise_events": [
            {
                "event_id": "noise_keyboard",
                "start_us": 18_000_000,
                "end_us": 18_080_000,
                "kind": "keyboard",
                "confidence": 0.99,
                "nearest_word_before": "wrd_000040",
                "nearest_word_after": None,
            }
        ],
    }

    payload = build_edit_proposals(
        package_root=package_root,
        layout=layout,
        source_manifest_path=source_manifest_path,
        source_manifest={"media_duration_us": 22_000_000},
        transcript_path=transcript_path,
        transcript=transcript,
        silence_path=silence_path,
        silence=silence,
        policy=EditingPolicy.smart_dense(),
        revision_id="rev_001",
    )
    proposal_types = {item["proposal_type"] for item in payload["proposals"]}
    assert {
        "filler_word",
        "filler_phrase",
        "stutter",
        "false_start",
        "abandoned_phrase",
        "self_correction",
        "exact_repetition",
        "near_repetition",
        "semantic_repetition",
        "duplicate_take",
        "weak_take",
        "dead_air",
        "accidental_noise",
        "housekeeping",
    } <= proposal_types


def test_smart_dense_policy_compiles_auto_review_and_blocked_fallbacks() -> None:
    proposals = [
        {
            "proposal_id": "prp_auto",
            "proposal_type": "filler_word",
            "proposed_cut_range": {"start_us": 1_000_000, "end_us": 1_200_000},
            "confidence": 0.99,
            "meaning_risk": "low",
            "continuity_risk": "low",
        },
        {
            "proposal_id": "prp_review",
            "proposal_type": "semantic_repetition",
            "proposed_cut_range": {"start_us": 2_000_000, "end_us": 2_400_000},
            "confidence": 0.99,
            "meaning_risk": "medium",
            "continuity_risk": "medium",
        },
        {
            "proposal_id": "prp_blocked",
            "proposal_type": "filler_word",
            "proposed_cut_range": {"start_us": 3_000_000, "end_us": 3_200_000},
            "confidence": 0.99,
            "meaning_risk": "low",
            "continuity_risk": "low",
        },
    ]

    compiled = compile_smart_dense_policy(
        proposals,
        EditingPolicy.smart_dense(),
        protected=[(3_100_000, 3_150_000)],
        protected_reasons=["wrd_000003:negation"],
    )

    by_id = {item["proposal_id"]: item for item in compiled}
    assert by_id["prp_auto"]["policy_result"] == "auto_eligible"
    assert by_id["prp_review"]["policy_result"] == "review_required"
    assert by_id["prp_blocked"]["policy_result"] == "blocked"
    assert by_id["prp_blocked"]["protected_content_check"]["passed"] is False
    assert all(item["safe_fallback"] == "keep_original" for item in compiled)
    assert all(item["approval_required"] for item in compiled)
