from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.domain.models import (
    ImageLayer,
    TimelineAssetRef,
    TimelineSpec,
    Transform,
    TransformKeyframe,
)
from videoedit.errors import ApprovalRequiredError, SegmentationValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import sha256_file

TRACK_REVIEW_SCHEMA_VERSION = "1.0.0"
TRACK_KEYFRAMES_SCHEMA_VERSION = "1.0.0"
_FINDING_NAMES = ("identity", "continuity", "geometry", "occlusion")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentationValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise SegmentationValidationError(f"{label} must be a JSON object")
    return value


def _file_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SegmentationValidationError(f"tracked-artifact input is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _require_current_ref(reference: object, path: Path, label: str) -> None:
    if not isinstance(reference, Mapping):
        raise SegmentationValidationError(f"{label} reference is missing")
    current = _file_ref(path)
    if (
        reference.get("path") != current["path"]
        or reference.get("sha256") != current["sha256"]
        or reference.get("size_bytes") != current["size_bytes"]
    ):
        raise StaleApprovalError(f"{label} is stale for the current file")


def _source_range(payload: Mapping[str, Any]) -> tuple[int, int]:
    value = payload.get("source_range")
    if isinstance(value, Mapping):
        start = int(value.get("start_frame", -1))
        end = int(value.get("end_frame", -1))
        if start >= 0 and end > start:
            return start, end
    frames = payload.get("frames")
    if not isinstance(frames, list):
        raise SegmentationValidationError("segmentation result has no source range or frames")
    indices = [
        int(item["frame_index"])
        for item in frames
        if isinstance(item, Mapping) and "frame_index" in item
    ]
    if not indices:
        raise SegmentationValidationError("segmentation result contains no frame indices")
    return min(indices), max(indices) + 1


def _frame_rate_text(value: object) -> str:
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("frame rate must be positive")
        return f"{value}/1"
    if isinstance(value, Mapping):
        numerator = int(value.get("numerator", 0))
        denominator = int(value.get("denominator", 0))
        if numerator > 0 and denominator > 0:
            return f"{numerator}/{denominator}"
    text = str(value or "")
    if "/" in text:
        numerator_text, denominator_text = text.split("/", 1)
        try:
            numerator = int(numerator_text)
            denominator = int(denominator_text)
        except ValueError:
            numerator = denominator = 0
        if numerator > 0 and denominator > 0:
            return f"{numerator}/{denominator}"
    raise ValueError("frame rate must be an integer or positive rational")


def _default_review_indices(start: int, end: int) -> list[int]:
    middle = start + ((end - start - 1) // 2)
    return sorted({start, middle, end - 1})


def write_object_track_review(
    package_root: Path,
    result_path: Path,
    segmentation_validation_path: Path,
    output_path: Path | None = None,
    *,
    object_id: int = 1,
    actor: str,
    role: str = "editor",
    decision: str = "pending",
    reviewed_frame_indices: Sequence[int] | None = None,
    findings: Mapping[str, str] | None = None,
    notes: Sequence[str] = (),
    project_id: str | None = None,
    revision_id: str = "rev_001",
) -> Path:
    """Persist an operator-supplied object-track decision.

    This function records a decision supplied by the caller; it never derives an
    approval from SAM output or from the structural validation report.
    """

    result_path = result_path.resolve()
    validation_path = segmentation_validation_path.resolve()
    result = _read_object(result_path, "segmentation result")
    validation = _read_object(validation_path, "segmentation validation")
    validate_artifact(package_root, "segmentation_result", result)
    validate_artifact(package_root, "segmentation_validation", validation)
    if result.get("status") != "complete":
        raise SegmentationValidationError("object-track review requires a complete result")
    if object_id < 1:
        raise ValueError("object_id must be positive")
    if decision not in {"pending", "approved", "rejected"}:
        raise ValueError("track review decision must be pending, approved, or rejected")
    if not actor.strip():
        raise ValueError("track review actor must not be empty")
    start_frame, end_frame = _source_range(result)
    _require_current_ref(validation.get("result"), result_path, "segmentation validation result")
    validation_range = validation.get("source_range")
    if isinstance(validation_range, Mapping):
        if (
            int(validation_range.get("start_frame", -1)) != start_frame
            or int(validation_range.get("end_frame", -1)) != end_frame
        ):
            raise StaleApprovalError("segmentation validation source range is stale")
    selected_indices = sorted(
        {
            int(index)
            for index in (
                reviewed_frame_indices
                if reviewed_frame_indices is not None
                else validation.get(
                    "review_frame_indices", _default_review_indices(start_frame, end_frame)
                )
            )
        }
    )
    if not selected_indices or any(
        index < start_frame or index >= end_frame for index in selected_indices
    ):
        raise ValueError("reviewed frame indices must be inside the segmentation source range")
    selected_findings = {
        name: str((findings or {}).get(name, "pending")) for name in _FINDING_NAMES
    }
    if any(
        value not in {"pending", "pass", "warning", "fail"} for value in selected_findings.values()
    ):
        raise ValueError("track review findings contain an unsupported status")
    if decision == "approved":
        if validation.get("status") != "pass":
            raise ApprovalRequiredError(
                "an object track cannot be approved while structural segmentation "
                "review is not pass"
            )
        if any(value != "pass" for value in selected_findings.values()):
            raise ApprovalRequiredError("an approved object track requires pass findings")
    project_value = project_id or result.get("project_id") or "track_review"
    revision_value = str(result.get("revision_id") or revision_id)
    payload: dict[str, Any] = {
        "schema_name": "object_track_review",
        "schema_version": TRACK_REVIEW_SCHEMA_VERSION,
        "artifact_id": f"art_object_track_review_{object_id}",
        "project_id": str(project_value),
        "revision_id": revision_value,
        "created_at": now_iso(),
        "producer": producer("object-track-review", "human-review"),
        "source_result": _file_ref(result_path),
        "segmentation_validation": _file_ref(validation_path),
        "source_range": {"start_frame": start_frame, "end_frame": end_frame},
        "object_id": object_id,
        "reviewed_frame_indices": selected_indices,
        "decision": decision,
        "actor": actor,
        "role": role,
        "findings": selected_findings,
        "fallback": {"mode": "original_shot", "on_uncertain": "keep_original"},
        "notes": list(notes),
    }
    validate_artifact(package_root, "object_track_review", payload)
    output = (
        output_path.resolve()
        if output_path is not None
        else result_path.parent
        / f"object-track-review-{sha256_file(result_path)[:16]}-{object_id}.json"
    )
    if output.is_file():
        existing = _read_object(output, "existing object-track review")
        validate_artifact(package_root, "object_track_review", existing)
        stable_fields = (
            "source_result",
            "segmentation_validation",
            "source_range",
            "object_id",
            "reviewed_frame_indices",
            "decision",
            "actor",
            "role",
            "findings",
            "fallback",
            "notes",
        )
        if any(existing.get(field) != payload.get(field) for field in stable_fields):
            raise StaleApprovalError(
                "object-track review path already contains a different decision"
            )
        return output
    return write_validated_artifact(package_root, "object_track_review", output, payload)


def _approved_track_inputs(
    package_root: Path,
    result_path: Path,
    segmentation_validation_path: Path,
    track_review_path: Path,
    object_id: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], tuple[int, int]]:
    result_path = result_path.resolve()
    validation_path = segmentation_validation_path.resolve()
    review_path = track_review_path.resolve()
    result = _read_object(result_path, "segmentation result")
    validation = _read_object(validation_path, "segmentation validation")
    review = _read_object(review_path, "object-track review")
    validate_artifact(package_root, "segmentation_result", result)
    validate_artifact(package_root, "segmentation_validation", validation)
    validate_artifact(package_root, "object_track_review", review)
    if result.get("status") != "complete":
        raise SegmentationValidationError("object-track keyframes require a complete result")
    if validation.get("status") != "pass":
        raise SegmentationValidationError(
            "object-track keyframes require a passing segmentation review"
        )
    if review.get("decision") != "approved":
        raise ApprovalRequiredError(
            "object-track keyframes require an explicit approved track review"
        )
    if int(review.get("object_id", -1)) != object_id:
        raise StaleApprovalError(
            "object-track review object ID does not match the requested object"
        )
    findings = review.get("findings")
    if not isinstance(findings, Mapping) or any(
        findings.get(name) != "pass" for name in _FINDING_NAMES
    ):
        raise ApprovalRequiredError("object-track review contains unresolved findings")
    _require_current_ref(validation.get("result"), result_path, "segmentation validation result")
    _require_current_ref(review.get("source_result"), result_path, "object-track review result")
    _require_current_ref(
        review.get("segmentation_validation"), validation_path, "object-track review"
    )
    start_frame, end_frame = _source_range(result)
    review_range = review.get("source_range")
    if not isinstance(review_range, Mapping):
        raise StaleApprovalError("object-track review has no source range")
    if (
        int(review_range.get("start_frame", -1)) != start_frame
        or int(review_range.get("end_frame", -1)) != end_frame
    ):
        raise StaleApprovalError("object-track review source range is stale")
    frames = result.get("frames")
    if not isinstance(frames, list):
        raise SegmentationValidationError("segmentation result frames must be an array")
    frame_indices = sorted(
        int(item["frame_index"])
        for item in frames
        if isinstance(item, Mapping) and "frame_index" in item
    )
    if frame_indices != list(range(start_frame, end_frame)):
        raise SegmentationValidationError(
            "object-track keyframes require a contiguous approved source range"
        )
    return result, validation, review, (start_frame, end_frame)


def _geometry(target: Mapping[str, Any] | None) -> tuple[float, float, float, float] | None:
    if target is None or not bool(target.get("visible")):
        return None
    bbox = target.get("bbox_xywh")
    centroid = target.get("centroid_xy")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)):
        raise SegmentationValidationError("visible object geometry has no bounding box")
    if not isinstance(centroid, Sequence) or isinstance(centroid, (str, bytes)):
        raise SegmentationValidationError("visible object geometry has no centroid")
    if len(bbox) != 4 or len(centroid) != 2:
        raise SegmentationValidationError("visible object geometry has invalid dimensions")
    values = [*bbox, *centroid]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise SegmentationValidationError("visible object geometry contains a nonnumeric value")
    x, y, width, height = (float(value) for value in bbox)
    centroid_x, centroid_y = (float(value) for value in centroid)
    if (
        not all(math.isfinite(value) for value in (x, y, width, height, centroid_x, centroid_y))
        or width <= 0
        or height <= 0
    ):
        raise SegmentationValidationError("visible object geometry is not finite and positive")
    return centroid_x, centroid_y, width, height


def _smoothed_geometry(
    geometries: Sequence[tuple[float, float, float, float] | None],
    index: int,
    radius: int,
) -> tuple[float, float, float, float]:
    if geometries[index] is None:
        raise ValueError("cannot smooth an invisible frame")
    run_start = index
    while run_start > 0 and geometries[run_start - 1] is not None:
        run_start -= 1
    run_end = index
    while run_end + 1 < len(geometries) and geometries[run_end + 1] is not None:
        run_end += 1
    start = max(run_start, index - radius)
    end = min(run_end, index + radius)
    values = [geometries[item] for item in range(start, end + 1)]
    visible = [value for value in values if value is not None]
    count = len(visible)
    return (
        sum(value[0] for value in visible) / count,
        sum(value[1] for value in visible) / count,
        sum(value[2] for value in visible) / count,
        sum(value[3] for value in visible) / count,
    )


def build_object_track_keyframes_payload(
    package_root: Path,
    result_path: Path,
    segmentation_validation_path: Path,
    track_review_path: Path,
    *,
    timeline_width: int,
    timeline_height: int,
    object_id: int = 1,
    padding: float = 1.2,
    window_radius_frames: int = 1,
    frame_rate: object | None = None,
) -> dict[str, Any]:
    """Build deterministic Remotion keyframes from an explicitly approved track."""

    if timeline_width <= 0 or timeline_height <= 0:
        raise ValueError("timeline dimensions must be positive")
    if padding <= 0:
        raise ValueError("padding must be positive")
    if not 0 <= window_radius_frames <= 10:
        raise ValueError("window_radius_frames must be between 0 and 10")
    result, _validation, review, (start_frame, end_frame) = _approved_track_inputs(
        package_root,
        result_path,
        segmentation_validation_path,
        track_review_path,
        object_id,
    )
    frames = result["frames"]
    frame_by_index = {
        int(item["frame_index"]): item
        for item in frames
        if isinstance(item, Mapping) and "frame_index" in item
    }
    geometries: list[tuple[float, float, float, float] | None] = []
    target_presence: list[str] = []
    for absolute_frame in range(start_frame, end_frame):
        target = _target_object(frame_by_index[absolute_frame], object_id)
        geometries.append(_geometry(target))
        if target is None:
            target_presence.append("uncertain")
        elif bool(target.get("visible")):
            target_presence.append("visible")
        else:
            target_presence.append("hidden")
    if not any(value is not None for value in geometries):
        raise SegmentationValidationError(f"object_id {object_id} was never visible")
    keyframes: list[dict[str, Any]] = []
    for local_frame, (geometry, presence) in enumerate(
        zip(geometries, target_presence, strict=True)
    ):
        if geometry is None:
            keyframes.append(
                {
                    "frame": local_frame,
                    "source_frame": start_frame + local_frame,
                    "visibility": presence,
                    "x": None,
                    "y": None,
                    "width": None,
                    "height": None,
                    "scale": 1.0,
                    "rotation_degrees": 0.0,
                    "opacity": 0.0,
                    "easing": "linear",
                }
            )
            continue
        centroid_x, centroid_y, width, height = _smoothed_geometry(
            geometries, local_frame, window_radius_frames
        )
        keyframes.append(
            {
                "frame": local_frame,
                "source_frame": start_frame + local_frame,
                "visibility": "visible",
                "x": centroid_x - (timeline_width / 2),
                "y": centroid_y - (timeline_height / 2),
                "width": max(1.0, width * padding),
                "height": max(1.0, height * padding),
                "scale": 1.0,
                "rotation_degrees": 0.0,
                "opacity": 1.0,
                "easing": "linear",
            }
        )
    input_video = result.get("input_video")
    selected_rate = frame_rate
    if selected_rate is None and isinstance(input_video, Mapping):
        selected_rate = input_video.get("frame_rate")
    if selected_rate is None:
        selected_rate = "1/1"
    warnings = ["rotation_defaulted_to_zero_because_segmentation_has_no_orientation"]
    if any(item["opacity"] == 0 for item in keyframes):
        warnings.append("hidden_frames_use_original_shot_fallback")
    project_id = str(result.get("project_id") or review.get("project_id") or "track_keyframes")
    revision_id = str(result.get("revision_id") or review.get("revision_id") or "rev_001")
    return {
        "schema_name": "object_track_keyframes",
        "schema_version": TRACK_KEYFRAMES_SCHEMA_VERSION,
        "artifact_id": f"art_object_track_keyframes_{object_id}",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("object-track-keyframes", "core-tracking"),
        "source_result": _file_ref(result_path),
        "segmentation_validation": _file_ref(segmentation_validation_path),
        "track_review": _file_ref(track_review_path),
        "source_range": {"start_frame": start_frame, "end_frame": end_frame},
        "frame_rate": _frame_rate_text(selected_rate),
        "timeline": {"width": timeline_width, "height": timeline_height},
        "object_id": object_id,
        "smoothing": {
            "method": "centered_moving_average_visible_runs",
            "window_radius_frames": window_radius_frames,
            "padding": padding,
            "scale_source": "bbox_dimensions_unit_scale",
            "rotation_source": "unavailable_default_zero",
        },
        "keyframes": keyframes,
        "fallback": {"mode": "original_shot", "on_uncertain": "keep_original"},
        "status": "complete",
        "warnings": warnings,
    }


def write_object_track_keyframes(
    package_root: Path,
    result_path: Path,
    segmentation_validation_path: Path,
    track_review_path: Path,
    output_path: Path | None = None,
    *,
    timeline_width: int,
    timeline_height: int,
    object_id: int = 1,
    padding: float = 1.2,
    window_radius_frames: int = 1,
    frame_rate: object | None = None,
) -> Path:
    payload = build_object_track_keyframes_payload(
        package_root,
        result_path,
        segmentation_validation_path,
        track_review_path,
        timeline_width=timeline_width,
        timeline_height=timeline_height,
        object_id=object_id,
        padding=padding,
        window_radius_frames=window_radius_frames,
        frame_rate=frame_rate,
    )
    result_path = result_path.resolve()
    output = (
        output_path.resolve()
        if output_path is not None
        else result_path.parent
        / f"object-track-keyframes-{sha256_file(result_path)[:16]}-{object_id}.json"
    )
    if output.is_file():
        existing = _read_object(output, "existing object-track keyframes")
        validate_artifact(package_root, "object_track_keyframes", existing)
        for field in (
            "source_result",
            "segmentation_validation",
            "track_review",
            "source_range",
            "frame_rate",
            "timeline",
            "object_id",
            "smoothing",
            "keyframes",
        ):
            if existing.get(field) != payload.get(field):
                raise StaleApprovalError("object-track keyframe path contains a stale manifest")
        return output
    return write_validated_artifact(package_root, "object_track_keyframes", output, payload)


def _load_current_keyframe_manifest(
    package_root: Path,
    keyframes_path: Path,
    result_path: Path,
    segmentation_validation_path: Path,
    track_review_path: Path,
    *,
    object_id: int,
    timeline_width: int,
    timeline_height: int,
) -> dict[str, Any]:
    manifest = _read_object(keyframes_path.resolve(), "object-track keyframes")
    validate_artifact(package_root, "object_track_keyframes", manifest)
    if manifest.get("status") != "complete":
        raise SegmentationValidationError("object-track keyframes are not complete")
    _require_current_ref(
        manifest.get("source_result"), result_path, "object-track keyframes result"
    )
    _require_current_ref(
        manifest.get("segmentation_validation"),
        segmentation_validation_path,
        "object-track keyframes validation",
    )
    _require_current_ref(
        manifest.get("track_review"), track_review_path, "object-track keyframes review"
    )
    if int(manifest.get("object_id", -1)) != object_id:
        raise StaleApprovalError(
            "object-track keyframes object ID does not match the requested object"
        )
    timeline = manifest.get("timeline")
    if not isinstance(timeline, Mapping) or (
        int(timeline.get("width", -1)) != timeline_width
        or int(timeline.get("height", -1)) != timeline_height
    ):
        raise StaleApprovalError("object-track keyframes dimensions do not match the timeline")
    return manifest


def _target_object(frame: Mapping[str, Any], object_id: int) -> dict[str, Any] | None:
    objects = frame.get("objects", [])
    if not isinstance(objects, list):
        return None
    for item in objects:
        if isinstance(item, dict) and item.get("object_id") == object_id:
            return item
    return None


def tracked_image_layer(
    package_root: Path,
    result_path: Path,
    asset_src: str,
    *,
    timeline_width: int,
    timeline_height: int,
    segmentation_validation_path: Path | None = None,
    track_review_path: Path | None = None,
    keyframe_manifest_path: Path | None = None,
    object_id: int = 1,
    layer_id: str = "tracked-replacement",
    z_index: int = 30,
    padding: float = 1.2,
    window_radius_frames: int = 1,
    frame_rate: object | None = None,
    start_frame_offset: int = 0,
) -> ImageLayer:
    """Compile an explicitly approved, smoothed track into a Remotion image layer."""

    if timeline_width <= 0 or timeline_height <= 0:
        raise ValueError("timeline dimensions must be positive")
    if start_frame_offset < 0:
        raise ValueError("start_frame_offset must be nonnegative")
    if segmentation_validation_path is None or track_review_path is None:
        raise ApprovalRequiredError(
            "tracked image layers require current segmentation validation and "
            "an approved track review"
        )
    payload = (
        _load_current_keyframe_manifest(
            package_root,
            keyframe_manifest_path,
            result_path,
            segmentation_validation_path,
            track_review_path,
            object_id=object_id,
            timeline_width=timeline_width,
            timeline_height=timeline_height,
        )
        if keyframe_manifest_path is not None
        else build_object_track_keyframes_payload(
            package_root,
            result_path,
            segmentation_validation_path,
            track_review_path,
            timeline_width=timeline_width,
            timeline_height=timeline_height,
            object_id=object_id,
            padding=padding,
            window_radius_frames=window_radius_frames,
            frame_rate=frame_rate,
        )
    )
    manifest_range = payload["source_range"]
    first_frame = int(manifest_range["start_frame"])
    duration_frames = int(manifest_range["end_frame"]) - first_frame
    keyframes = [
        TransformKeyframe.model_validate(
            {
                key: item[key]
                for key in (
                    "frame",
                    "x",
                    "y",
                    "width",
                    "height",
                    "scale",
                    "rotation_degrees",
                    "opacity",
                    "easing",
                )
            }
        )
        for item in payload["keyframes"]
    ]
    first_item = next(
        (item for item in payload["keyframes"] if item["visibility"] == "visible"), None
    )
    if first_item is None:
        raise SegmentationValidationError(f"object_id {object_id} was never visible")
    first_visible = Transform.model_validate(
        {
            "x": first_item["x"],
            "y": first_item["y"],
            "width": first_item["width"],
            "height": first_item["height"],
            "scale": first_item["scale"],
            "rotation_degrees": first_item["rotation_degrees"],
            "opacity": first_item["opacity"],
        }
    )

    return ImageLayer(
        id=layer_id,
        start_frame=start_frame_offset + first_frame,
        duration_frames=duration_frames,
        z_index=z_index,
        src=asset_src,
        fit="contain",
        transform=first_visible,
        keyframes=keyframes,
    )


def append_tracked_image_layer(
    package_root: Path,
    timeline_path: Path,
    result_path: Path,
    asset_src: str,
    output_path: Path,
    *,
    segmentation_validation_path: Path | None = None,
    track_review_path: Path | None = None,
    keyframe_manifest_path: Path | None = None,
    object_id: int = 1,
    layer_id: str = "tracked-replacement",
    z_index: int = 30,
    padding: float = 1.2,
    window_radius_frames: int = 1,
    start_frame_offset: int = 0,
    asset_id: str | None = None,
    asset_sha256: str | None = None,
) -> Path:
    if (asset_id is None) != (asset_sha256 is None):
        raise ValueError("asset_id and asset_sha256 must be supplied together")
    timeline_payload = json.loads(timeline_path.read_text(encoding="utf-8"))
    timeline = TimelineSpec.model_validate(timeline_payload)
    layer = tracked_image_layer(
        package_root,
        result_path,
        asset_src,
        timeline_width=timeline.width,
        timeline_height=timeline.height,
        segmentation_validation_path=segmentation_validation_path,
        track_review_path=track_review_path,
        keyframe_manifest_path=keyframe_manifest_path,
        object_id=object_id,
        layer_id=layer_id,
        z_index=z_index,
        padding=padding,
        window_radius_frames=window_radius_frames,
        frame_rate=_frame_rate_text(timeline.fps),
        start_frame_offset=start_frame_offset,
    )
    assets = list(timeline.assets)
    if asset_id is not None and asset_sha256 is not None:
        assets.append(
            TimelineAssetRef(
                asset_id=asset_id,
                src=asset_src,
                sha256=asset_sha256,
                role="subject",
            )
        )
    updated = timeline.model_copy(update={"layers": [*timeline.layers, layer], "assets": assets})
    # model_copy does not re-run validators, so validate the serialized result again.
    validated = TimelineSpec.model_validate(updated.model_dump(mode="json"))
    return write_text_atomically(output_path, validated.model_dump_json(indent=2) + "\n")
