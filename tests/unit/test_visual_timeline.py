from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from videoedit.domain.models import (
    RationalFrameRate,
    TextLayer,
    TimelineSpec,
    TimelineTransition,
)
from videoedit.services.visual_timeline import (
    compile_transition_plan,
    validate_visual_timeline,
    write_visual_timeline,
)


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def timeline() -> TimelineSpec:
    return TimelineSpec(
        project_id="timeline_test",
        width=640,
        height=360,
        fps=RationalFrameRate(numerator=30000, denominator=1001),
        duration_frames=30,
        layers=[
            TextLayer(
                id="front-title",
                start_frame=0,
                duration_frames=30,
                role="front",
                text="test",
            )
        ],
    )


def test_rational_frame_rate_is_schema_valid_and_serializable(tmp_path: Path) -> None:
    model = timeline()
    path = write_visual_timeline(package_root(), tmp_path / "timeline.json", model)
    payload = json.loads(path.read_text(encoding="utf-8"))
    validated = validate_visual_timeline(package_root(), payload)

    assert payload["fps"] == {"numerator": 30000, "denominator": 1001}
    assert payload["layers"][0]["template"] == "plain"
    assert validated.fps == RationalFrameRate(numerator=30000, denominator=1001)


def test_timeline_rejects_duplicate_ids_and_invalid_dimensions() -> None:
    with pytest.raises(ValidationError):
        TimelineSpec(
            project_id="timeline_test",
            duration_frames=30,
            layers=[
                TextLayer(id="same", start_frame=0, duration_frames=10, text="a"),
                TextLayer(id="same", start_frame=10, duration_frames=10, text="b"),
            ],
        )
    with pytest.raises(ValidationError):
        TextLayer(
            id="invalid", start_frame=0, duration_frames=10, text="x", transform={"width": -1}
        )


def test_visual_timeline_rejects_network_refs_and_hash_mismatch(tmp_path: Path) -> None:
    payload = timeline().model_dump(mode="json")
    payload["layers"][0]["text"] = "safe"
    payload["layers"].append(
        {
            "kind": "image",
            "id": "remote-image",
            "start_frame": 0,
            "duration_frames": 10,
            "z_index": 1,
            "role": "middle",
            "src": "https://example.invalid/image.png",
            "fit": "contain",
            "transform": {
                "x": 0,
                "y": 0,
                "width": None,
                "height": None,
                "scale": 1,
                "rotation_degrees": 0,
                "opacity": 1,
            },
            "keyframes": [],
        }
    )
    with pytest.raises(ValueError, match="local"):
        validate_visual_timeline(package_root(), payload)

    static_root = tmp_path / "public"
    staged = static_root / "generated" / "asset.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"local asset")
    payload = timeline().model_dump(mode="json")
    payload["assets"] = [
        {
            "asset_id": "asset-local",
            "src": "generated/asset.bin",
            "sha256": "0" * 64,
            "role": "middle",
        }
    ]
    with pytest.raises(ValueError, match="hash"):
        validate_visual_timeline(package_root(), payload, asset_root=static_root)

    payload.pop("assets")
    payload["layers"].append(
        {
            "kind": "image",
            "id": "unbound-image",
            "start_frame": 0,
            "duration_frames": 10,
            "z_index": 1,
            "role": "middle",
            "src": "generated/asset.bin",
            "fit": "contain",
            "transform": {
                "x": 0,
                "y": 0,
                "width": None,
                "height": None,
                "scale": 1,
                "rotation_degrees": 0,
                "opacity": 1,
            },
            "keyframes": [],
        }
    )
    with pytest.raises(ValueError, match="hash-bound asset reference"):
        validate_visual_timeline(package_root(), payload, asset_root=static_root)


def test_transition_compilation_is_fail_closed_until_ids_are_approved() -> None:
    plan_path = package_root() / "examples" / "transition_plan.example.json"

    unapproved = compile_transition_plan(
        package_root(),
        plan_path,
        fps=(30, 1),
        duration_frames=360,
    )
    assert unapproved.transitions == ()
    assert "transition_not_approved:trn_new_point_001" in unapproved.warnings

    approved = compile_transition_plan(
        package_root(),
        plan_path,
        fps=(30, 1),
        duration_frames=360,
        approved_transition_ids={"trn_new_point_001"},
    )
    assert len(approved.transitions) == 1
    transition = approved.transitions[0]
    assert transition.start_frame == 72
    assert transition.duration_frames == 9
    assert transition.incoming_first_readable_frame == 81
    assert transition.full_frame_coverage is True
    assert len(approved.transition_plan_sha256) == 64


def test_timeline_transitions_require_coverage_and_do_not_overlap() -> None:
    with pytest.raises(ValidationError, match="full-frame coverage"):
        TimelineTransition(
            id="trn_bad",
            start_frame=10,
            duration_frames=5,
            transition_type="swipe_left",
            full_frame_coverage=False,
            incoming_first_readable_frame=15,
        )

    with pytest.raises(ValidationError, match="overlap"):
        TimelineSpec(
            project_id="transition_test",
            duration_frames=60,
            transitions=[
                TimelineTransition(
                    id="trn_one",
                    start_frame=10,
                    duration_frames=10,
                    transition_type="swipe_left",
                    incoming_first_readable_frame=20,
                ),
                TimelineTransition(
                    id="trn_two",
                    start_frame=19,
                    duration_frames=5,
                    transition_type="push_right",
                    incoming_first_readable_frame=24,
                ),
            ],
        )
