from __future__ import annotations

from typing import Any

from videoedit.services.segment_visual_qa import _timeline_checks


def _finding(findings: list[dict[str, Any]], check_code: str) -> dict[str, Any]:
    return next(item for item in findings if item["check_code"] == check_code)


def _timeline(layers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "width": 640,
        "height": 360,
        "duration_frames": 30,
        "layers": layers,
        "captions": [],
    }


def test_middle_text_plate_is_not_misclassified_as_picture_in_picture() -> None:
    findings = _timeline_checks(
        _timeline(
            [
                {
                    "kind": "text",
                    "id": "middle-plate-title",
                    "start_frame": 0,
                    "duration_frames": 30,
                    "z_index": 10,
                    "role": "middle",
                    "text": "TEXT BEHIND SUBJECT",
                    "transform": {
                        "x": 0,
                        "y": -65,
                        "width": None,
                        "height": None,
                        "scale": 1,
                    },
                }
            ]
        )
    )

    pip = _finding(findings, "PIP_FRAMING")
    assert pip["status"] == "skipped"
    assert pip["evidence"] == {}


def test_bounded_middle_media_is_checked_as_picture_in_picture() -> None:
    findings = _timeline_checks(
        _timeline(
            [
                {
                    "kind": "video",
                    "id": "screen-overlay",
                    "start_frame": 0,
                    "duration_frames": 30,
                    "z_index": 10,
                    "role": "middle",
                    "src": "generated/screen-recording.mp4",
                    "transform": {
                        "x": 500,
                        "y": 240,
                        "width": 120,
                        "height": 90,
                        "scale": 1,
                    },
                }
            ]
        )
    )

    pip = _finding(findings, "PIP_FRAMING")
    assert pip["status"] == "pass"
    assert pip["evidence"]["candidate_layer_ids"] == ["screen-overlay"]


def test_explicit_pip_media_marker_still_fails_without_dimensions() -> None:
    findings = _timeline_checks(
        _timeline(
            [
                {
                    "kind": "image",
                    "id": "pip-card",
                    "start_frame": 0,
                    "duration_frames": 30,
                    "z_index": 10,
                    "role": "front",
                    "src": "generated/pip-card.png",
                    "transform": {
                        "x": 20,
                        "y": 20,
                        "width": None,
                        "height": None,
                        "scale": 1,
                    },
                }
            ]
        )
    )

    pip = _finding(findings, "PIP_FRAMING")
    assert pip["status"] == "fail"
    assert pip["evidence"]["out_of_bounds"] == ["pip-card"]
