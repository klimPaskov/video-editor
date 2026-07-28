from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import ProcessResult
from videoedit.domain.models import TimelineAssetRef, TimelineSpec, VideoLayer
from videoedit.errors import (
    ApprovalRequiredError,
    ForegroundValidationError,
    OccluderValidationError,
    SegmentationValidationError,
    StaleApprovalError,
    VideoeditError,
)
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.foreground import (
    AlphaStatistics,
    parse_alpha_statistics,
    validate_foreground_output,
)
from videoedit.services.media import parse_rate
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segmentation import validate_segmentation_result
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)
from videoedit.services.tracking import _approved_track_inputs
from videoedit.services.visual_timeline import validate_visual_timeline

OCCLUDER_IMPLEMENTATION_VERSION = f"{__version__}:occluder-v1"
OCCLUDER_SCHEMA_VERSION = "1.0.0"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OccluderValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise OccluderValidationError(f"{label} must be a JSON object: {path}")
    return value


def _file_ref(path: Path, label: str = "file") -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise OccluderValidationError(f"{label} is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _require_current_ref(reference: object, path: Path, label: str) -> None:
    if not isinstance(reference, Mapping):
        raise StaleApprovalError(f"{label} reference is missing")
    current = _file_ref(path, label)
    if any(reference.get(name) != current[name] for name in ("path", "sha256", "size_bytes")):
        raise StaleApprovalError(f"{label} is stale for the current file")


def _project_path(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise OccluderValidationError(f"{label} escapes the project: {resolved}") from exc
    return resolved


def _safe_output_path(layout: ProjectLayout, output: Path) -> Path:
    resolved = _project_path(layout, output, "occluder output")
    try:
        resolved.relative_to(layout.raw.resolve())
    except ValueError:
        return resolved
    raise OccluderValidationError("occluder output must not be written under raw sources")


def _promote_media(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != sha256_file(staged):
            raise OccluderValidationError(
                f"immutable occluder target already exists with another hash: {target}"
            )
        staged.unlink(missing_ok=True)
        return
    os.replace(staged, target)


def _first_video(probe: Mapping[str, Any], label: str) -> dict[str, Any]:
    streams = probe.get("streams", [])
    if isinstance(streams, list):
        for item in streams:
            if isinstance(item, dict) and item.get("codec_type") == "video":
                return item
    raise OccluderValidationError(f"{label} has no video stream")


def _rate_from_stream(stream: Mapping[str, Any], label: str) -> dict[str, int]:
    rate = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate"))
    if rate is None:
        raise OccluderValidationError(f"{label} has no rational frame rate")
    return rate


def _rate_equal(left: Mapping[str, int], right: Mapping[str, int]) -> bool:
    return int(left.get("numerator", 0)) == int(right.get("numerator", -1)) and int(
        left.get("denominator", 0)
    ) == int(right.get("denominator", -1))


def _frame_range(payload: Mapping[str, Any]) -> tuple[int, int]:
    value = payload.get("source_range")
    if not isinstance(value, Mapping):
        raise SegmentationValidationError("occluder track has no source range")
    start_frame = int(value.get("start_frame", -1))
    end_frame = int(value.get("end_frame", -1))
    if start_frame < 0 or end_frame <= start_frame:
        raise SegmentationValidationError("occluder source range must be nonempty and half-open")
    return start_frame, end_frame


def _command_record(result: ProcessResult, working_directory: Path, version: str) -> dict[str, Any]:
    arguments = result.arguments or ("unknown",)
    return {
        "executable": arguments[0],
        "arguments": list(arguments[1:]),
        "working_directory": str(working_directory.resolve()),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "version": version or "unknown",
    }


def _stage_file_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
        return False
    path = Path(str(value["path"])).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError:
        return False
    try:
        return (
            path.is_file()
            and path.stat().st_size == int(value.get("size_bytes", -1))
            and sha256_file(path) == value.get("sha256")
        )
    except (OSError, TypeError, ValueError):
        return False


def _cached_occluder(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, Any] | None,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping) or not all(
        name in artifacts for name in ("occluder_manifest", "output_media")
    ):
        return None
    if not all(_stage_file_ref_valid(layout, value) for value in artifacts.values()):
        return None
    manifest_path = Path(str(artifacts["occluder_manifest"]["path"])).resolve()
    try:
        payload = _read_object(manifest_path, "cached occluder manifest")
        validate_artifact(package_root, "occluder_manifest", payload)
    except (OSError, ValueError, KeyError, OccluderValidationError):
        return None
    if payload.get("status") != "complete":
        return None
    return manifest_path


def _source_from_result(result: Mapping[str, Any], result_path: Path) -> tuple[Path, str]:
    input_value = result.get("input")
    if isinstance(input_value, Mapping):
        path_value = input_value.get("path")
        hash_value = input_value.get("sha256")
    else:
        path_value = result.get("input_path")
        hash_value = result.get("input_sha256")
    if not isinstance(path_value, str) or not path_value:
        raise SegmentationValidationError("segmentation result has no source path")
    source = Path(path_value).expanduser()
    if not source.is_absolute():
        source = (result_path.parent / source).resolve()
    else:
        source = source.resolve()
    if not isinstance(hash_value, str) or len(hash_value) != 64:
        raise SegmentationValidationError("segmentation result has no source SHA-256")
    return source, hash_value


def _target_object(frame: Mapping[str, Any], object_id: int) -> Mapping[str, Any] | None:
    objects = frame.get("objects", [])
    if not isinstance(objects, list):
        return None
    for item in objects:
        if isinstance(item, Mapping) and int(item.get("object_id", -1)) == object_id:
            return item
    return None


def _validate_mask_file(
    adapter: FFmpegAdapter,
    path: Path,
    *,
    width: int,
    height: int,
    frame_index: int,
) -> None:
    if not path.is_file():
        raise SegmentationValidationError(
            f"occluder mask is missing at frame {frame_index}: {path}"
        )
    probe = adapter.probe(path)
    stream = _first_video(probe, f"occluder mask at frame {frame_index}")
    if int(stream.get("width") or 0) != width or int(stream.get("height") or 0) != height:
        raise SegmentationValidationError(
            f"occluder mask dimensions do not match source at frame {frame_index}"
        )
    if str(stream.get("pix_fmt") or "") not in {"gray", "gray8"}:
        raise SegmentationValidationError(
            f"occluder mask is not lossless gray8 at frame {frame_index}"
        )
    decoded = adapter.full_decode_check(path)
    if decoded.exit_code != 0:
        raise SegmentationValidationError(f"occluder mask failed to decode at frame {frame_index}")


def _selected_mask_frames(
    layout: ProjectLayout,
    result_path: Path,
    result: Mapping[str, Any],
    *,
    object_id: int,
    width: int,
    height: int,
    adapter: FFmpegAdapter,
    start_frame: int,
    end_frame: int,
) -> list[dict[str, Any]]:
    frames_value = result.get("frames")
    if not isinstance(frames_value, list):
        raise SegmentationValidationError("segmentation result frames must be an array")
    frames = [item for item in frames_value if isinstance(item, Mapping)]
    by_index = {int(item.get("frame_index", -1)): item for item in frames}
    selected: list[dict[str, Any]] = []
    for frame_index in range(start_frame, end_frame):
        frame = by_index.get(frame_index)
        if frame is None:
            raise SegmentationValidationError(f"occluder track is missing frame {frame_index}")
        target = _target_object(frame, object_id)
        if target is None:
            raise SegmentationValidationError(
                f"occluder track has no object {object_id} at frame {frame_index}"
            )
        mask_value = target.get("mask_path")
        expected_hash = target.get("mask_sha256")
        if not isinstance(mask_value, str) or not mask_value:
            raise SegmentationValidationError(
                f"occluder track has no mask path at frame {frame_index}"
            )
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise SegmentationValidationError(
                f"occluder track mask hash is missing at frame {frame_index}"
            )
        mask_path = Path(mask_value).expanduser()
        if not mask_path.is_absolute():
            mask_path = (result_path.parent / mask_path).resolve()
        else:
            mask_path = mask_path.resolve()
        try:
            mask_path.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise SegmentationValidationError(
                f"occluder mask escapes the project at frame {frame_index}"
            ) from exc
        if not mask_path.is_file() or sha256_file(mask_path) != expected_hash:
            raise StaleApprovalError(f"occluder mask hash is stale at frame {frame_index}")
        _validate_mask_file(
            adapter,
            mask_path,
            width=width,
            height=height,
            frame_index=frame_index,
        )
        selected.append(
            {
                "frame_index": frame_index,
                "visible": bool(target.get("visible")),
                "source": _file_ref(mask_path, f"occluder mask at frame {frame_index}"),
                "path": mask_path,
            }
        )
    return selected


def _copy_mask_sequence(
    selected: Sequence[Mapping[str, Any]],
    output_directory: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    output_directory.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for item in selected:
        frame_index = int(item["frame_index"])
        source = Path(str(item["path"])).resolve()
        target = output_directory / f"{frame_index:06d}.png"
        shutil.copyfile(source, target)
        if sha256_file(target) != str(item["source"]["sha256"]):
            raise OccluderValidationError(
                f"occluder mask changed while staging at frame {frame_index}"
            )
        records.append(
            {
                "frame_index": frame_index,
                "visible": bool(item["visible"]),
                "source": dict(item["source"]),
                "staged": _file_ref(target, f"staged occluder mask at frame {frame_index}"),
            }
        )
    return output_directory / "%06d.png", records


def _manifest_output_path(
    layout: ProjectLayout,
    output: Path | None,
    stage_key: str,
    revision_id: str,
) -> Path:
    if output is not None:
        return _safe_output_path(layout, output)
    return layout.work / "occluder" / revision_id / stage_key / "occluder.mov"


def render_tracked_occluder(
    package_root: Path,
    layout: ProjectLayout,
    result_path: Path,
    segmentation_validation_path: Path,
    track_review_path: Path,
    output: Path | None = None,
    *,
    manifest_output: Path | None = None,
    object_id: int = 1,
    layer_id: str = "tracked-occluder",
    z_index: int = 40,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Render a reviewed object mask as an alpha-bearing front-layer clip."""

    if object_id < 1:
        raise ValueError("object_id must be positive")
    if not layer_id.strip():
        raise ValueError("occluder layer_id must not be empty")
    result_path = _project_path(layout, result_path, "segmentation result")
    validation_path = _project_path(layout, segmentation_validation_path, "segmentation validation")
    review_path = _project_path(layout, track_review_path, "track review")
    selected_adapter = adapter or FFmpegAdapter()
    result, validation, review, (start_frame, end_frame) = _approved_track_inputs(
        package_root,
        result_path,
        validation_path,
        review_path,
        object_id,
    )
    if validation.get("status") != "pass" or review.get("decision") != "approved":
        raise ApprovalRequiredError("occluder rendering requires a passing approved track")
    source, source_hash = _source_from_result(result, result_path)
    source = _project_path(layout, source, "occluder source")
    if not source.is_file() or sha256_file(source) != source_hash:
        raise StaleApprovalError("occluder source hash does not match the reviewed result")
    _require_current_ref(validation.get("source"), source, "segmentation validation source")
    structural = validate_segmentation_result(
        package_root,
        result_path,
        adapter=selected_adapter,
        verify_files=True,
    )
    if structural.status != "pass":
        raise SegmentationValidationError(
            "occluder requires a passing structural segmentation validation, "
            f"got {structural.status}"
        )
    input_video = result.get("input_video")
    if not isinstance(input_video, Mapping):
        raise SegmentationValidationError("segmentation result has no input video metadata")
    source_probe = selected_adapter.probe(source)
    source_video = _first_video(source_probe, "occluder source")
    source_width = int(source_video.get("width") or 0)
    source_height = int(source_video.get("height") or 0)
    source_rate = _rate_from_stream(source_video, "occluder source")
    source_frame_count = selected_adapter.probe_frame_count(source)
    if source_frame_count is None or source_frame_count <= 0:
        raise SegmentationValidationError("occluder source has no decoded frame count")
    declared_rate = parse_rate(input_video.get("frame_rate"))
    if declared_rate is None or not _rate_equal(source_rate, declared_rate):
        raise StaleApprovalError("occluder source frame rate differs from the reviewed result")
    if source_width != int(input_video.get("width") or 0) or source_height != int(
        input_video.get("height") or 0
    ):
        raise StaleApprovalError("occluder source dimensions differ from the reviewed result")
    if end_frame > source_frame_count:
        raise SegmentationValidationError("occluder source range exceeds decoded source frames")
    selected_masks = _selected_mask_frames(
        layout,
        result_path,
        result,
        object_id=object_id,
        width=source_width,
        height=source_height,
        adapter=selected_adapter,
        start_frame=start_frame,
        end_frame=end_frame,
    )
    source_range_count = end_frame - start_frame
    adapter_version = str(selected_adapter.version()) or "unknown"
    stage_key = make_stage_key(
        "tracked_occluder",
        OCCLUDER_IMPLEMENTATION_VERSION,
        [
            source_hash,
            sha256_file(result_path),
            sha256_file(validation_path),
            sha256_file(review_path),
        ],
        {
            "object_id": object_id,
            "layer_id": layer_id,
            "z_index": z_index,
            "revision_id": revision_id,
            "adapter_version": adapter_version,
            "source_range": {"start_frame": start_frame, "end_frame": end_frame},
            "requested_output": str(output.expanduser().resolve()) if output else None,
            "requested_manifest_output": (
                str(manifest_output.expanduser().resolve()) if manifest_output else None
            ),
        },
    )
    final_output = _manifest_output_path(layout, output, stage_key, revision_id).resolve()
    if final_output == source:
        raise OccluderValidationError("occluder output must differ from its source")
    stage_name = "tracked_occluder"
    with ProjectLock(layout, stage=stage_name, revision_id=revision_id):
        previous = load_stage_state(package_root, layout, stage_name, revision_id)
        cached = _cached_occluder(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"occluder-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage=stage_name,
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        staged_source = stage_dir / "source-range.mkv"
        staged_mask_pattern = stage_dir / "masks" / "%06d.png"
        mask_records: list[dict[str, Any]] = []
        staged_mask = stage_dir / "mask-sequence.mkv"
        staged_output = stage_dir / "occluder.part.mov"
        manifest_path = (
            _safe_output_path(layout, manifest_output)
            if manifest_output is not None
            else layout.artifacts / f"occluder-{stage_key[:16]}.json"
        )
        commands: list[dict[str, Any]] = []
        try:
            staged_mask_pattern, mask_records = _copy_mask_sequence(
                selected_masks,
                stage_dir / "masks",
            )
            extraction = selected_adapter.extract_video_frame_range(
                source,
                staged_source,
                start_frame=start_frame,
                end_frame=end_frame,
            )
            commands.append(_command_record(extraction, stage_dir, adapter_version))
            source_range_probe = selected_adapter.probe(staged_source)
            source_range_video = _first_video(source_range_probe, "occluder source range")
            source_range_frame_count = selected_adapter.probe_frame_count(staged_source)
            if source_range_frame_count != source_range_count:
                raise SegmentationValidationError(
                    "decoded occluder source range frame count does not match the approved range"
                )
            if (
                int(source_range_video.get("width") or 0) != source_width
                or int(source_range_video.get("height") or 0) != source_height
                or not _rate_equal(
                    _rate_from_stream(source_range_video, "occluder source range"), source_rate
                )
            ):
                raise SegmentationValidationError(
                    "decoded occluder source range changed dimensions or frame rate"
                )
            encoding = selected_adapter.encode_mask_sequence(
                staged_mask_pattern,
                staged_mask,
                fps=f"{source_rate['numerator']}/{source_rate['denominator']}",
                start_number=start_frame,
                frame_count=source_range_count,
            )
            commands.append(_command_record(encoding, stage_dir, adapter_version))
            mask_probe = selected_adapter.probe(staged_mask)
            mask_video = _first_video(mask_probe, "encoded occluder mask")
            mask_count = selected_adapter.probe_frame_count(staged_mask)
            if (
                int(mask_video.get("width") or 0) != source_width
                or int(mask_video.get("height") or 0) != source_height
                or str(mask_video.get("pix_fmt") or "") not in {"gray", "gray8"}
                or mask_count != source_range_count
                or not _rate_equal(
                    _rate_from_stream(mask_video, "encoded occluder mask"), source_rate
                )
            ):
                raise SegmentationValidationError(
                    "encoded occluder mask does not match the approved source range"
                )
            mask_decode = selected_adapter.full_decode_check(staged_mask)
            commands.append(_command_record(mask_decode, stage_dir, adapter_version))
            if mask_decode.exit_code != 0:
                raise SegmentationValidationError("encoded occluder mask failed full decode")
            attached = selected_adapter.attach_alpha(staged_source, staged_mask, staged_output)
            commands.append(_command_record(attached, stage_dir, adapter_version))
            output_probe = selected_adapter.probe(staged_output)
            _first_video(output_probe, "tracked occluder output")
            if any(
                isinstance(item, dict) and item.get("codec_type") == "audio"
                for item in output_probe.get("streams", [])
                if isinstance(item, dict)
            ):
                raise ForegroundValidationError("tracked occluder output must not contain audio")
            output_frame_count = selected_adapter.probe_frame_count(staged_output)
            if output_frame_count is None or output_frame_count <= 0:
                raise ForegroundValidationError("tracked occluder output has no decoded frames")
            alpha_samples: list[AlphaStatistics] = []
            for frame_index in sorted({0, source_range_count // 2, source_range_count - 1}):
                measured = selected_adapter.measure_alpha(staged_output, frame_index=frame_index)
                commands.append(_command_record(measured, stage_dir, adapter_version))
                alpha_samples.append(parse_alpha_statistics(measured))
            decoded = selected_adapter.full_decode_check(staged_output)
            commands.append(_command_record(decoded, stage_dir, adapter_version))
            validation_result = validate_foreground_output(
                source_range_probe,
                output_probe,
                source_frame_count=source_range_frame_count,
                output_frame_count=output_frame_count,
                alpha_samples=alpha_samples,
                full_decode_ok=decoded.exit_code == 0,
            )
            if not validation_result.is_valid:
                failed = [
                    name
                    for name, status in validation_result.validation.items()
                    if status != "pass"
                ]
                if validation_result.alpha.get("polarity") != "mixed":
                    failed.append(
                        f"alpha_polarity={validation_result.alpha.get('polarity', 'unknown')}"
                    )
                raise ForegroundValidationError(
                    "tracked occluder alpha validation failed: " + ", ".join(failed)
                )
            _promote_media(staged_output, final_output)
            project_id = str(result.get("project_id") or layout.root.name)
            payload: dict[str, Any] = {
                "schema_name": "occluder_manifest",
                "schema_version": OCCLUDER_SCHEMA_VERSION,
                "artifact_id": f"art_object_occluder_{object_id}",
                "project_id": project_id,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("object-occluder", "ffmpeg", adapter_version),
                "inputs": [
                    artifact_input("art_source", source),
                    artifact_input("art_segmentation_result", result_path),
                    artifact_input("art_segmentation_validation", validation_path),
                    artifact_input("art_object_track_review", review_path),
                ],
                "status": "complete",
                "source": _file_ref(source, "occluder source"),
                "source_range": {"start_frame": start_frame, "end_frame": end_frame},
                "track": {
                    "source_result": _file_ref(result_path, "segmentation result"),
                    "segmentation_validation": _file_ref(
                        validation_path, "segmentation validation"
                    ),
                    "review": _file_ref(review_path, "track review"),
                    "object_id": object_id,
                    "decision": "approved",
                    "findings": dict(review.get("findings", {})),
                },
                "mask": {
                    "pattern": str(staged_mask_pattern),
                    "encoding": "png_gray8",
                    "polarity": "white_foreground",
                    "frames": mask_records,
                },
                "output": _file_ref(final_output, "tracked occluder output"),
                "video": validation_result.video,
                "alpha": validation_result.alpha,
                "validation": {
                    "source_identity": "pass",
                    "source_range": "pass",
                    "track_review": "pass",
                    "mask_sequence": "pass",
                    **validation_result.validation,
                    "alpha_polarity": "pass",
                },
                "layer": {
                    "layer_id": layer_id,
                    "asset_id": f"asset_{layer_id}",
                    "role": "front",
                    "z_index": z_index,
                    "start_frame": start_frame,
                    "duration_frames": source_range_count,
                },
                "fallback": {"mode": "original_shot", "on_uncertain": "keep_original"},
                "commands": commands,
                "warnings": [
                    "transparent_video_is_consumed only after alpha and frame alignment checks",
                    "original_shot_fallback_remains_when_track_or_alpha_review_is_stale",
                    *validation_result.warnings,
                ],
            }
            validate_artifact(package_root, "occluder_manifest", payload)
            write_validated_artifact(package_root, "occluder_manifest", manifest_path, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"occluder_manifest": manifest_path, "output_media": final_output},
                warnings=list(dict.fromkeys(payload["warnings"])),
            )
            return manifest_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="occluder_failed", message=message)
            raise OccluderValidationError(message) from exc


def append_occluder_video_layer(
    package_root: Path,
    timeline_path: Path,
    manifest_path: Path,
    asset_src: str,
    output_path: Path,
    *,
    asset_sha256: str | None = None,
) -> Path:
    """Append a validated transparent occluder as an explicit front layer."""

    manifest_path = manifest_path.resolve()
    manifest = _read_object(manifest_path, "occluder manifest")
    validate_artifact(package_root, "occluder_manifest", manifest)
    if manifest.get("status") != "complete":
        raise OccluderValidationError("occluder manifest is not complete")
    output_ref = manifest.get("output")
    if not isinstance(output_ref, Mapping):
        raise OccluderValidationError("occluder manifest has no output reference")
    output_media = Path(str(output_ref["path"])).resolve()
    _require_current_ref(output_ref, output_media, "occluder output")
    track = manifest.get("track")
    if not isinstance(track, Mapping):
        raise OccluderValidationError("occluder manifest has no track references")
    for field, label in (
        ("source_result", "segmentation result"),
        ("segmentation_validation", "segmentation validation"),
        ("review", "track review"),
    ):
        reference = track.get(field)
        if not isinstance(reference, Mapping):
            raise StaleApprovalError(f"occluder {label} reference is missing")
        _require_current_ref(reference, Path(str(reference["path"])), label)
    timeline_payload = json.loads(timeline_path.resolve().read_text(encoding="utf-8"))
    timeline = TimelineSpec.model_validate(timeline_payload)
    video = manifest.get("video")
    layer_data = manifest.get("layer")
    source_range = manifest.get("source_range")
    if not isinstance(video, Mapping) or not isinstance(layer_data, Mapping):
        raise OccluderValidationError("occluder manifest is missing video or layer metadata")
    if not isinstance(source_range, Mapping):
        raise OccluderValidationError("occluder manifest is missing source range")
    timeline_rate = timeline.fps
    if isinstance(timeline_rate, int):
        timeline_rate_value = {"numerator": timeline_rate, "denominator": 1}
    else:
        timeline_rate_value = {
            "numerator": timeline_rate.numerator,
            "denominator": timeline_rate.denominator,
        }
    manifest_rate = video.get("frame_rate")
    if not isinstance(manifest_rate, Mapping) or not _rate_equal(
        timeline_rate_value, manifest_rate
    ):
        raise StaleApprovalError("occluder frame rate does not match the visual timeline")
    if timeline.width != int(video.get("width") or 0) or timeline.height != int(
        video.get("height") or 0
    ):
        raise StaleApprovalError("occluder dimensions do not match the visual timeline")
    start_frame = int(layer_data.get("start_frame", -1))
    duration_frames = int(layer_data.get("duration_frames", 0))
    if (
        start_frame < 0
        or duration_frames <= 0
        or start_frame + duration_frames > timeline.duration_frames
    ):
        raise OccluderValidationError("occluder layer exceeds the visual timeline")
    if start_frame != int(source_range.get("start_frame", -1)):
        raise StaleApprovalError("occluder layer start is stale for the source range")
    if str(layer_data.get("role")) != "front":
        raise OccluderValidationError("occluder layer must have front role")
    layer_id = str(layer_data.get("layer_id", ""))
    asset_id = str(layer_data.get("asset_id", ""))
    if not layer_id or not asset_id:
        raise OccluderValidationError("occluder layer and asset IDs must not be empty")
    expected_hash = str(output_ref.get("sha256"))
    if asset_sha256 is not None and asset_sha256 != expected_hash:
        raise StaleApprovalError("staged occluder asset hash does not match its manifest")
    layer = VideoLayer(
        id=layer_id,
        start_frame=start_frame,
        duration_frames=duration_frames,
        z_index=int(layer_data.get("z_index", 0)),
        role="front",
        src=asset_src,
        source_from_frame=0,
        volume=0,
        muted=True,
        transparent=True,
        fit="fill",
    )
    layers = list(timeline.layers)
    existing = next((item for item in layers if item.id == layer_id), None)
    if existing is not None:
        if not isinstance(existing, VideoLayer) or existing.model_dump(
            mode="json"
        ) != layer.model_dump(mode="json"):
            raise StaleApprovalError(
                f"timeline already contains a different occluder layer: {layer_id}"
            )
    else:
        layers.append(layer)
    assets = list(timeline.assets)
    existing_asset = next((item for item in assets if item.asset_id == asset_id), None)
    asset_ref = TimelineAssetRef(
        asset_id=asset_id,
        src=asset_src,
        sha256=expected_hash,
        role="front",
    )
    if existing_asset is not None:
        if existing_asset.model_dump(mode="json") != asset_ref.model_dump(mode="json"):
            raise StaleApprovalError(
                f"timeline already contains a different occluder asset: {asset_id}"
            )
    else:
        assets.append(asset_ref)
    updated = timeline.model_copy(update={"layers": layers, "assets": assets})
    validated = TimelineSpec.model_validate(updated.model_dump(mode="json"))
    validate_visual_timeline(package_root, validated.model_dump(mode="json"))
    return write_text_atomically(output_path, validated.model_dump_json(indent=2) + "\n")
