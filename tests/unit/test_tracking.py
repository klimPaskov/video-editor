from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import ApprovalRequiredError, StaleApprovalError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import sha256_file
from videoedit.services.tracking import (
    append_tracked_image_layer,
    tracked_image_layer,
    write_object_track_keyframes,
    write_object_track_review,
)


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def write_result(path: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "job_id": "track-ball",
        "status": "complete",
        "worker": "sam3",
        "input_path": "/tmp/source.mp4",
        "input_sha256": "0" * 64,
        "prompt": {"text": "ball", "frame_index": 4},
        "mask_pattern": "/tmp/masks/%06d.png",
        "frame_count": 3,
        "frames": [
            {
                "frame_index": 4,
                "combined_mask_path": "/tmp/masks/000004.png",
                "objects": [
                    {
                        "object_id": 7,
                        "mask_path": "/tmp/instances/000004-object-7.png",
                        "visible": True,
                        "area_pixels": 400,
                        "bbox_xywh": [100, 200, 20, 20],
                        "centroid_xy": [109.5, 209.5],
                    }
                ],
            },
            {
                "frame_index": 5,
                "combined_mask_path": "/tmp/masks/000005.png",
                "objects": [
                    {
                        "object_id": 7,
                        "mask_path": "/tmp/instances/000005-object-7.png",
                        "visible": False,
                        "area_pixels": 0,
                        "bbox_xywh": None,
                        "centroid_xy": None,
                    }
                ],
            },
            {
                "frame_index": 6,
                "combined_mask_path": "/tmp/masks/000006.png",
                "objects": [
                    {
                        "object_id": 7,
                        "mask_path": "/tmp/instances/000006-object-7.png",
                        "visible": True,
                        "area_pixels": 576,
                        "bbox_xywh": [120, 190, 24, 24],
                        "centroid_xy": [131.5, 201.5],
                    }
                ],
            },
        ],
        "software": {"sam3": "test", "python": "3.12"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_validation(tmp_path: Path, result: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source")
    payload = {
        "schema_name": "segmentation_validation",
        "schema_version": "1.0.0",
        "artifact_id": "art_segmentation_validation",
        "project_id": "track_review",
        "revision_id": "rev_001",
        "created_at": "2026-07-24T12:00:00Z",
        "source": {
            "path": str(source.resolve()),
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
        },
        "result": {
            "path": str(result.resolve()),
            "sha256": sha256_file(result),
            "size_bytes": result.stat().st_size,
        },
        "source_range": {"start_frame": 4, "end_frame": 7},
        "status": "pass",
        "validation": {
            "result_schema": "pass",
            "source_identity": "pass",
            "frame_range": "pass",
            "frame_continuity": "pass",
            "mask_files": "pass",
            "mask_dimensions": "pass",
            "mask_format": "pass",
            "geometry": "pass",
        },
        "diagnostics": {
            "missing_frames": [],
            "identity_warnings": [],
            "area_jump_frames": [],
            "centroid_jump_frames": [],
            "leak_warnings": [],
        },
        "review_frame_indices": [4, 5, 6],
        "contact_sheets": [],
        "warnings": [],
    }
    path = tmp_path / "segmentation-validation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_approved_review(tmp_path: Path, result: Path) -> tuple[Path, Path]:
    validation = write_validation(tmp_path, result)
    review = write_object_track_review(
        package_root(),
        result,
        validation,
        output_path=tmp_path / "object-track-review.json",
        object_id=7,
        actor="fixture-reviewer",
        decision="approved",
        findings={
            "identity": "pass",
            "continuity": "pass",
            "geometry": "pass",
            "occlusion": "pass",
        },
    )
    return validation, review


def test_tracking_result_compiles_to_centered_keyframes(tmp_path: Path) -> None:
    result = tmp_path / "segmentation.json"
    write_result(result)
    validation, review = write_approved_review(tmp_path, result)

    layer = tracked_image_layer(
        package_root(),
        result,
        "generated/demo/apple.png",
        timeline_width=640,
        timeline_height=360,
        segmentation_validation_path=validation,
        track_review_path=review,
        object_id=7,
        padding=1.5,
    )

    assert layer.start_frame == 4
    assert layer.duration_frames == 3
    assert layer.transform.x == 109.5 - 320
    assert layer.transform.y == 209.5 - 180
    assert layer.transform.width == 30
    assert [item.frame for item in layer.keyframes] == [0, 1, 2]
    assert layer.keyframes[1].opacity == 0


def test_tracking_layer_can_be_appended_to_valid_timeline(tmp_path: Path) -> None:
    result = tmp_path / "segmentation.json"
    timeline = tmp_path / "timeline.json"
    output = tmp_path / "timeline-with-overlay.json"
    write_result(result)
    validation, review = write_approved_review(tmp_path, result)
    timeline.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "project_id": "demo",
                "width": 640,
                "height": 360,
                "fps": 30,
                "duration_frames": 20,
                "background": {"kind": "solid", "value": "#000000"},
                "layers": [],
                "audio": [],
                "captions": [],
            }
        ),
        encoding="utf-8",
    )

    append_tracked_image_layer(
        package_root(),
        timeline,
        result,
        "generated/demo/apple.png",
        output,
        segmentation_validation_path=validation,
        track_review_path=review,
        object_id=7,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["layers"][0]["kind"] == "image"
    assert payload["layers"][0]["keyframes"][2]["frame"] == 2


def test_tracking_requires_an_explicit_operator_review(tmp_path: Path) -> None:
    result = tmp_path / "segmentation.json"
    write_result(result)

    with pytest.raises(ApprovalRequiredError, match="approved track review"):
        tracked_image_layer(
            package_root(),
            result,
            "generated/demo/apple.png",
            timeline_width=640,
            timeline_height=360,
            object_id=7,
        )


def test_tracking_smooths_visible_geometry_and_writes_hash_bound_manifest(
    tmp_path: Path,
) -> None:
    result = tmp_path / "segmentation.json"
    write_result(result)
    payload = json.loads(result.read_text(encoding="utf-8"))
    middle = payload["frames"][1]["objects"][0]
    middle.update(
        {
            "visible": True,
            "area_pixels": 484,
            "bbox_xywh": [110, 195, 22, 22],
            "centroid_xy": [120.5, 205.5],
        }
    )
    result.write_text(json.dumps(payload), encoding="utf-8")
    validation, review = write_approved_review(tmp_path, result)

    manifest = write_object_track_keyframes(
        package_root(),
        result,
        validation,
        review,
        output_path=tmp_path / "object-track-keyframes.json",
        timeline_width=640,
        timeline_height=360,
        object_id=7,
        padding=1.0,
        window_radius_frames=1,
        frame_rate=30,
    )
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    validate_artifact(package_root(), "object_track_keyframes", manifest_payload)
    assert manifest_payload["keyframes"][1]["x"] == pytest.approx(120.5 - 320)
    assert manifest_payload["keyframes"][1]["width"] == pytest.approx(22.0)
    assert manifest_payload["keyframes"][1]["scale"] == 1.0
    assert manifest_payload["keyframes"][1]["rotation_degrees"] == 0.0


def test_tracking_rejects_changed_result_after_review(tmp_path: Path) -> None:
    result = tmp_path / "segmentation.json"
    write_result(result)
    validation, review = write_approved_review(tmp_path, result)
    result.write_text(result.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(StaleApprovalError, match="stale"):
        write_object_track_keyframes(
            package_root(),
            result,
            validation,
            review,
            output_path=tmp_path / "object-track-keyframes.json",
            timeline_width=640,
            timeline_height=360,
            object_id=7,
        )
