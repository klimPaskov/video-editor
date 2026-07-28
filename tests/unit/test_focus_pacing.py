from __future__ import annotations

from pathlib import Path

import pytest

from videoedit.domain.models import RationalFrameRate, TimelineSpec, VideoLayer
from videoedit.services.focus_pacing import (
    build_focus_pacing_plan,
    build_zoom_keyframes,
    classify_speedup_candidate,
    classify_zoom_candidate,
    review_batch,
    validate_focus_pacing_plan,
)
from videoedit.services.focus_qa import evaluate_focus_pacing_qa
from videoedit.services.retiming import (
    compile_retimed_timeline,
    map_source_range,
    rebase_items,
    rebase_timecoded_items,
    rebase_transcript,
    validate_retimed_timeline,
)
from videoedit.services.visual_timeline import apply_purposeful_zooms

ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


def _zoom_candidate() -> dict[str, object]:
    return {
        "zoom_id": "zoom_prompt_box",
        "purpose": "prompt_box",
        "source_range": {"start_us": 500_000, "end_us": 2_500_000},
        "target_visible_range": {"start_us": 400_000, "end_us": 2_700_000},
        "zoom_in_end_us": 900_000,
        "zoom_out_start_us": 2_100_000,
        "target_description": "Visible prompt box",
        "target_track": [
            {
                "time_us": 500_000,
                "bbox": {"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.4},
            },
            {
                "time_us": 2_400_000,
                "bbox": {"x": 0.27, "y": 0.2, "width": 0.5, "height": 0.4},
            },
        ],
        "peak_scale": 1.35,
        "reason": "Make the prompt box readable while it is used.",
        "confidence": {
            "target_visibility": 0.99,
            "target_identity": 0.99,
            "boundary": 0.98,
            "stability": 0.99,
            "overall": 0.985,
        },
        "evidence_frames": ["start.png", "end.png"],
    }


def _speedup_candidate() -> dict[str, object]:
    return {
        "speedup_id": "speed_prompt_write",
        "action_type": "prompt_writing",
        "source_range": {"start_us": 1_000_000, "end_us": 2_600_000},
        "request_source": "project_brief",
        "request_text": "Speed up visible prompt writing only.",
        "playback_rate": 2.5,
        "audio_mode": "audible_pitch_preserved",
        "audio_exception_explicit": False,
        "forbidden_content_check": {
            "contains_browsing": False,
            "contains_reading": False,
            "contains_waiting": False,
            "contains_other_action": False,
            "contains_navigation": False,
            "contains_result_inspection": False,
            "contains_loading": False,
            "contains_cursor_wandering": False,
        },
        "start_evidence_frame": "speed-start.png",
        "end_evidence_frame": "speed-end.png",
        "action_visibility_confidence": 0.99,
        "boundary_confidence": 0.98,
        "overall_confidence": 0.985,
        "reason": "Condense only the visible prompt-writing action.",
    }


def test_examples_and_positive_focus_plan_remain_schema_valid() -> None:
    focus_example = validate_focus_pacing_plan(
        ROOT,
        __import__("json").loads(
            (ROOT / "examples" / "focus_pacing_plan.example.json").read_text()
        ),
    )
    assert focus_example.zooms[0].policy_result == "auto_eligible"

    payload = build_focus_pacing_plan(
        package_root=ROOT,
        project_id="p21_focus",
        revision_id="rev_001",
        inputs=[{"artifact_id": "art_source", "sha256": HASH}],
        zoom_candidates=[_zoom_candidate()],
        speedup_candidates=[_speedup_candidate()],
        operator_request={
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
        config_hash=HASH,
    )
    plan = validate_focus_pacing_plan(ROOT, payload)
    assert plan.zooms[0].policy_result == "auto_eligible"
    assert plan.speedups[0].audio_mode == "audible_pitch_preserved"
    assert review_batch(plan) == []


def test_unclear_zoom_has_safe_no_zoom_fallback() -> None:
    candidate = _zoom_candidate()
    candidate["confidence"] = {
        "target_visibility": 0.7,
        "target_identity": 0.7,
        "boundary": 0.7,
        "stability": 0.7,
        "overall": 0.7,
    }
    zoom = classify_zoom_candidate(candidate)
    assert zoom.policy_result == "skipped"
    assert zoom.fallback == "no_zoom"
    assert "confidence_below_review_threshold" in zoom.warnings


def test_speedup_requires_request_and_rejects_mixed_activity() -> None:
    with pytest.raises(ValueError, match="explicit operator request"):
        classify_speedup_candidate(
            _speedup_candidate(),
            operator_request={  # type: ignore[arg-type]
                "speedups_requested": False,
                "request_source": "none",
                "request_text": None,
            },
        )

    mixed = _speedup_candidate()
    mixed_check = dict(mixed["forbidden_content_check"])  # type: ignore[arg-type]
    mixed_check["contains_browsing"] = True
    mixed["forbidden_content_check"] = mixed_check
    with pytest.raises(ValueError, match="forbidden"):
        classify_speedup_candidate(
            mixed,
            operator_request={  # type: ignore[arg-type]
                "speedups_requested": True,
                "request_source": "project_brief",
                "request_text": "Speed up visible prompt writing only.",
            },
        )
    safe_plan = build_focus_pacing_plan(
        package_root=ROOT,
        project_id="p21_safe",
        revision_id="rev_001",
        inputs=[{"artifact_id": "art_source", "sha256": HASH}],
        speedup_candidates=[mixed],
        operator_request={
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
        config_hash=HASH,
    )
    assert safe_plan["speedups"] == []
    assert "speedup_fallback_normal_speed:speed_prompt_write" in safe_plan["warnings"]


def test_retimed_map_rebases_downstream_ranges_and_zoom_keyframes() -> None:
    speedup = classify_speedup_candidate(
        _speedup_candidate(),
        operator_request={  # type: ignore[arg-type]
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
    )
    payload = compile_retimed_timeline(
        package_root=ROOT,
        project_id="p21_retime",
        revision_id="rev_001",
        source_duration_us=4_000_000,
        keep_ranges=[(0, 4_000_000)],
        speedups=[speedup],
        edit_decision_list_sha256=HASH,
        focus_pacing_plan_sha256=HASH,
        config_hash=HASH,
    )
    timeline = validate_retimed_timeline(ROOT, payload)
    assert timeline.output_duration_us == 3_040_000
    assert map_source_range(timeline, 1_000_000, 2_600_000) == {
        "start_us": 1_000_000,
        "end_us": 1_640_000,
    }
    assert rebase_items(
        timeline,
        [{"start_us": 2_600_000, "end_us": 3_000_000, "id": "caption"}],
    ) == [{"start_us": 1_640_000, "end_us": 2_040_000, "id": "caption"}]
    rebased_transcript = rebase_transcript(
        timeline,
        {
            "source_duration_us": 4_000_000,
            "segments": [
                {
                    "segment_id": "seg_001",
                    "text": "write now",
                    "start_us": 1_000_000,
                    "end_us": 3_000_000,
                    "word_ids": ["w1", "w2"],
                    "average_log_probability": 0.0,
                    "no_speech_probability": 0.0,
                }
            ],
            "words": [
                {
                    "word_id": "w1",
                    "segment_id": "seg_001",
                    "text": "write",
                    "start_us": 1_100_000,
                    "end_us": 1_300_000,
                    "probability": 1.0,
                    "timing_status": "original",
                },
                {
                    "word_id": "w2",
                    "segment_id": "seg_001",
                    "text": "now",
                    "start_us": 2_600_000,
                    "end_us": 2_900_000,
                    "probability": 1.0,
                    "timing_status": "original",
                },
            ],
            "warnings": [],
            "confidence_summary": {
                "word_count": 2,
                "mean_word_probability": 1.0,
                "minimum_word_probability": 1.0,
                "low_confidence_word_ids": [],
                "uncertain_word_count": 0,
                "speaker_count": 0,
            },
        },
    )
    assert rebased_transcript["output_duration_us"] == 3_040_000
    assert rebased_transcript["words"][1]["start_us"] == 1_640_000
    assert (
        rebase_timecoded_items(
            timeline, [{"start_us": 2_600_000, "end_us": 2_900_000, "asset": "broll"}]
        )[0]["end_us"]
        == 1_940_000
    )

    zoom = classify_zoom_candidate(_zoom_candidate())
    keyframes = build_zoom_keyframes(
        zoom,
        fps_numerator=30,
        width=1920,
        height=1080,
    )
    assert [item.easing for item in keyframes] == [
        "linear",
        "ease_in",
        "ease_in_out",
        "ease_out",
    ]
    assert max(abs(item.x or 0) for item in keyframes) <= 1920 * (1.35 - 1) / 2

    visual = TimelineSpec(
        project_id="p21_retime",
        width=1920,
        height=1080,
        fps=RationalFrameRate(numerator=30, denominator=1),
        duration_frames=120,
        layers=[
            VideoLayer(
                id="base-edit",
                start_frame=0,
                duration_frames=120,
                src="generated/base.mp4",
            )
        ],
    )
    focus_plan = build_focus_pacing_plan(
        package_root=ROOT,
        project_id="p21_retime",
        revision_id="rev_001",
        inputs=[{"artifact_id": "art_source", "sha256": HASH}],
        zoom_candidates=[_zoom_candidate()],
        config_hash=HASH,
    )
    with_zoom = apply_purposeful_zooms(visual, focus_plan)
    assert len(with_zoom.layers[0].keyframes) == 4

    full_plan_payload = build_focus_pacing_plan(
        package_root=ROOT,
        project_id="p21_retime",
        revision_id="rev_001",
        inputs=[{"artifact_id": "art_source", "sha256": HASH}],
        zoom_candidates=[_zoom_candidate()],
        speedup_candidates=[_speedup_candidate()],
        operator_request={
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
        config_hash=HASH,
    )
    full_plan = validate_focus_pacing_plan(ROOT, full_plan_payload)
    qa = evaluate_focus_pacing_qa(
        full_plan,
        retimed_timeline=timeline,
        transcript={
            "segments": [{"words": [{"start_us": 1_100_000, "end_us": 1_200_000, "word_id": "w1"}]}]
        },
        keyframes_by_zoom={
            "zoom_prompt_box": build_zoom_keyframes(
                full_plan.zooms[0], fps_numerator=30, width=1920, height=1080
            )
        },
    )
    assert qa["final_ready"] is True
    assert qa["required_failures"] == 0


def test_zoom_qa_checks_compiled_translation_against_the_visible_target() -> None:
    zoom = classify_zoom_candidate(_zoom_candidate())
    keyframes = build_zoom_keyframes(
        zoom,
        fps_numerator=30,
        width=1920,
        height=1080,
    )
    plan = validate_focus_pacing_plan(
        ROOT,
        build_focus_pacing_plan(
            package_root=ROOT,
            project_id="p21_zoom_geometry",
            revision_id="rev_001",
            inputs=[{"artifact_id": "art_source", "sha256": HASH}],
            zoom_candidates=[_zoom_candidate()],
            config_hash=HASH,
        ),
    )

    passing = evaluate_focus_pacing_qa(
        plan,
        keyframes_by_zoom={zoom.zoom_id: keyframes},
        width=1920,
        height=1080,
    )
    centered = next(
        item for item in passing["findings"] if item["check_code"] == "ZOOM_TARGET_CENTERED"
    )
    assert centered["status"] == "pass"

    bad_keyframes = list(keyframes)
    bad_keyframes[1] = bad_keyframes[1].model_copy(update={"x": 0.0})
    failing = evaluate_focus_pacing_qa(
        plan,
        keyframes_by_zoom={zoom.zoom_id: bad_keyframes},
        width=1920,
        height=1080,
    )
    centered_failure = next(
        item for item in failing["findings"] if item["check_code"] == "ZOOM_TARGET_CENTERED"
    )
    assert centered_failure["status"] == "fail"
    assert centered_failure["required"] is True


def test_zoom_application_rejects_unknown_approval_ids() -> None:
    visual = TimelineSpec(
        project_id="p21_zoom_ids",
        width=1920,
        height=1080,
        fps=RationalFrameRate(numerator=30, denominator=1),
        duration_frames=120,
        layers=[VideoLayer(id="base-edit", start_frame=0, duration_frames=120, src="base.mp4")],
    )
    plan = build_focus_pacing_plan(
        package_root=ROOT,
        project_id="p21_zoom_ids",
        revision_id="rev_001",
        inputs=[{"artifact_id": "art_source", "sha256": HASH}],
        zoom_candidates=[_zoom_candidate()],
        config_hash=HASH,
    )
    with pytest.raises(ValueError, match="not present in the focus plan"):
        apply_purposeful_zooms(visual, plan, approved_zoom_ids={"zoom_missing"})


def test_retimed_speedup_is_split_across_disjoint_kept_ranges() -> None:
    candidate_input = _speedup_candidate()
    candidate_input["source_range"] = {"start_us": 500_000, "end_us": 2_500_000}
    speedup = classify_speedup_candidate(
        candidate_input,
        operator_request={  # type: ignore[arg-type]
            "speedups_requested": True,
            "request_source": "project_brief",
            "request_text": "Speed up visible prompt writing only.",
        },
    )
    payload = compile_retimed_timeline(
        package_root=ROOT,
        project_id="p21_split_retime",
        revision_id="rev_001",
        source_duration_us=4_000_000,
        keep_ranges=[(0, 1_000_000), (2_000_000, 4_000_000)],
        speedups=[speedup],
        edit_decision_list_sha256=HASH,
        focus_pacing_plan_sha256=HASH,
        config_hash=HASH,
    )

    timeline = validate_retimed_timeline(ROOT, payload)
    speed_segments = [
        segment for segment in timeline.segments if segment.operation == "prompt_speedup"
    ]
    assert [(item.source_range.start_us, item.source_range.end_us) for item in speed_segments] == [
        (500_000, 1_000_000),
        (2_000_000, 2_500_000),
    ]
    assert timeline.output_duration_us == 2_400_000
