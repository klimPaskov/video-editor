from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.adapters.process import ProcessResult
from videoedit.errors import ApprovalRequiredError, ForegroundValidationError, MaskValidationError
from videoedit.services.artifacts import (
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.foreground import AlphaStatistics, parse_alpha_statistics
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.worker_runtime import validate_worker_runtime_approval

MATANYONE_CONTRACT_VERSION = "1.1"
MATANYONE_IMPLEMENTATION_VERSION = f"{__version__}:matting-v1"
MATANYONE_REPOSITORY = "https://github.com/pq-yang/MatAnyone2"
OUTPUT_DURATION_TOLERANCE_US = 100_000
LOSSLESS_MASK_CODECS = {"ffv1", "ffvhuff", "huffyuv", "png", "rawvideo", "qtrle"}
GRAY_PIXEL_FORMAT_PREFIXES = ("gray", "monow")
MATTE_QUALITY_CATEGORIES = (
    "hair",
    "fingers",
    "clothing",
    "holes",
    "transparent_regions",
    "fast_motion",
    "motion_blur",
    "entry_exit",
    "temporal_edges",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameRange(_StrictModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> FrameRange:
        if self.end_frame <= self.start_frame:
            raise ValueError("matting source range must be half-open and non-empty")
        return self


class InputVideo(_StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    frame_rate: str = Field(pattern=r"^[0-9]+/[0-9]+$")
    duration_us: int = Field(default=0, ge=0)


class FileRef(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


class ApprovalRef(_StrictModel):
    artifact_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuntimeApprovalRef(_StrictModel):
    artifact_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class InitialMask(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_index: Literal[0] = 0
    polarity: Literal["white_foreground"] = "white_foreground"
    source: Literal["sam3", "interactive", "manual"]


class WorkerRef(_StrictModel):
    name: Literal["matanyone2"] = "matanyone2"
    contract_version: Literal["1.1"] = "1.1"
    implementation_version: str = MATANYONE_IMPLEMENTATION_VERSION


class RuntimeRef(_StrictModel):
    upstream_repository: str = MATANYONE_REPOSITORY
    upstream_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    checkpoint_id: str | None = None
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checkpoint_path: str | None = None
    python: str = "3.10"
    pytorch: str = ""
    cuda: str = ""
    device: str = "cuda:0"
    access: Literal["pending", "approved", "blocked"] = "blocked"
    runtime_approval: RuntimeApprovalRef | None = None

    @model_validator(mode="after")
    def validate_repository(self) -> RuntimeRef:
        if self.upstream_repository != MATANYONE_REPOSITORY:
            raise ValueError("MatAnyone 2 runtime must use the official upstream repository")
        if self.access == "approved" and (
            not self.upstream_commit
            or not self.checkpoint_id
            or not self.checkpoint_path
            or not self.checkpoint_sha256
            or self.runtime_approval is None
        ):
            raise ValueError(
                "approved MatAnyone 2 runtime requires pinned code, checkpoint, "
                "and runtime approval"
            )
        return self


class OutputContract(_StrictModel):
    foreground_format: Literal["video_rgb"] = "video_rgb"
    alpha_format: Literal["video_gray"] = "video_gray"
    alpha_polarity: Literal["white_foreground"] = "white_foreground"


class MattingParameters(_StrictModel):
    max_size: int | None = Field(default=1080, ge=64)
    n_warmup: int = Field(default=10, ge=0)
    r_erode: int = Field(default=10, ge=0)
    r_dilate: int = Field(default=10, ge=0)


class MattingJob(_StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    job_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=3)
    revision_id: str = Field(pattern=r"^rev_[0-9]{3,}$")
    input_path: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input: FileRef
    input_video: InputVideo
    initial_mask_path: str = Field(min_length=1)
    initial_mask_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_mask: InitialMask
    initial_mask_validation: dict[str, Any] | None = None
    mask_approval: ApprovalRef
    output_dir: str = Field(min_length=1)
    source_range: FrameRange
    approval: ApprovalRef
    worker: WorkerRef = Field(default_factory=WorkerRef)
    runtime: RuntimeRef
    output_contract: OutputContract = Field(default_factory=OutputContract)
    parameters: MattingParameters = Field(default_factory=MattingParameters)
    model_id: str = "PeiqingYang/MatAnyone2"
    device: str = "cuda:0"
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_consistency(self) -> MattingJob:
        if self.input.path != self.input_path:
            raise ValueError("input.path must match input_path")
        if self.input.sha256 != self.input_sha256:
            raise ValueError("input.sha256 must match input_sha256")
        if self.initial_mask.path != self.initial_mask_path:
            raise ValueError("initial_mask.path must match initial_mask_path")
        if self.initial_mask.sha256 != self.initial_mask_sha256:
            raise ValueError("initial_mask.sha256 must match initial_mask_sha256")
        if self.start_frame != self.source_range.start_frame:
            raise ValueError("start_frame must match source_range.start_frame")
        if self.end_frame != self.source_range.end_frame:
            raise ValueError("end_frame must match source_range.end_frame")
        if self.start_frame != 0:
            raise ValueError(
                "MatAnyone 2 first-frame masks currently require source_range.start_frame == 0"
            )
        if self.end_frame > self.input_video.frame_count:
            raise ValueError("matting source range exceeds input video frame count")
        if (
            self.initial_mask.width != self.input_video.width
            or self.initial_mask.height != self.input_video.height
        ):
            raise ValueError("initial mask dimensions must match input video dimensions")
        return self


@dataclass(frozen=True, slots=True)
class InitialMaskValidation:
    source_video: dict[str, Any]
    mask_video: dict[str, Any]
    mask_statistics: dict[str, Any]
    validation: dict[str, str]
    warnings: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return all(value == "pass" for value in self.validation.values())

    def as_job_field(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.is_valid else "fail",
            "mask_video": self.mask_video,
            "mask_statistics": self.mask_statistics,
            "validation": self.validation,
            "warnings": list(self.warnings),
        }


def _within(path: Path, root: Path) -> bool:
    resolved = path.expanduser().resolve()
    base = root.expanduser().resolve()
    return resolved == base or base in resolved.parents


def _output_stream(probe: Mapping[str, Any]) -> dict[str, Any] | None:
    return _first_stream(probe, "video")


def _output_metadata(adapter: FFmpegAdapter, path: Path) -> dict[str, Any]:
    probe = adapter.probe(path)
    stream = _output_stream(probe)
    if stream is None:
        raise MaskValidationError(f"matting output has no video stream: {path}")
    frame_count = adapter.probe_frame_count(path)
    if frame_count is None or frame_count <= 0:
        raise MaskValidationError(f"matting output frame count is unavailable: {path}")
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_count": frame_count,
        "frame_rate": _rate_string(stream),
        "duration_us": _stream_duration_us(probe, stream),
        "pixel_format": str(stream.get("pix_fmt") or "unknown"),
    }


def _output_ref(
    adapter: FFmpegAdapter,
    path: Path,
    declared: Mapping[str, Any],
) -> tuple[dict[str, Any], ProcessResult]:
    if not path.is_file():
        raise MaskValidationError(f"matting output is missing: {path}")
    expected_hash = declared.get("sha256")
    actual_hash = sha256_file(path)
    if expected_hash != actual_hash:
        raise MaskValidationError(f"matting output hash does not match the result: {path}")
    metadata = _output_metadata(adapter, path)
    decode = adapter.full_decode_check(path)
    return {**dict(declared), "path": str(path), "sha256": actual_hash, **metadata}, decode


def verify_matting_payload(
    package_root: Path,
    payload: Mapping[str, Any],
    *,
    adapter: FFmpegAdapter | None = None,
    require_contrasting_background: bool = False,
) -> dict[str, Any]:
    """Re-probe v1.1 matte outputs before any composition consumes them.

    This verifier proves structural role and alpha evidence only. Contrasting
    background review remains a separate human-reviewed gate and is never
    inferred from process success.
    """

    value = dict(payload)
    validate_artifact(package_root, "matting_result", value)
    if value.get("schema_version") != "1.1":
        raise MaskValidationError("structural matte verification requires contract 1.1")
    if value.get("status") != "complete":
        raise MaskValidationError("matting result is not complete")
    output_dir_value = value.get("output_dir")
    outputs = value.get("outputs")
    if not isinstance(output_dir_value, str) or not isinstance(outputs, Mapping):
        raise MaskValidationError("matting result is missing output directory or role references")
    output_dir = Path(output_dir_value).expanduser().resolve()
    selected = adapter or FFmpegAdapter()
    role_paths: dict[str, Path] = {}
    checked_refs: dict[str, dict[str, Any]] = {}
    decodes: dict[str, ProcessResult] = {}
    role_failures: list[str] = []
    for role, path_field in (("foreground", "foreground_path"), ("alpha", "alpha_path")):
        reference = outputs.get(role)
        declared_path = value.get(path_field)
        if not isinstance(reference, Mapping) or not isinstance(declared_path, str):
            role_failures.append(f"{role}_reference_missing")
            continue
        path = Path(declared_path).expanduser().resolve()
        if reference.get("path") != str(path):
            role_failures.append(f"{role}_path_not_bound")
            continue
        if not _within(path, output_dir):
            role_failures.append(f"{role}_path_escapes_output_dir")
            continue
        role_paths[role] = path
        try:
            checked, decode = _output_ref(selected, path, reference)
        except (OSError, ValueError, MaskValidationError) as exc:
            role_failures.append(f"{role}_media_invalid:{exc}")
            continue
        checked_refs[role] = checked
        decodes[role] = decode
    if set(role_paths) != {"foreground", "alpha"}:
        raise MaskValidationError("foreground and alpha output references are incomplete")
    if role_paths["foreground"] == role_paths["alpha"]:
        role_failures.append("foreground_and_alpha_share_path")
    if set(checked_refs) != {"foreground", "alpha"}:
        raise MaskValidationError("foreground and alpha outputs cannot be structurally verified")

    foreground = checked_refs["foreground"]
    alpha = checked_refs["alpha"]
    foreground_format = str(foreground.get("pixel_format") or "").lower()
    alpha_format = str(alpha.get("pixel_format") or "").lower()
    foreground_role = foreground_format != "" and not foreground_format.startswith(
        GRAY_PIXEL_FORMAT_PREFIXES
    )
    alpha_role = alpha_format.startswith(GRAY_PIXEL_FORMAT_PREFIXES)
    dimensions_pass = (
        foreground.get("width") == alpha.get("width")
        and foreground.get("height") == alpha.get("height")
        and int(foreground.get("width", 0)) > 0
        and int(foreground.get("height", 0)) > 0
    )
    frame_count_pass = (
        foreground.get("frame_count") == alpha.get("frame_count")
        and int(foreground.get("frame_count", 0)) > 0
    )
    frame_rate_pass = (
        parse_rate(foreground.get("frame_rate")) is not None
        and parse_rate(alpha.get("frame_rate")) is not None
        and foreground.get("frame_rate") == alpha.get("frame_rate")
    )
    duration_pass = (
        abs(int(foreground.get("duration_us", 0)) - int(alpha.get("duration_us", 0)))
        <= OUTPUT_DURATION_TOLERANCE_US
        and int(foreground.get("duration_us", 0)) > 0
    )
    decode_pass = all(result.exit_code == 0 for result in decodes.values())
    if not foreground_role:
        role_failures.append("foreground_output_is_grayscale")
    if not alpha_role:
        role_failures.append("alpha_output_is_not_grayscale")
    if not dimensions_pass:
        role_failures.append("foreground_alpha_dimensions_mismatch")
    if not frame_count_pass:
        role_failures.append("foreground_alpha_frame_count_mismatch")
    if not frame_rate_pass:
        role_failures.append("foreground_alpha_frame_rate_mismatch")
    if not duration_pass:
        role_failures.append("foreground_alpha_duration_mismatch")
    if not decode_pass:
        role_failures.append("foreground_or_alpha_decode_failed")

    alpha_samples: list[AlphaStatistics] = []
    if alpha_role and frame_count_pass:
        count = int(alpha["frame_count"])
        sample_indices = sorted({0, count // 2, count - 1})
        for frame_index in sample_indices:
            try:
                alpha_samples.append(
                    parse_alpha_statistics(
                        selected.measure_mask(role_paths["alpha"], frame_index=frame_index)
                    )
                )
            except (ValueError, MaskValidationError) as exc:
                role_failures.append(f"alpha_measurement_failed:{exc}")
                break
    polarity = _mask_polarity(alpha_samples) if alpha_samples else "unknown"
    alpha_polarity_pass = polarity == "white_foreground"
    if not alpha_polarity_pass:
        role_failures.append("alpha_polarity_not_white_foreground")

    existing_verification = value.get("verification")
    existing_contrast = (
        existing_verification.get("contrasting_background")
        if isinstance(existing_verification, Mapping)
        else None
    )
    contrast = "pass" if existing_contrast == "pass" else "pending"
    if require_contrasting_background and contrast != "pass":
        role_failures.append("contrasting_background_review_pending")
    status = "fail" if role_failures else ("pass" if contrast == "pass" else "pending")
    warnings: list[str] = []
    diagnostics = value.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        warnings.extend(str(item) for item in diagnostics.get("warnings", []) if item)
        diagnostics_value = dict(diagnostics)
    else:
        diagnostics_value = {
            "review_frame_indices": [],
            "warnings": [],
            "stability": {
                key: "pending"
                for key in (
                    "status",
                    "hair",
                    "fingers",
                    "clothing",
                    "holes",
                    "transparent_regions",
                    "fast_motion",
                    "motion_blur",
                    "entry_exit",
                    "temporal_edges",
                )
            },
        }
    warnings.extend(role_failures)
    diagnostics_value["warnings"] = list(dict.fromkeys(warnings))
    verification = {
        "status": status,
        "foreground_role": "pass" if foreground_role else "fail",
        "alpha_role": "pass" if alpha_role else "fail",
        "alpha_polarity": "white_foreground" if alpha_polarity_pass else "unknown",
        "dimensions": "pass" if dimensions_pass else "fail",
        "frame_count": "pass" if frame_count_pass else "fail",
        "decode": "pass" if decode_pass else "fail",
        "contrasting_background": contrast,
    }
    value["outputs"] = checked_refs
    value["verification"] = verification
    value["diagnostics"] = diagnostics_value
    return value


def verify_matting_result(
    package_root: Path,
    result_path: Path,
    *,
    output_path: Path | None = None,
    adapter: FFmpegAdapter | None = None,
    require_contrasting_background: bool = False,
) -> Path:
    """Write a new verified-result revision without overwriting raw worker output."""

    result_path = result_path.expanduser().resolve()
    if not result_path.is_file():
        raise MaskValidationError(f"matting result is missing: {result_path}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MaskValidationError("matting result must be a JSON object")
    verified = verify_matting_payload(
        package_root,
        payload,
        adapter=adapter,
        require_contrasting_background=require_contrasting_background,
    )
    destination = (
        (output_path or result_path.with_name(f"{result_path.stem}-verified.json"))
        .expanduser()
        .resolve()
    )
    if destination == result_path:
        raise MaskValidationError("verified matting result must be a new artifact")
    return write_validated_artifact(package_root, "matting_result", destination, verified)


def render_matting_contrast_previews(
    package_root: Path,
    result_path: Path,
    *,
    output_dir: Path | None = None,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Create black/white matte previews and a pending review manifest.

    The result and all media are hash-bound. Preview files are rendered in a
    private staging directory and promoted as one directory only after the
    output probes, full decodes, contact sheets, and manifest schema pass.
    The manifest intentionally remains ``pending`` until an operator compares
    the subject edges on both backgrounds.
    """

    result_path = result_path.expanduser().resolve()
    if not result_path.is_file():
        raise MaskValidationError(f"matting result is missing: {result_path}")
    raw_payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise MaskValidationError("matting result must be a JSON object")
    validate_artifact(package_root, "matting_result", raw_payload)
    if raw_payload.get("schema_version") != "1.1":
        raise MaskValidationError("contrasting-background review requires contract 1.1")
    selected = adapter or FFmpegAdapter()
    verified = verify_matting_payload(package_root, raw_payload, adapter=selected)
    structural = verified.get("verification")
    if not isinstance(structural, Mapping) or structural.get("status") == "fail":
        raise MaskValidationError("matting output structural verification failed")

    source_value = verified.get("input_path")
    source_hash = verified.get("input_sha256")
    if not isinstance(source_value, str) or not isinstance(source_hash, str):
        raise MaskValidationError("matting result has no hash-bound source video")
    source_path = Path(source_value).expanduser().resolve()
    if not source_path.is_file() or sha256_file(source_path) != source_hash:
        raise MaskValidationError("matting source is missing or its hash is stale")

    source_range_value = verified.get("source_range")
    if not isinstance(source_range_value, Mapping):
        raise MaskValidationError("matting result has no source range")
    start_frame = int(source_range_value.get("start_frame", -1))
    end_frame = int(source_range_value.get("end_frame", -1))
    if start_frame < 0 or end_frame <= start_frame:
        raise MaskValidationError("matting source range is invalid")

    outputs = verified.get("outputs")
    foreground_ref = outputs.get("foreground") if isinstance(outputs, Mapping) else None
    alpha_ref = outputs.get("alpha") if isinstance(outputs, Mapping) else None
    if not isinstance(foreground_ref, Mapping) or not isinstance(alpha_ref, Mapping):
        raise MaskValidationError("matting result has no verified foreground and alpha outputs")
    foreground_path = Path(str(foreground_ref["path"])).expanduser().resolve()
    alpha_path = Path(str(alpha_ref["path"])).expanduser().resolve()
    output_count = int(foreground_ref.get("frame_count", 0))
    if output_count <= 0 or end_frame - start_frame != output_count:
        raise MaskValidationError("matting output frame count does not match its source range")

    source_probe = selected.probe(source_path)
    source_video = _first_stream(source_probe, "video")
    source_frame_count = selected.probe_frame_count(source_path)
    if source_video is None or source_frame_count is None or source_frame_count <= 0:
        raise MaskValidationError("source video metadata is unavailable for matte review")
    if end_frame > source_frame_count:
        raise MaskValidationError("matting source range exceeds source video")
    if int(source_video.get("width") or 0) != int(foreground_ref.get("width") or 0) or int(
        source_video.get("height") or 0
    ) != int(foreground_ref.get("height") or 0):
        raise MaskValidationError("source and matte output dimensions do not match")
    if _rate_string(source_video) != foreground_ref.get("frame_rate"):
        raise MaskValidationError("source and matte output frame rates do not match")

    result_hash = sha256_file(result_path)
    encoder_identity = adapter_encoder_identity(selected)
    encoder_label = str(encoder_identity["video_codec"])
    encoder_suffix = "" if encoder_label == "libx264" else f"-{encoder_label}"
    default_output = result_path.with_name(
        f"{result_path.stem}-contrast-{result_hash[:16]}{encoder_suffix}"
    )
    final_root = (output_dir or default_output).expanduser().resolve()
    matte_output_dir = Path(str(verified.get("output_dir", ""))).expanduser().resolve()
    if final_root == matte_output_dir:
        raise MaskValidationError("contrast review output must be separate from matte outputs")
    if final_root.is_file():
        raise MaskValidationError(f"contrast review output is a file: {final_root}")
    manifest_path = final_root / "matting-contrast-review.json"
    if final_root.exists():
        if output_dir is None and manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise MaskValidationError("existing contrast review manifest is not an object")
            validate_artifact(package_root, "matting_contrast_review", existing)
            existing_refs: list[Mapping[str, Any]] = []
            for value in (
                existing.get("source"),
                existing.get("result"),
                existing.get("source_contact_sheet"),
            ):
                if isinstance(value, Mapping):
                    existing_refs.append(value)
            existing_previews = existing.get("previews")
            if isinstance(existing_previews, Mapping):
                for color in ("black", "white"):
                    preview = existing_previews.get(color)
                    if isinstance(preview, Mapping):
                        for field in ("preview", "contact_sheet"):
                            reference = preview.get(field)
                            if isinstance(reference, Mapping):
                                existing_refs.append(reference)
            for reference in existing_refs:
                path = Path(str(reference.get("path"))).expanduser().resolve()
                if not path.is_file() or sha256_file(path) != reference.get("sha256"):
                    raise MaskValidationError(f"existing contrast review manifest is stale: {path}")
            return manifest_path
        raise MaskValidationError(f"contrast review output already exists: {final_root}")

    review_indices = sorted({0, output_count // 2, output_count - 1})
    final_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{final_root.name}.", dir=str(final_root.parent))
    ).resolve()
    version = str(selected.version()) or "unknown"
    commands: list[dict[str, Any]] = []

    def _run(command_result: ProcessResult, label: str) -> None:
        commands.append(_command_record(command_result, staging_root, version))
        if command_result.exit_code != 0:
            detail = command_result.stderr.strip()[-2000:]
            raise MaskValidationError(f"{label} failed: {detail}")

    def _staged_file_ref(final_path: Path, staged_path: Path) -> dict[str, Any]:
        if not staged_path.is_file() or staged_path.stat().st_size <= 0:
            raise MaskValidationError(f"matting review file was not produced: {staged_path}")
        return {
            "path": str(final_path),
            "sha256": sha256_file(staged_path),
            "size_bytes": staged_path.stat().st_size,
        }

    try:
        black_stage = staging_root / "black-preview.mp4"
        white_stage = staging_root / "white-preview.mp4"
        source_decode = selected.full_decode_check(source_path)
        _run(source_decode, "matte review source decode")
        _run(
            selected.render_contrasting_background(
                foreground_path,
                alpha_path,
                black_stage,
                color="black",
            ),
            "black contrasting-background preview",
        )
        _run(
            selected.render_contrasting_background(
                foreground_path,
                alpha_path,
                white_stage,
                color="white",
            ),
            "white contrasting-background preview",
        )

        preview_metadata: dict[str, dict[str, Any]] = {}
        for color, path in (("black", black_stage), ("white", white_stage)):
            preview_metadata[color] = _output_metadata(selected, path)
            decode = selected.full_decode_check(path)
            _run(decode, f"{color} contrasting-background decode")
            if (
                preview_metadata[color]["width"] != foreground_ref["width"]
                or preview_metadata[color]["height"] != foreground_ref["height"]
            ):
                raise MaskValidationError(f"{color} preview dimensions do not match foreground")
            if preview_metadata[color]["frame_count"] != output_count:
                raise MaskValidationError(f"{color} preview frame count does not match matte")
            if preview_metadata[color]["frame_rate"] != foreground_ref["frame_rate"]:
                raise MaskValidationError(f"{color} preview frame rate does not match foreground")
            if (
                abs(
                    int(preview_metadata[color]["duration_us"]) - int(foreground_ref["duration_us"])
                )
                > OUTPUT_DURATION_TOLERANCE_US
            ):
                raise MaskValidationError(f"{color} preview duration does not match foreground")

        black_hash = sha256_file(black_stage)
        white_hash = sha256_file(white_stage)
        if black_hash == white_hash:
            raise MaskValidationError("black and white contrast previews are identical")

        source_stage = staging_root / "source-contact-sheet.png"
        black_sheet_stage = staging_root / "black-contact-sheet.png"
        white_sheet_stage = staging_root / "white-contact-sheet.png"
        absolute_indices = [start_frame + index for index in review_indices]
        _run(
            selected.make_contact_sheet(
                source_path,
                source_stage,
                absolute_indices,
                scale_width=320,
                tile_columns=min(3, len(absolute_indices)),
            ),
            "source matte review contact sheet",
        )
        _run(
            selected.make_contact_sheet(
                black_stage,
                black_sheet_stage,
                review_indices,
                scale_width=320,
                tile_columns=min(3, len(review_indices)),
            ),
            "black matte review contact sheet",
        )
        _run(
            selected.make_contact_sheet(
                white_stage,
                white_sheet_stage,
                review_indices,
                scale_width=320,
                tile_columns=min(3, len(review_indices)),
            ),
            "white matte review contact sheet",
        )

        final_black = final_root / black_stage.name
        final_white = final_root / white_stage.name
        final_source_sheet = final_root / source_stage.name
        final_black_sheet = final_root / black_sheet_stage.name
        final_white_sheet = final_root / white_sheet_stage.name
        payload = {
            "schema_name": "matting_contrast_review",
            "schema_version": "1.0.0",
            "artifact_id": "art_matting_contrast_review",
            "project_id": str(verified["project_id"]),
            "revision_id": str(verified["revision_id"]),
            "created_at": now_iso(),
            "producer": producer("matting_contrast_review", "ffmpeg", version),
            "source": {
                "path": str(source_path),
                "sha256": source_hash,
                "size_bytes": source_path.stat().st_size,
            },
            "result": {
                "path": str(result_path),
                "sha256": result_hash,
                "size_bytes": result_path.stat().st_size,
            },
            "source_range": {
                "start_frame": start_frame,
                "end_frame": end_frame,
            },
            "review_frame_indices": review_indices,
            "source_contact_sheet": _staged_file_ref(final_source_sheet, source_stage),
            "previews": {
                "black": {
                    "background_color": "black",
                    "preview": _staged_file_ref(final_black, black_stage),
                    "contact_sheet": _staged_file_ref(final_black_sheet, black_sheet_stage),
                },
                "white": {
                    "background_color": "white",
                    "preview": _staged_file_ref(final_white, white_stage),
                    "contact_sheet": _staged_file_ref(final_white_sheet, white_sheet_stage),
                },
            },
            "validation": {
                "result_schema": "pass",
                "source_identity": "pass",
                "source_decode": "pass",
                "foreground_alpha_structure": "pass",
                "black_decode": "pass",
                "white_decode": "pass",
                "dimensions": "pass",
                "frame_count": "pass",
                "frame_rate": "pass",
                "duration": "pass",
                "preview_distinct": "pass",
                "source_contact_sheet": "pass",
                "background_comparison": "pending",
                "operator_review": "pending",
            },
            "status": "pending",
            "warnings": [
                (
                    "Black and white previews are evidence only; an operator must compare hair, "
                    "fingers, clothing, holes, transparent regions, motion blur, entry/exit "
                    "edges, and temporal edges."
                ),
                "Background color correctness is not inferred from distinct file hashes.",
            ],
            "commands": commands,
        }
        staged_manifest = staging_root / manifest_path.name
        write_validated_artifact(
            package_root,
            "matting_contrast_review",
            staged_manifest,
            payload,
        )
        os.replace(staging_root, final_root)
        return manifest_path
    except BaseException:
        if staging_root.is_dir():
            failed_root = final_root.parent / f".{final_root.name}.failed-{result_hash[:12]}"
            try:
                os.replace(staging_root, failed_root)
            except OSError:
                pass
        raise


def build_matting_quality_review(
    package_root: Path,
    result_path: Path,
    contrast_review_path: Path,
    *,
    output_path: Path | None = None,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Persist decoded alpha evidence and explicit pending stability checks.

    This report is deliberately not a model-quality classifier. It proves that
    the current result and contrast package are still hash-bound, samples the
    alpha at the declared review frames, and enumerates the human checks that
    must be completed before a matte can be accepted.
    """

    result_path = result_path.expanduser().resolve()
    contrast_review_path = contrast_review_path.expanduser().resolve()
    if not result_path.is_file():
        raise MaskValidationError(f"matting result is missing: {result_path}")
    if not contrast_review_path.is_file():
        raise MaskValidationError(f"contrast review manifest is missing: {contrast_review_path}")
    raw_payload = json.loads(result_path.read_text(encoding="utf-8"))
    contrast_payload = json.loads(contrast_review_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict) or not isinstance(contrast_payload, dict):
        raise MaskValidationError("matting quality inputs must be JSON objects")
    validate_artifact(package_root, "matting_result", raw_payload)
    validate_artifact(package_root, "matting_contrast_review", contrast_payload)
    if raw_payload.get("schema_version") != "1.1":
        raise MaskValidationError("matting quality review requires result contract 1.1")
    selected = adapter or FFmpegAdapter()
    verified = verify_matting_payload(package_root, raw_payload, adapter=selected)
    structural = verified.get("verification")
    if not isinstance(structural, Mapping) or structural.get("status") == "fail":
        raise MaskValidationError("matting output structural verification failed")
    if contrast_payload.get("status") == "fail":
        raise MaskValidationError("contrast review manifest failed validation")

    result_hash = sha256_file(result_path)

    def _current_ref(value: object, label: str) -> Path:
        if not isinstance(value, Mapping):
            raise MaskValidationError(f"{label} is missing a file reference")
        path_value = value.get("path")
        hash_value = value.get("sha256")
        if not isinstance(path_value, str) or not isinstance(hash_value, str):
            raise MaskValidationError(f"{label} has an invalid file reference")
        path = Path(path_value).expanduser().resolve()
        if not path.is_file() or sha256_file(path) != hash_value:
            raise MaskValidationError(f"{label} is missing or hash-stale: {path}")
        return path

    contrast_result = _current_ref(contrast_payload.get("result"), "contrast result")
    if contrast_result != result_path or contrast_payload["result"].get("sha256") != result_hash:
        raise MaskValidationError("contrast review is not bound to the current matting result")
    source_path = _current_ref(contrast_payload.get("source"), "contrast source")
    result_source = Path(str(verified["input_path"])).expanduser().resolve()
    if source_path != result_source or sha256_file(source_path) != verified.get("input_sha256"):
        raise MaskValidationError("contrast review is not bound to the current source")
    _current_ref(contrast_payload.get("source_contact_sheet"), "source contact sheet")
    previews = contrast_payload.get("previews")
    if not isinstance(previews, Mapping):
        raise MaskValidationError("contrast review has no preview references")
    for color in ("black", "white"):
        preview = previews.get(color)
        if not isinstance(preview, Mapping):
            raise MaskValidationError(f"contrast review has no {color} preview")
        _current_ref(preview.get("preview"), f"{color} preview")
        _current_ref(preview.get("contact_sheet"), f"{color} contact sheet")
    contrast_validation = contrast_payload.get("validation")
    if not isinstance(contrast_validation, Mapping):
        raise MaskValidationError("contrast review has no validation checks")
    required_contrast_checks = (
        "result_schema",
        "source_identity",
        "source_decode",
        "foreground_alpha_structure",
        "black_decode",
        "white_decode",
        "dimensions",
        "frame_count",
        "frame_rate",
        "duration",
        "preview_distinct",
        "source_contact_sheet",
    )
    if any(contrast_validation.get(key) != "pass" for key in required_contrast_checks):
        raise MaskValidationError("contrast review structural checks are incomplete")

    source_range = verified.get("source_range")
    outputs = verified.get("outputs")
    if not isinstance(source_range, Mapping) or not isinstance(outputs, Mapping):
        raise MaskValidationError("matting result is missing range or output references")
    start_frame = int(source_range.get("start_frame", -1))
    end_frame = int(source_range.get("end_frame", -1))
    foreground_ref = outputs.get("foreground")
    alpha_ref = outputs.get("alpha")
    if not isinstance(foreground_ref, Mapping) or not isinstance(alpha_ref, Mapping):
        raise MaskValidationError("matting result is missing foreground or alpha output")
    alpha_path = _current_ref(alpha_ref, "alpha output")
    output_count = int(foreground_ref.get("frame_count", 0))
    if start_frame < 0 or end_frame <= start_frame or output_count <= 0:
        raise MaskValidationError("matting quality source range is invalid")
    if end_frame - start_frame != output_count:
        raise MaskValidationError("matting quality range does not match output frame count")

    review_indices = {0, output_count // 2, output_count - 1}
    declared_review = contrast_payload.get("review_frame_indices")
    if isinstance(declared_review, list):
        review_indices.update(
            int(value)
            for value in declared_review
            if isinstance(value, int) and 0 <= value < output_count
        )
    diagnostics = verified.get("diagnostics")
    if isinstance(diagnostics, Mapping) and isinstance(
        diagnostics.get("review_frame_indices"), list
    ):
        for value in diagnostics["review_frame_indices"]:
            if isinstance(value, int):
                relative = value - start_frame
                if 0 <= relative < output_count:
                    review_indices.add(relative)
    ordered_indices = sorted(review_indices)
    version = str(selected.version()) or "unknown"
    commands: list[dict[str, Any]] = []
    decode = selected.full_decode_check(alpha_path)
    commands.append(_command_record(decode, alpha_path.parent, version))
    if decode.exit_code != 0:
        raise MaskValidationError("alpha output failed the quality-review decode")

    alpha_samples: list[dict[str, Any]] = []
    for frame_index in ordered_indices:
        measurement = selected.measure_mask(alpha_path, frame_index=frame_index)
        commands.append(_command_record(measurement, alpha_path.parent, version))
        if measurement.exit_code != 0:
            raise MaskValidationError(f"alpha measurement failed at frame {frame_index}")
        try:
            statistics = parse_alpha_statistics(measurement)
        except (ValueError, ForegroundValidationError, MaskValidationError) as exc:
            raise MaskValidationError(
                f"alpha measurement could not be parsed at frame {frame_index}: {exc}"
            ) from exc
        alpha_samples.append(
            {
                "frame_index": frame_index,
                "minimum": statistics.minimum,
                "maximum": statistics.maximum,
                "mean": statistics.mean,
            }
        )
    alpha_range_pass = all(
        0 <= float(sample["minimum"]) <= 255
        and 0 <= float(sample["maximum"]) <= 255
        and 0 <= float(sample["mean"]) <= 255
        and float(sample["minimum"]) <= float(sample["maximum"])
        for sample in alpha_samples
    )
    coverage_pass = {0, output_count // 2, output_count - 1}.issubset(review_indices)
    category_reviews = {
        category: {
            "status": "pending",
            "evidence_frame_indices": ordered_indices,
            "notes": [
                (
                    "Operator must compare this category on the source and both contrast "
                    "previews; no semantic quality detector was used."
                )
            ],
            "fallback": (
                "Retain the original shot or use the approved controlled green-screen/"
                "chroma-key path until the matte is accepted."
            ),
        }
        for category in MATTE_QUALITY_CATEGORIES
    }
    destination = (
        (
            output_path
            or result_path.with_name(f"{result_path.stem}-quality-{result_hash[:16]}.json")
        )
        .expanduser()
        .resolve()
    )
    if destination == result_path or destination == contrast_review_path:
        raise MaskValidationError("matting quality review must be a new artifact")
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise MaskValidationError("existing matting quality review is not an object")
        validate_artifact(package_root, "matting_quality_review", existing)
        if existing.get("result", {}).get("sha256") != result_hash or existing.get(
            "contrast_review", {}
        ).get("sha256") != sha256_file(contrast_review_path):
            raise MaskValidationError("existing matting quality review is stale")
        return destination
    payload = {
        "schema_name": "matting_quality_review",
        "schema_version": "1.0.0",
        "artifact_id": "art_matting_quality_review",
        "project_id": str(verified["project_id"]),
        "revision_id": str(verified["revision_id"]),
        "created_at": now_iso(),
        "producer": producer("matting_quality_review", "ffmpeg", version),
        "result": {
            "path": str(result_path),
            "sha256": result_hash,
            "size_bytes": result_path.stat().st_size,
        },
        "contrast_review": {
            "path": str(contrast_review_path),
            "sha256": sha256_file(contrast_review_path),
            "size_bytes": contrast_review_path.stat().st_size,
        },
        "source_range": {
            "start_frame": start_frame,
            "end_frame": end_frame,
        },
        "review_frame_indices": ordered_indices,
        "alpha_samples": alpha_samples,
        "validation": {
            "result_schema": "pass",
            "contrast_review": "pass",
            "alpha_decode": "pass",
            "alpha_range": "pass" if alpha_range_pass else "fail",
            "frame_coverage": "pass" if coverage_pass else "fail",
            "semantic_categories": "pending",
            "operator_review": "pending",
        },
        "category_reviews": category_reviews,
        "status": "pending" if alpha_range_pass and coverage_pass else "fail",
        "warnings": [
            (
                "Category statuses remain pending until an operator compares hair, fingers, "
                "clothing, holes, transparent regions, fast motion, motion blur, entry/exit, "
                "and temporal edges."
            ),
            (
                "Alpha samples prove numeric range and frame coverage only; they do not prove "
                "identity, edge quality, or temporal stability."
            ),
        ],
        "commands": commands,
    }
    return write_validated_artifact(package_root, "matting_quality_review", destination, payload)


def _first_stream(probe: Mapping[str, Any], stream_type: str) -> dict[str, Any] | None:
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        return None
    for value in streams:
        if isinstance(value, dict) and value.get("codec_type") == stream_type:
            return dict(value)
    return None


def _stream_duration_us(probe: Mapping[str, Any], stream: Mapping[str, Any]) -> int:
    duration = seconds_to_us(stream.get("duration"))
    if duration is not None:
        return duration
    format_value = probe.get("format")
    if isinstance(format_value, Mapping):
        return seconds_to_us(format_value.get("duration")) or 0
    return 0


def _rate_string(stream: Mapping[str, Any]) -> str:
    rate = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate"))
    if rate is None:
        return "0/1"
    return f"{rate['numerator']}/{rate['denominator']}"


def _video_metadata(
    probe: Mapping[str, Any], stream: Mapping[str, Any], frame_count: int
) -> dict[str, Any]:
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_count": frame_count,
        "frame_rate": _rate_string(stream),
        "duration_us": _stream_duration_us(probe, stream),
        "codec": str(stream.get("codec_name") or "unknown"),
        "pixel_format": str(stream.get("pix_fmt") or "unknown"),
    }


def _mask_polarity(samples: list[AlphaStatistics]) -> str:
    minimum = min((sample.minimum for sample in samples), default=0.0)
    maximum = max((sample.maximum for sample in samples), default=0.0)
    if minimum >= 255 and maximum >= 255:
        return "opaque"
    if minimum <= 0 and maximum <= 0:
        return "transparent"
    if minimum <= 5 and maximum >= 250:
        return "white_foreground"
    return "unknown"


def _is_lossless_gray(stream: Mapping[str, Any]) -> bool:
    codec = str(stream.get("codec_name") or "").lower()
    pixel_format = str(stream.get("pix_fmt") or "").lower()
    return codec in LOSSLESS_MASK_CODECS and pixel_format.startswith(GRAY_PIXEL_FORMAT_PREFIXES)


def validate_initial_mask_alignment(
    source_probe: Mapping[str, Any],
    mask_probe: Mapping[str, Any],
    *,
    source_frame_count: int | None,
    mask_frame_count: int | None,
    mask_samples: list[AlphaStatistics],
    full_decode_ok: bool = True,
) -> InitialMaskValidation:
    """Validate a person mask that is explicitly intended for source frame zero."""

    source_video = _first_stream(source_probe, "video")
    mask_video = _first_stream(mask_probe, "video")
    if source_video is None or mask_video is None:
        raise MaskValidationError("initial person mask validation requires two video streams")
    mask_has_audio = _first_stream(mask_probe, "audio") is not None
    source_width = int(source_video.get("width") or 0)
    source_height = int(source_video.get("height") or 0)
    mask_width = int(mask_video.get("width") or 0)
    mask_height = int(mask_video.get("height") or 0)
    dimensions_pass = (
        source_width > 0
        and source_height > 0
        and source_width == mask_width
        and source_height == mask_height
    )
    frame_count_pass = mask_frame_count == 1
    frame_rate_pass = (
        parse_rate(mask_video.get("avg_frame_rate")) is not None
        or parse_rate(mask_video.get("r_frame_rate")) is not None
    )
    values = [
        value for sample in mask_samples for value in (sample.minimum, sample.maximum, sample.mean)
    ]
    range_pass = bool(values) and all(
        math.isfinite(value) and 0 <= value <= 255 for value in values
    )
    polarity = _mask_polarity(mask_samples) if range_pass else "unknown"
    polarity_pass = polarity == "white_foreground"
    lossless_pass = _is_lossless_gray(mask_video)
    audio_pass = not mask_has_audio
    warnings: list[str] = []
    if mask_frame_count != 1:
        warnings.append("initial_mask_must_contain_exactly_one_frame")
    if polarity in {"opaque", "transparent"}:
        warnings.append(f"initial_mask_polarity_is_{polarity}")
    if not lossless_pass:
        warnings.append("initial_mask_must_be_lossless_grayscale")
    if mask_has_audio:
        warnings.append("initial_mask_must_not_contain_audio")
    return InitialMaskValidation(
        source_video=_video_metadata(
            source_probe,
            source_video,
            source_frame_count if source_frame_count is not None else 0,
        ),
        mask_video=_video_metadata(
            mask_probe,
            mask_video,
            mask_frame_count if mask_frame_count is not None else 0,
        ),
        mask_statistics={
            "min": min((sample.minimum for sample in mask_samples), default=0.0),
            "max": max((sample.maximum for sample in mask_samples), default=0.0),
            "mean": (
                sum(sample.mean for sample in mask_samples) / len(mask_samples)
                if mask_samples
                else 0.0
            ),
            "polarity": polarity,
            "sampled_frames": len(mask_samples),
        },
        validation={
            "full_decode": "pass" if full_decode_ok else "fail",
            "lossless": "pass" if lossless_pass else "fail",
            "pixel_format": "pass"
            if str(mask_video.get("pix_fmt") or "").lower().startswith(GRAY_PIXEL_FORMAT_PREFIXES)
            else "fail",
            "dimensions": "pass" if dimensions_pass else "fail",
            "frame_count": "pass" if frame_count_pass else "fail",
            "frame_rate": "pass" if frame_rate_pass else "fail",
            "range": "pass" if range_pass else "fail",
            "polarity": "pass" if polarity_pass else "fail",
            "frame_index": "pass" if frame_count_pass else "fail",
            "audio": "pass" if audio_pass else "fail",
        },
        warnings=tuple(warnings),
    )


def _command_record(result: ProcessResult, working_directory: Path, version: str) -> dict[str, Any]:
    arguments = result.arguments or ("ffmpeg",)
    return {
        "executable": arguments[0],
        "arguments": list(arguments[1:]),
        "working_directory": str(working_directory.resolve()),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "version": version or "unknown",
    }


def _owned_path(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise MaskValidationError(f"{label} must stay inside the project: {resolved}") from exc
    return resolved


def _mask_validation(
    source: Path,
    initial_mask: Path,
    *,
    adapter: FFmpegAdapter,
) -> tuple[InitialMaskValidation, list[dict[str, Any]]]:
    source_probe = adapter.probe(source)
    mask_probe = adapter.probe(initial_mask)
    source_video = _first_stream(source_probe, "video")
    mask_video = _first_stream(mask_probe, "video")
    if source_video is None or mask_video is None:
        raise MaskValidationError("source and initial mask must contain video streams")
    source_frame_count = adapter.probe_frame_count(source)
    mask_frame_count = adapter.probe_frame_count(initial_mask)
    if source_frame_count is None or source_frame_count <= 0:
        raise MaskValidationError("source frame count could not be determined")
    if mask_frame_count is None:
        raise MaskValidationError("initial mask frame count could not be determined")
    measurement = adapter.measure_mask(initial_mask, frame_index=0)
    decode = adapter.full_decode_check(initial_mask)
    version = str(adapter.version()) or "unknown"
    working_directory = initial_mask.parent
    commands = [
        _command_record(measurement, working_directory, version),
        _command_record(decode, working_directory, version),
    ]
    validation = validate_initial_mask_alignment(
        source_probe,
        mask_probe,
        source_frame_count=source_frame_count,
        mask_frame_count=mask_frame_count,
        mask_samples=[parse_alpha_statistics(measurement)],
        full_decode_ok=decode.exit_code == 0,
    )
    if not validation.is_valid:
        failed = [name for name, status in validation.validation.items() if status != "pass"]
        raise MaskValidationError("initial person mask validation failed: " + ", ".join(failed))
    return validation, commands


def build_matting_job(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    initial_mask: Path,
    *,
    job_id: str,
    mask_source: Literal["sam3", "interactive", "manual"],
    mask_approval: Mapping[str, Any],
    approval: Mapping[str, Any],
    end_frame: int | None = None,
    upstream_commit: str | None = None,
    checkpoint_id: str | None = None,
    checkpoint_sha256: str | None = None,
    checkpoint_path: Path | None = None,
    runtime_approval_path: Path | None = None,
    runtime_access: Literal["pending", "approved", "blocked"] = "blocked",
    pytorch: str = "",
    cuda: str = "",
    device: str = "cuda:0",
    output_dir: Path | None = None,
    revision_id: str = "rev_001",
    parameters: Mapping[str, Any] | None = None,
    adapter: FFmpegAdapter | None = None,
) -> dict[str, Any]:
    """Build a hash-bound v1.1 job from an operator-approved first-frame mask.

    The function validates the mask but never creates the approval reference. The
    caller must provide a current, human-created ``mask_approval`` reference.
    """

    source = _owned_path(layout, source, "matting source")
    initial_mask = _owned_path(layout, initial_mask, "initial person mask")
    if not source.is_file():
        raise MaskValidationError(f"matting source is missing: {source}")
    if not initial_mask.is_file():
        raise MaskValidationError(f"initial person mask is missing: {initial_mask}")
    try:
        mask_ref = ApprovalRef.model_validate(dict(mask_approval))
    except ValueError as exc:
        raise ApprovalRequiredError(
            "MatAnyone 2 requires a hash-bound first-frame mask approval reference"
        ) from exc
    try:
        effect_ref = ApprovalRef.model_validate(dict(approval))
    except ValueError as exc:
        raise ApprovalRequiredError(
            "MatAnyone 2 requires a hash-bound Gate 1 effect approval reference"
        ) from exc
    if mask_source not in {"sam3", "interactive", "manual"}:
        raise MaskValidationError(f"unsupported initial mask source: {mask_source}")
    selected = adapter or FFmpegAdapter()
    validation, commands = _mask_validation(source, initial_mask, adapter=selected)
    source_probe = selected.probe(source)
    source_video = _first_stream(source_probe, "video")
    source_frame_count = selected.probe_frame_count(source)
    if source_video is None or source_frame_count is None:
        raise MaskValidationError("source video metadata could not be determined")
    if end_frame is None:
        end_frame = source_frame_count
    if end_frame <= 0 or end_frame > source_frame_count:
        raise MaskValidationError("matting end_frame must be within the source frame count")
    if checkpoint_path is not None:
        checkpoint_path = checkpoint_path.expanduser().resolve()
        if not checkpoint_path.is_file():
            raise MaskValidationError(
                f"declared MatAnyone 2 checkpoint is missing: {checkpoint_path}"
            )
        observed_checkpoint_hash = sha256_file(checkpoint_path)
        if checkpoint_sha256 != observed_checkpoint_hash:
            raise MaskValidationError("declared MatAnyone 2 checkpoint SHA-256 does not match")
    if runtime_access == "approved" and checkpoint_path is None:
        raise ApprovalRequiredError(
            "live MatAnyone 2 access requires an operator-provided local checkpoint"
        )
    runtime_approval_ref: dict[str, str] | None = None
    if runtime_approval_path is not None:
        runtime_approval_ref = validate_worker_runtime_approval(
            package_root,
            layout,
            runtime_approval_path,
            worker="matanyone2",
            upstream_commit=upstream_commit or "",
            checkpoint_id=checkpoint_id or "",
            checkpoint_sha256=checkpoint_sha256 or "",
            pytorch=pytorch,
            cuda=cuda,
            device=device,
            revision_id=revision_id,
        )
    if runtime_access == "approved" and runtime_approval_ref is None:
        raise ApprovalRequiredError(
            "live MatAnyone 2 access requires a separate human runtime approval"
        )
    source_sha256 = sha256_file(source)
    source_ref: dict[str, Any] = {
        "path": str(source),
        "sha256": source_sha256,
        "size_bytes": source.stat().st_size,
    }
    mask_hash = sha256_file(initial_mask)
    mask_video = validation.mask_video
    output = output_dir or layout.work / "matanyone2" / job_id
    output = _owned_path(layout, output, "matting output")
    runtime = RuntimeRef(
        upstream_commit=upstream_commit,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
        pytorch=pytorch,
        cuda=cuda,
        device=device,
        access=runtime_access,
        runtime_approval=(
            RuntimeApprovalRef.model_validate(runtime_approval_ref)
            if runtime_approval_ref is not None
            else None
        ),
    )
    input_video = InputVideo(
        width=int(source_video.get("width") or 0),
        height=int(source_video.get("height") or 0),
        frame_count=source_frame_count,
        frame_rate=_rate_string(source_video),
        duration_us=_stream_duration_us(source_probe, source_video),
    )
    job = MattingJob(
        job_id=job_id,
        config_sha256=config_sha256(layout),
        project_id=layout.root.name,
        revision_id=revision_id,
        input_path=str(source),
        input_sha256=source_sha256,
        input=FileRef.model_validate(source_ref),
        input_video=input_video,
        initial_mask_path=str(initial_mask),
        initial_mask_sha256=mask_hash,
        initial_mask=InitialMask(
            path=str(initial_mask),
            sha256=mask_hash,
            width=int(mask_video["width"]),
            height=int(mask_video["height"]),
            source=mask_source,
        ),
        initial_mask_validation={
            **validation.as_job_field(),
            "commands": commands,
        },
        mask_approval=mask_ref,
        output_dir=str(output),
        source_range=FrameRange(start_frame=0, end_frame=end_frame),
        approval=effect_ref,
        runtime=runtime,
        parameters=MattingParameters.model_validate(dict(parameters or {})),
        device=device,
        start_frame=0,
        end_frame=end_frame,
    )
    payload = job.model_dump(mode="json")
    validate_artifact(package_root, "matting_job", payload)
    return payload


def validate_matting_job(
    package_root: Path,
    payload: Mapping[str, Any],
    *,
    verify_files: bool = True,
) -> MattingJob:
    value = dict(payload)
    validate_artifact(package_root, "matting_job", value)
    try:
        job = MattingJob.model_validate(value)
    except ValueError as exc:
        raise MaskValidationError(f"invalid MatAnyone 2 job: {exc}") from exc
    if verify_files:
        source = Path(job.input_path)
        initial_mask = Path(job.initial_mask_path)
        if not source.is_file() or sha256_file(source) != job.input_sha256:
            raise MaskValidationError("matting job input hash does not match its file")
        if not initial_mask.is_file() or sha256_file(initial_mask) != job.initial_mask_sha256:
            raise MaskValidationError("matting job initial mask hash does not match its file")
        if job.runtime.checkpoint_path and job.runtime.checkpoint_sha256:
            checkpoint = Path(job.runtime.checkpoint_path)
            if not checkpoint.is_file() or sha256_file(checkpoint) != job.runtime.checkpoint_sha256:
                raise MaskValidationError("matting checkpoint hash cannot be verified")
    return job
