from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import ApprovalRequiredError, SegmentationValidationError
from videoedit.services.artifacts import (
    config_sha256,
    now_iso,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.worker_runtime import validate_worker_runtime_approval

SEGMENTATION_CONTRACT_VERSION = "1.1"
SEGMENTATION_IMPLEMENTATION_VERSION = f"{__version__}:segmentation-v1"
SAM3_REPOSITORY = "https://github.com/facebookresearch/sam3"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameRange(_StrictModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> FrameRange:
        if self.end_frame <= self.start_frame:
            raise ValueError("segmentation source range must be half-open and non-empty")
        return self


class SegmentationPrompt(_StrictModel):
    type: Literal["text", "point", "box", "mask"] = "text"
    frame_index: int = Field(ge=0)
    text: str | None = None
    points: list[tuple[float, float]] | None = None
    point_labels: list[int] | None = None
    box_xywh: tuple[float, float, float, float] | None = None
    mask_path: str | None = None
    object_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_prompt_payload(self) -> SegmentationPrompt:
        if self.type == "text" and not self.text:
            raise ValueError("text prompt requires prompt.text")
        if self.type == "point":
            if not self.points:
                raise ValueError("point prompt requires prompt.points")
            if self.point_labels is None or len(self.point_labels) != len(self.points):
                raise ValueError("point prompt labels must align with points")
            if any(label not in (0, 1) for label in self.point_labels):
                raise ValueError("point prompt labels must be 0 or 1")
            if any(not 0 <= x <= 1 or not 0 <= y <= 1 for x, y in self.points):
                raise ValueError("point prompt coordinates must be normalized to [0, 1]")
        if self.type == "box":
            if self.box_xywh is None or self.box_xywh[2] <= 0 or self.box_xywh[3] <= 0:
                raise ValueError("box prompt requires positive box_xywh")
        if self.type == "mask" and not self.mask_path:
            raise ValueError("mask prompt requires prompt.mask_path")
        return self


class InputVideo(_StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(ge=0)
    frame_rate: str = Field(min_length=3)
    duration_us: int = Field(default=0, ge=0)


class OutputContract(_StrictModel):
    mask_format: Literal["png_gray8"] = "png_gray8"
    lossless: Literal[True] = True
    polarity: Literal["white_foreground"] = "white_foreground"


class ApprovalRef(_StrictModel):
    artifact_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuntimeApprovalRef(_StrictModel):
    artifact_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerRef(_StrictModel):
    name: Literal["sam3"] = "sam3"
    contract_version: str = SEGMENTATION_CONTRACT_VERSION
    implementation_version: str = SEGMENTATION_IMPLEMENTATION_VERSION


class RuntimeRef(_StrictModel):
    upstream_repository: str = SAM3_REPOSITORY
    upstream_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    checkpoint_id: str | None = None
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checkpoint_path: str | None = None
    python: str = "3.12"
    pytorch: str = ""
    cuda: str = ""
    device: str = ""
    access: Literal["pending", "approved", "blocked"] = "blocked"
    runtime_approval: RuntimeApprovalRef | None = None

    @model_validator(mode="after")
    def validate_upstream(self) -> RuntimeRef:
        if self.upstream_repository != SAM3_REPOSITORY:
            raise ValueError("SAM 3.1 runtime must use the official upstream repository")
        if self.access == "approved" and (
            not self.upstream_commit
            or not self.checkpoint_id
            or not self.checkpoint_sha256
            or not self.checkpoint_path
            or self.runtime_approval is None
        ):
            raise ValueError(
                "approved SAM 3.1 runtime requires pinned code, checkpoint, and runtime approval"
            )
        return self


class SegmentationJob(_StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    job_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=3)
    revision_id: str = Field(pattern=r"^rev_[0-9]{3,}$")
    input_path: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input: dict[str, Any]
    input_video: InputVideo
    output_dir: str = Field(min_length=1)
    source_range: FrameRange
    prompt: SegmentationPrompt
    expected_object_count: int = Field(ge=1)
    output_contract: OutputContract = Field(default_factory=OutputContract)
    approval: ApprovalRef
    worker: WorkerRef = Field(default_factory=WorkerRef)
    runtime: RuntimeRef
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    gpus: list[int] | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> SegmentationJob:
        if self.input.get("path") != self.input_path:
            raise ValueError("input.path must match input_path")
        if self.input.get("sha256") != self.input_sha256:
            raise ValueError("input.sha256 must match input_sha256")
        if self.start_frame != self.source_range.start_frame:
            raise ValueError("start_frame must match source_range.start_frame")
        if self.end_frame != self.source_range.end_frame:
            raise ValueError("end_frame must match source_range.end_frame")
        if (
            not self.source_range.start_frame
            <= self.prompt.frame_index
            < self.source_range.end_frame
        ):
            raise ValueError("prompt.frame_index must be inside the approved source range")
        if self.end_frame > self.input_video.frame_count:
            raise ValueError("source range exceeds input frame count")
        if self.gpus is not None and (
            any(gpu < 0 for gpu in self.gpus) or len(set(self.gpus)) != len(self.gpus)
        ):
            raise ValueError("gpus must contain unique nonnegative device indices")
        return self


@dataclass(frozen=True, slots=True)
class SegmentationValidation:
    is_valid: bool
    status: Literal["pass", "warning", "fail"]
    validation: dict[str, str]
    diagnostics: dict[str, list[int] | list[str]]
    review_frame_indices: list[int]
    warnings: list[str]


def _owned_path(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise SegmentationValidationError(f"{label} escapes project root: {resolved}") from exc
    return resolved


def _rational(value: object) -> str:
    text = str(value or "")
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            if int(numerator) > 0 and int(denominator) > 0:
                return f"{int(numerator)}/{int(denominator)}"
        except ValueError:
            pass
    try:
        number = float(text)
    except (TypeError, ValueError):
        return "0/1"
    if number <= 0:
        return "0/1"
    return f"{number:g}/1"


def _video_metadata(probe: Mapping[str, Any], frame_count: int) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if not isinstance(video, dict):
        raise SegmentationValidationError("source has no video stream")
    duration_value = (
        probe.get("format", {}).get("duration") if isinstance(probe.get("format"), dict) else None
    )
    duration_us = seconds_to_us(duration_value) or 0
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "frame_count": frame_count,
        "frame_rate": _rational(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "duration_us": duration_us,
    }


def build_segmentation_job(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    *,
    job_id: str,
    prompt: Mapping[str, Any],
    start_frame: int,
    end_frame: int,
    approval: Mapping[str, Any],
    upstream_commit: str | None,
    checkpoint_id: str | None,
    checkpoint_sha256: str | None = None,
    checkpoint_path: Path | None = None,
    pytorch: str = "",
    cuda: str = "",
    device: str = "",
    runtime_approval_path: Path | None = None,
    expected_object_count: int = 1,
    output_dir: Path | None = None,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> dict[str, Any]:
    source = _owned_path(layout, source, "segmentation source")
    if not source.is_file():
        raise SegmentationValidationError(f"segmentation source is missing: {source}")
    if start_frame < 0 or end_frame <= start_frame:
        raise SegmentationValidationError("segmentation range must be a non-empty half-open range")
    if expected_object_count <= 0:
        raise SegmentationValidationError("expected_object_count must be positive")
    if not isinstance(approval, Mapping) or not approval.get("artifact_id"):
        raise ApprovalRequiredError("SAM segmentation requires an approved Gate 1 effect reference")
    selected = adapter or FFmpegAdapter()
    probe = selected.probe(source)
    frame_count = selected.probe_frame_count(source)
    if frame_count is None:
        raise SegmentationValidationError("source frame count could not be determined")
    video = _video_metadata(probe, frame_count)
    if end_frame > frame_count:
        raise SegmentationValidationError("segmentation range exceeds source frame count")
    prompt_model = SegmentationPrompt.model_validate(dict(prompt))
    if not start_frame <= prompt_model.frame_index < end_frame:
        raise SegmentationValidationError("prompt frame must be inside the approved source range")
    source_ref: dict[str, Any] = {
        "path": str(source),
        "sha256": sha256_file(source),
        "size_bytes": source.stat().st_size,
    }
    selected_output = output_dir or layout.work / "sam3" / job_id
    selected_output = _owned_path(layout, selected_output, "segmentation output")
    checkpoint_resolved = checkpoint_path.expanduser().resolve() if checkpoint_path else None
    if checkpoint_resolved is not None and not checkpoint_resolved.is_file():
        raise SegmentationValidationError(f"declared checkpoint is missing: {checkpoint_resolved}")
    if checkpoint_resolved is not None and checkpoint_sha256 != sha256_file(checkpoint_resolved):
        raise SegmentationValidationError("declared checkpoint SHA-256 does not match its file")
    runtime_approval_ref: dict[str, str] | None = None
    if runtime_approval_path is not None:
        runtime_approval_ref = validate_worker_runtime_approval(
            package_root,
            layout,
            runtime_approval_path,
            worker="sam3",
            upstream_commit=upstream_commit or "",
            checkpoint_id=checkpoint_id or "",
            checkpoint_sha256=checkpoint_sha256 or "",
            pytorch=pytorch,
            cuda=cuda,
            device=device,
            revision_id=revision_id,
        )
    runtime = RuntimeRef(
        upstream_commit=upstream_commit,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_path=str(checkpoint_resolved) if checkpoint_resolved else None,
        pytorch=pytorch,
        cuda=cuda,
        device=device,
        runtime_approval=(
            RuntimeApprovalRef.model_validate(runtime_approval_ref)
            if runtime_approval_ref is not None
            else None
        ),
        access=(
            "approved"
            if (
                checkpoint_resolved
                and checkpoint_sha256
                and checkpoint_id
                and upstream_commit
                and runtime_approval_ref
            )
            else "blocked"
        ),
    )
    job = SegmentationJob(
        job_id=job_id,
        config_sha256=config_sha256(layout),
        project_id=layout.root.name,
        revision_id=revision_id,
        input_path=str(source),
        input_sha256=source_ref["sha256"],
        input=source_ref,
        input_video=InputVideo.model_validate(video),
        output_dir=str(selected_output),
        source_range=FrameRange(start_frame=start_frame, end_frame=end_frame),
        prompt=prompt_model,
        expected_object_count=expected_object_count,
        approval=ApprovalRef.model_validate(dict(approval)),
        runtime=runtime,
        start_frame=start_frame,
        end_frame=end_frame,
        gpus=[0],
    )
    payload = job.model_dump(mode="json")
    payload["prompt"] = job.prompt.model_dump(mode="json", exclude_none=True)
    payload["schema_version"] = SEGMENTATION_CONTRACT_VERSION
    payload["config_sha256"] = config_sha256(layout)
    validate_artifact(package_root, "segmentation_job", payload)
    return payload


def validate_segmentation_job(
    package_root: Path,
    payload: Mapping[str, Any],
    *,
    verify_files: bool = True,
) -> SegmentationJob | None:
    value = dict(payload)
    validate_artifact(package_root, "segmentation_job", value)
    if value.get("schema_version") == "1.0":
        return None
    try:
        job = SegmentationJob.model_validate(value)
    except ValueError as exc:
        raise SegmentationValidationError(f"invalid SAM segmentation job: {exc}") from exc
    if verify_files:
        source = Path(job.input_path)
        if not source.is_file() or sha256_file(source) != job.input_sha256:
            raise SegmentationValidationError("segmentation job input hash does not match its file")
        checkpoint_path = job.runtime.checkpoint_path
        if checkpoint_path and job.runtime.checkpoint_sha256:
            checkpoint = Path(checkpoint_path)
            if not checkpoint.is_file() or sha256_file(checkpoint) != job.runtime.checkpoint_sha256:
                raise SegmentationValidationError("segmentation checkpoint hash cannot be verified")
    return job


def _file_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _path_within(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def _result_output_roots(result_path: Path, job: Mapping[str, Any] | None) -> list[Path]:
    roots = [result_path.parent.resolve()]
    if job and isinstance(job.get("output_dir"), str):
        roots.append(Path(job["output_dir"]).expanduser().resolve())
    return roots


def _mask_probe(adapter: FFmpegAdapter, path: Path, width: int, height: int) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    try:
        probe = adapter.probe(path)
        streams = probe.get("streams", [])
        stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(stream, dict):
            return False, "no_video_stream"
        if int(stream.get("width") or 0) != width or int(stream.get("height") or 0) != height:
            return False, "dimensions"
        pixel_format = str(stream.get("pix_fmt") or "")
        if pixel_format not in {"gray", "gray8"}:
            return False, f"pixel_format:{pixel_format}"
        decoded = adapter.full_decode_check(path)
        if decoded.exit_code != 0:
            return False, "decode"
    except (OSError, ValueError, RuntimeError):
        return False, "probe"
    return True, "pass"


def _review_frames(
    start_frame: int,
    end_frame: int,
    prompt_frame: int,
    frames: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Sequence[int] | Sequence[str]],
) -> list[int]:
    selected = {start_frame, max(start_frame, end_frame - 1), prompt_frame}
    selected.add(start_frame + max(0, (end_frame - start_frame - 1) // 2))
    for key in ("area_jump_frames", "centroid_jump_frames", "missing_frames"):
        selected.update(int(value) for value in diagnostics.get(key, []) if isinstance(value, int))
    visible_by_object: dict[int, list[int]] = {}
    for frame in frames:
        index = int(frame.get("frame_index", -1))
        for obj in frame.get("objects", []):
            if isinstance(obj, Mapping) and obj.get("visible"):
                visible_by_object.setdefault(int(obj.get("object_id", 0)), []).append(index)
    for indices in visible_by_object.values():
        if indices:
            selected.add(min(indices))
            selected.add(max(indices))
    return sorted(index for index in selected if start_frame <= index < end_frame)


def _track_diagnostics(
    frames: Sequence[Mapping[str, Any]],
    *,
    expected_object_count: int,
) -> dict[str, list[int] | list[str]]:
    missing_frames: list[int] = []
    identity_warnings: list[str] = []
    area_jump_frames: list[int] = []
    centroid_jump_frames: list[int] = []
    leak_warnings: list[str] = []
    ordered = sorted(frames, key=lambda item: int(item.get("frame_index", -1)))
    if ordered:
        expected = range(int(ordered[0]["frame_index"]), int(ordered[-1]["frame_index"]) + 1)
        present = {int(item["frame_index"]) for item in ordered}
        missing_frames = [index for index in expected if index not in present]
    previous_ids: set[int] | None = None
    previous_geometry: dict[int, tuple[float, float, int, int]] = {}
    for frame in ordered:
        index = int(frame.get("frame_index", -1))
        objects = [item for item in frame.get("objects", []) if isinstance(item, Mapping)]
        visible = [item for item in objects if bool(item.get("visible"))]
        current_ids = {int(item.get("object_id", 0)) for item in visible}
        if len(current_ids) > expected_object_count:
            identity_warnings.append(
                f"frame {index} has {len(current_ids)} visible objects; "
                f"expected at most {expected_object_count}"
            )
        if previous_ids is not None:
            vanished = previous_ids - current_ids
            appeared = current_ids - previous_ids
            if vanished and appeared:
                identity_warnings.append(
                    f"possible identity switch at frame {index}: "
                    f"vanished={sorted(vanished)} appeared={sorted(appeared)}"
                )
        previous_ids = current_ids
        for item in visible:
            object_id = int(item.get("object_id", 0))
            area = int(item.get("area_pixels", 0))
            centroid = item.get("centroid_xy")
            bbox = item.get("bbox_xywh")
            if area <= 0 or not isinstance(centroid, Sequence) or len(centroid) != 2:
                continue
            if isinstance(bbox, Sequence) and len(bbox) == 4:
                width = max(1, int(float(bbox[2])))
                height = max(1, int(float(bbox[3])))
            else:
                width = height = 1
            previous = previous_geometry.get(object_id)
            if previous is not None:
                previous_x, previous_y, previous_area, _ = previous
                if previous_area > 0 and max(area, previous_area) / min(area, previous_area) > 2.5:
                    area_jump_frames.append(index)
                distance = math.hypot(
                    float(centroid[0]) - previous_x, float(centroid[1]) - previous_y
                )
                if distance > max(50.0, math.sqrt(max(area, previous_area)) * 5):
                    centroid_jump_frames.append(index)
            previous_geometry[object_id] = (
                float(centroid[0]),
                float(centroid[1]),
                area,
                width * height,
            )
            if item.get("area_ratio") is not None and float(item["area_ratio"]) > 0.98:
                leak_warnings.append(f"object {object_id} nearly fills frame at frame {index}")
    return {
        "missing_frames": sorted(set(missing_frames)),
        "identity_warnings": list(dict.fromkeys(identity_warnings)),
        "area_jump_frames": sorted(set(area_jump_frames)),
        "centroid_jump_frames": sorted(set(centroid_jump_frames)),
        "leak_warnings": list(dict.fromkeys(leak_warnings)),
    }


def validate_segmentation_result(
    package_root: Path,
    result_path: Path,
    *,
    job: Mapping[str, Any] | None = None,
    adapter: FFmpegAdapter | None = None,
    verify_files: bool = True,
) -> SegmentationValidation:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentationValidationError(
            f"segmentation result is unreadable: {result_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SegmentationValidationError("segmentation result must be a JSON object")
    validate_artifact(package_root, "segmentation_result", payload)
    if payload.get("status") != "complete":
        raise SegmentationValidationError("segmentation result is not complete")
    source_range = payload.get("source_range") or (job or {}).get("source_range")
    if not isinstance(source_range, Mapping):
        frame_indices = [int(item["frame_index"]) for item in payload.get("frames", [])]
        if not frame_indices:
            raise SegmentationValidationError("segmentation result contains no frames")
        source_range = {"start_frame": min(frame_indices), "end_frame": max(frame_indices) + 1}
    start_frame = int(source_range["start_frame"])
    end_frame = int(source_range["end_frame"])
    input_video = payload.get("input_video") or (job or {}).get("input_video") or {}
    width = int(input_video.get("width") or 0) if isinstance(input_video, Mapping) else 0
    height = int(input_video.get("height") or 0) if isinstance(input_video, Mapping) else 0
    expected_object_count = int((job or {}).get("expected_object_count", 1))
    source_hash = payload.get("input_sha256")
    if isinstance(payload.get("input"), Mapping):
        source_hash = payload["input"].get("sha256", source_hash)
    if job and source_hash != job.get("input_sha256"):
        raise SegmentationValidationError("segmentation result input hash does not match the job")
    frames_value = payload.get("frames")
    if not isinstance(frames_value, list):
        raise SegmentationValidationError("segmentation result frames must be an array")
    frames = [item for item in frames_value if isinstance(item, Mapping)]
    diagnostics = _track_diagnostics(frames, expected_object_count=expected_object_count)
    validation = {
        "result_schema": "pass",
        "source_identity": "pass",
        "frame_range": "pass",
        "frame_continuity": "pass" if not diagnostics["missing_frames"] else "warning",
        "mask_files": "pass",
        "mask_dimensions": "pass",
        "mask_format": "pass",
        "geometry": "pass",
    }
    hard_failure = False
    frame_numbers = sorted(int(item.get("frame_index", -1)) for item in frames)
    expected = list(range(start_frame, end_frame))
    if frame_numbers != expected:
        validation["frame_range"] = "fail"
        hard_failure = True
    selected = adapter or FFmpegAdapter()
    output_roots = _result_output_roots(result_path.resolve(), job)
    if verify_files:
        for frame in frames:
            combined = Path(str(frame.get("combined_mask_path", "")))
            if not _path_within(combined, output_roots):
                validation["mask_files"] = "fail"
                hard_failure = True
                continue
            if width <= 0 or height <= 0:
                validation["mask_dimensions"] = "fail"
                hard_failure = True
            ok, reason = _mask_probe(selected, combined, width, height)
            if not ok:
                validation["mask_files"] = "fail"
                validation["mask_format"] = (
                    "fail" if reason.startswith("pixel_format") else validation["mask_format"]
                )
                hard_failure = True
            for item in frame.get("objects", []):
                if not isinstance(item, Mapping) or not item.get("visible"):
                    continue
                mask_path = Path(str(item.get("mask_path", "")))
                if not _path_within(mask_path, output_roots):
                    validation["mask_files"] = "fail"
                    hard_failure = True
                    continue
                ok, reason = _mask_probe(selected, mask_path, width, height)
                if not ok:
                    validation["mask_files"] = "fail"
                    validation["mask_format"] = (
                        "fail" if reason.startswith("pixel_format") else validation["mask_format"]
                    )
                    hard_failure = True
                expected_hash = item.get("mask_sha256")
                if (
                    expected_hash
                    and mask_path.is_file()
                    and sha256_file(mask_path) != expected_hash
                ):
                    validation["mask_files"] = "fail"
                    hard_failure = True
            expected_combined_hash = frame.get("combined_mask_sha256")
            if (
                expected_combined_hash
                and combined.is_file()
                and sha256_file(combined) != expected_combined_hash
            ):
                validation["mask_files"] = "fail"
                hard_failure = True
    else:
        validation["mask_files"] = "pass"
    if (
        diagnostics["identity_warnings"]
        or diagnostics["area_jump_frames"]
        or diagnostics["centroid_jump_frames"]
    ):
        validation["geometry"] = "warning"
    warnings: list[str] = []
    if diagnostics["missing_frames"]:
        warnings.append("missing_frames_require_review")
    if diagnostics["identity_warnings"]:
        warnings.append("identity_uncertain_preserve_original_fallback")
    if diagnostics["area_jump_frames"]:
        warnings.append("area_jumps_require_review")
    if diagnostics["centroid_jump_frames"]:
        warnings.append("centroid_jumps_require_review")
    if diagnostics["leak_warnings"]:
        warnings.append("possible_mask_leaks_require_review")
    status: Literal["pass", "warning", "fail"] = (
        "fail" if hard_failure else ("warning" if warnings else "pass")
    )
    prompt_value = payload.get("prompt")
    prompt_frame = (
        int(prompt_value.get("frame_index", start_frame))
        if isinstance(prompt_value, Mapping)
        else start_frame
    )
    review_frame_indices = _review_frames(
        start_frame,
        end_frame,
        prompt_frame,
        frames,
        diagnostics,
    )
    return SegmentationValidation(
        is_valid=status == "pass",
        status=status,
        validation=validation,
        diagnostics=diagnostics,
        review_frame_indices=review_frame_indices,
        warnings=warnings,
    )


def write_segmentation_contact_sheets(
    layout: ProjectLayout,
    source_path: Path,
    result_path: Path,
    validation: SegmentationValidation,
    *,
    adapter: FFmpegAdapter | None = None,
) -> list[Path]:
    """Render source and available mask review frames into project-owned files."""

    source_path = _owned_path(layout, source_path, "segmentation review source")
    result_path = _owned_path(layout, result_path, "segmentation review result")
    if not source_path.is_file():
        raise SegmentationValidationError(f"segmentation review source is missing: {source_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentationValidationError("segmentation result cannot be read for review") from exc
    if not isinstance(payload, dict):
        raise SegmentationValidationError("segmentation result must be an object for review")
    source_range = payload.get("source_range")
    if not isinstance(source_range, Mapping):
        raise SegmentationValidationError("segmentation result has no source range for review")
    start_frame = int(source_range["start_frame"])
    frame_by_index = {
        int(item["frame_index"]): item
        for item in payload.get("frames", [])
        if isinstance(item, Mapping) and "frame_index" in item
    }
    review_indices = [index for index in validation.review_frame_indices if index in frame_by_index]
    if not review_indices:
        raise SegmentationValidationError("segmentation result has no review frames")
    result_hash = sha256_file(result_path)[:16]
    review_root = layout.review / "sam3" / result_hash
    staging_root = layout.work / "sam3-review" / result_hash
    review_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    selected = adapter or FFmpegAdapter()
    outputs: list[Path] = []

    def render(
        source: Path,
        target: Path,
        indices: Sequence[int],
        *,
        input_start_number: int | None = None,
    ) -> None:
        temporary = staging_root / f".{target.name}.tmp.png"
        selected.make_contact_sheet(
            source,
            temporary,
            indices,
            scale_width=320,
            tile_columns=min(4, len(indices)),
            input_start_number=input_start_number,
        )
        if not temporary.is_file():
            raise SegmentationValidationError(f"contact sheet was not produced: {temporary}")
        os.replace(temporary, target)
        outputs.append(target)

    render(
        source_path,
        review_root / "source-contact-sheet.png",
        review_indices,
    )
    mask_indices = [
        index
        for index in review_indices
        if Path(str(frame_by_index[index].get("combined_mask_path", ""))).is_file()
    ]
    if mask_indices:
        first_mask = Path(str(frame_by_index[mask_indices[0]]["combined_mask_path"])).resolve()
        render(
            first_mask.parent / "%06d.png",
            review_root / "mask-contact-sheet.png",
            mask_indices,
            input_start_number=start_frame,
        )
    return outputs


def write_segmentation_validation(
    package_root: Path,
    layout: ProjectLayout,
    result_path: Path,
    validation: SegmentationValidation,
    *,
    source_path: Path,
    source_range: Mapping[str, int],
    contact_sheets: Sequence[Path] = (),
    revision_id: str = "rev_001",
) -> Path:
    result_path = result_path.resolve()
    source_path = source_path.resolve()
    payload = {
        "schema_name": "segmentation_validation",
        "schema_version": "1.0.0",
        "artifact_id": "art_segmentation_validation",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "source": _file_ref(source_path),
        "result": _file_ref(result_path),
        "source_range": {
            "start_frame": int(source_range["start_frame"]),
            "end_frame": int(source_range["end_frame"]),
        },
        "status": validation.status,
        "validation": validation.validation,
        "diagnostics": validation.diagnostics,
        "review_frame_indices": validation.review_frame_indices,
        "contact_sheets": [_file_ref(path) for path in contact_sheets],
        "warnings": validation.warnings,
    }
    output = layout.artifacts / f"segmentation-validation-{sha256_file(result_path)[:16]}.json"
    write_validated_artifact(package_root, "segmentation_validation", output, payload)
    alias = layout.artifacts / "segmentation-validation.json"
    write_validated_artifact(package_root, "segmentation_validation", alias, payload)
    return output
