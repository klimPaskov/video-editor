#!/usr/bin/env python3
"""Run one isolated MatAnyone 2 job through the versioned JSON boundary."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

_WORKERS_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKERS_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKERS_ROOT))

from common_process import PROCESS_ADAPTER_VERSION, WorkerProcessError, run_process  # noqa: E402

CONTRACT_VERSION = "1.1"
WORKER_VERSION = "matanyone2-worker-v1"
OFFICIAL_REPOSITORY = "https://github.com/pq-yang/MatAnyone2"
LEGACY_LIVE_ERROR = (
    "MatAnyone 2 contract 1.0 is validation-only; live inference requires contract 1.1 "
    "with an approved local checkpoint"
)


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "editable-or-unknown"


def package_git_commit(name: str) -> str | None:
    try:
        module = importlib.import_module(name)
        module_file = Path(module.__file__).resolve()
    except (ImportError, TypeError, AttributeError):
        return None
    for candidate in (module_file.parent, *module_file.parents):
        if not (candidate / ".git").exists():
            continue
        try:
            completed = run_process(("git", "rev-parse", "HEAD"), cwd=candidate, timeout_seconds=30)
        except WorkerProcessError:
            continue
        if completed.exit_code == 0:
            return completed.stdout.strip()
    return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_runtime_approval(
    job: dict[str, Any], runtime: dict[str, Any], *, require_files: bool
) -> None:
    reference = runtime.get("runtime_approval")
    if not isinstance(reference, dict):
        raise RuntimeError("MatAnyone 2 runtime approval reference is missing")
    approval_path_value = reference.get("path")
    approval_hash = reference.get("sha256")
    if not isinstance(approval_path_value, str) or not approval_path_value:
        raise RuntimeError("MatAnyone 2 runtime approval path is not declared")
    if not isinstance(approval_hash, str) or not _is_sha256(approval_hash):
        raise RuntimeError("MatAnyone 2 runtime approval SHA-256 is not declared")
    if not require_files:
        return
    approval_path = Path(approval_path_value).expanduser().resolve()
    if not approval_path.is_file():
        raise FileNotFoundError(f"MatAnyone 2 runtime approval is unavailable: {approval_path}")
    if sha256(approval_path) != approval_hash:
        raise RuntimeError("MatAnyone 2 runtime approval SHA-256 cannot be verified")
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("MatAnyone 2 runtime approval is unreadable") from exc
    if not isinstance(approval, dict):
        raise RuntimeError("MatAnyone 2 runtime approval must be an object")
    identity = {
        "worker": "matanyone2",
        "upstream_repository": OFFICIAL_REPOSITORY,
        "upstream_commit": runtime.get("upstream_commit"),
        "checkpoint_id": runtime.get("checkpoint_id"),
        "checkpoint_sha256": runtime.get("checkpoint_sha256"),
        "license_id": "ntu-s-lab-1.0",
        "python": runtime.get("python"),
        "pytorch": runtime.get("pytorch"),
        "cuda": runtime.get("cuda"),
        "device": runtime.get("device"),
    }
    identity_hash = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        approval.get("schema_name") != "worker_runtime_approval"
        or approval.get("schema_version") != "1.0.0"
        or approval.get("worker") != "matanyone2"
        or approval.get("project_id") != job.get("project_id")
        or approval.get("revision_id") != job.get("revision_id")
        or approval.get("decision") != "approved"
        or approval.get("identity_sha256") != identity_hash
    ):
        raise RuntimeError("MatAnyone 2 runtime approval does not match the declared identity")
    config_hash = job.get("config_sha256")
    if not _is_sha256(config_hash) or approval.get("config_sha256") != config_hash:
        raise RuntimeError("MatAnyone 2 runtime approval is stale for the job configuration")
    if "expires_at" not in approval:
        raise RuntimeError("MatAnyone 2 runtime approval expiry is not declared")
    expires_at = approval["expires_at"]
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise RuntimeError("MatAnyone 2 runtime approval expiry is invalid")
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise RuntimeError("MatAnyone 2 runtime approval has expired")
        except ValueError as exc:
            raise RuntimeError("MatAnyone 2 runtime approval expiry is invalid") from exc
    for key, expected in identity.items():
        if approval.get(key) != expected:
            raise RuntimeError(f"MatAnyone 2 runtime approval field does not match: {key}")


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _source_range(job: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
    value = job.get("source_range")
    if isinstance(value, dict):
        start_frame = int(value.get("start_frame", -1))
        end_frame = int(value.get("end_frame", -1))
    else:
        start_frame = int(job.get("start_frame", 0))
        end_value = job.get("end_frame")
        end_frame = int(end_value) if end_value is not None else 2**31 - 1
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("source range must be a non-empty half-open frame range")
    return (
        start_frame,
        end_frame,
        {"start_frame": start_frame, "end_frame": end_frame},
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _path_matches(value: object, expected: Path) -> bool:
    """Compare contract paths after platform-specific canonicalization."""

    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).expanduser().resolve() == expected
    except (OSError, RuntimeError):
        return False


def validate_job(job: dict[str, Any], *, require_files: bool = False) -> None:
    required = ("job_id", "input_path", "initial_mask_path", "output_dir")
    missing = [key for key in required if key not in job]
    if missing:
        raise ValueError(f"Missing required job fields: {', '.join(missing)}")
    version = str(job.get("schema_version", "1.0"))
    if version not in {"1.0", CONTRACT_VERSION}:
        raise ValueError(f"unsupported matting contract version: {version}")
    input_path = Path(str(job["input_path"])).expanduser().resolve()
    mask_path = Path(str(job["initial_mask_path"])).expanduser().resolve()
    start_frame, end_frame, source_range = _source_range(job)
    prompt_frame = int(job.get("initial_mask_frame", 0))
    if prompt_frame != 0:
        raise ValueError("the initial person mask must describe frame 0")
    if version == CONTRACT_VERSION:
        required_v11 = (
            "config_sha256",
            "project_id",
            "revision_id",
            "input_sha256",
            "input",
            "input_video",
            "source_range",
            "initial_mask_sha256",
            "initial_mask",
            "mask_approval",
            "approval",
            "worker",
            "runtime",
            "output_contract",
            "parameters",
            "start_frame",
            "end_frame",
        )
        missing_v11 = [key for key in required_v11 if key not in job]
        if missing_v11:
            raise ValueError(f"MatAnyone 2 contract 1.1 is missing: {', '.join(missing_v11)}")
        if job["source_range"] != source_range:
            raise ValueError("source_range must match the canonical start/end frame fields")
        input_video = job["input_video"]
        if not isinstance(input_video, dict):
            raise ValueError("input_video must be an object")
        if start_frame != 0:
            raise ValueError("MatAnyone 2 first-frame masks require source_range.start_frame == 0")
        if int(input_video.get("width", 0)) <= 0 or int(input_video.get("height", 0)) <= 0:
            raise ValueError("input_video dimensions must be positive")
        if end_frame > int(input_video.get("frame_count", 0)):
            raise ValueError("source range exceeds input video frame count")
        initial_mask = job["initial_mask"]
        if not isinstance(initial_mask, dict):
            raise ValueError("initial_mask must be an object")
        if not _path_matches(initial_mask.get("path"), mask_path):
            raise ValueError("initial_mask.path must match initial_mask_path")
        if initial_mask.get("frame_index") != 0:
            raise ValueError("initial_mask.frame_index must be zero")
        if int(initial_mask.get("width", 0)) != int(input_video.get("width", 0)) or int(
            initial_mask.get("height", 0)
        ) != int(input_video.get("height", 0)):
            raise ValueError("initial mask dimensions must match input video dimensions")
        runtime = job["runtime"]
        if not isinstance(runtime, dict) or runtime.get("access") != "approved":
            raise RuntimeError(
                "MatAnyone 2 runtime gate is not approved; live inference is disabled"
            )
        _verify_runtime_approval(job, runtime, require_files=require_files)
        checkpoint_path = runtime.get("checkpoint_path")
        checkpoint_hash = runtime.get("checkpoint_sha256")
        if not isinstance(checkpoint_path, str) or not checkpoint_path:
            raise RuntimeError("MatAnyone 2 checkpoint_path is not declared")
        if not isinstance(checkpoint_hash, str) or not _is_sha256(checkpoint_hash):
            raise RuntimeError("MatAnyone 2 checkpoint SHA-256 is not declared")
        if not isinstance(runtime.get("upstream_commit"), str) or not re.fullmatch(
            r"[a-f0-9]{40}", runtime["upstream_commit"]
        ):
            raise RuntimeError("MatAnyone 2 upstream commit is not pinned")
        approval = job["approval"]
        if not isinstance(approval, dict) or not _is_sha256(approval.get("sha256")):
            raise RuntimeError("MatAnyone 2 effect approval is not hash-bound")
        input_ref = job["input"]
        if not isinstance(input_ref, dict) or not _path_matches(input_ref.get("path"), input_path):
            raise RuntimeError("MatAnyone 2 input path is not bound to the job")
        if input_ref.get("sha256") != job.get("input_sha256"):
            raise RuntimeError("MatAnyone 2 input hash is not bound to the job")
        if job["initial_mask"].get("sha256") != job.get("initial_mask_sha256"):
            raise RuntimeError("MatAnyone 2 initial mask hash is not bound to the job")
        mask_approval = job["mask_approval"]
        if not isinstance(mask_approval, dict) or not _is_sha256(mask_approval.get("sha256")):
            raise RuntimeError("MatAnyone 2 first-frame mask approval is not hash-bound")
        if require_files:
            checkpoint = Path(checkpoint_path).expanduser().resolve()
            if not checkpoint.is_file():
                raise FileNotFoundError(f"MatAnyone 2 checkpoint is unavailable: {checkpoint}")
            if sha256(checkpoint) != checkpoint_hash:
                raise RuntimeError("MatAnyone 2 checkpoint SHA-256 cannot be verified")
    if require_files:
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        if not mask_path.is_file():
            raise FileNotFoundError(mask_path)
        expected_input_hash = job.get("input_sha256")
        if expected_input_hash is not None and sha256(input_path) != expected_input_hash:
            raise RuntimeError("source hash changed after the matting job was approved")
        expected_mask_hash = job.get("initial_mask_sha256")
        if expected_mask_hash is not None and sha256(mask_path) != expected_mask_hash:
            raise RuntimeError("initial mask hash changed after the matting job was approved")
    if end_frame <= start_frame:
        raise ValueError("source range must be a non-empty half-open frame range")


def _require_live_contract(job: dict[str, Any]) -> None:
    if str(job.get("schema_version", "1.0")) != CONTRACT_VERSION:
        raise RuntimeError(LEGACY_LIVE_ERROR)


def _processor(job: dict[str, Any]) -> Any:
    _require_live_contract(job)
    device = str(job.get("device") or job.get("runtime", {}).get("device") or "cuda:0")
    from matanyone2.inference.inference_core import InferenceCore
    from matanyone2.utils.get_default_model import get_matanyone2_model

    runtime = job["runtime"]
    assert isinstance(runtime, dict)
    checkpoint_path = str(runtime["checkpoint_path"])
    model = get_matanyone2_model(checkpoint_path, device)
    return InferenceCore(model, cfg=model.cfg, device=device)


def _media_candidates(output_dir: Path) -> list[Path]:
    extensions = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
    return sorted(
        path.resolve()
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )


def _within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _output_paths(returned: object, output_dir: Path) -> tuple[Path, Path]:
    candidates: list[Path] = []
    if isinstance(returned, (list, tuple)):
        candidates = [Path(str(value)).expanduser().resolve() for value in returned if value]
    if len(candidates) < 2:
        candidates = _media_candidates(output_dir)
    alpha = next(
        (path for path in candidates if "alpha" in path.name.lower() or "pha" in path.name.lower()),
        None,
    )
    foreground = next(
        (
            path
            for path in candidates
            if "foreground" in path.name.lower() or "fgr" in path.name.lower()
        ),
        None,
    )
    if foreground is None or alpha is None or foreground == alpha:
        raise RuntimeError(
            "MatAnyone 2 did not return independently identifiable foreground and alpha outputs"
        )
    for label, path in (("foreground", foreground), ("alpha", alpha)):
        if not _within(path, output_dir):
            raise RuntimeError(f"MatAnyone 2 {label} output escapes the job output directory")
        if not path.is_file():
            raise FileNotFoundError(f"MatAnyone 2 {label} output is missing: {path}")
    return foreground, alpha


def _probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe is required to record MatAnyone 2 output metadata")
    try:
        completed = run_process(
            (
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path.resolve()),
            ),
            timeout_seconds=120,
            stdout_limit_bytes=4 * 1024 * 1024,
            stderr_limit_bytes=64 * 1024,
        )
    except WorkerProcessError as exc:
        raise RuntimeError(f"ffprobe failed for MatAnyone 2 output: {exc}") from exc
    if completed.exit_code != 0:
        raise RuntimeError(f"ffprobe failed for MatAnyone 2 output: {completed.stderr[-2000:]}")
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    if any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams):
        raise RuntimeError(f"MatAnyone 2 matte output must not contain an audio stream: {path}")
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if not isinstance(video, dict):
        raise RuntimeError(f"MatAnyone 2 output has no video stream: {path}")
    frame_value = video.get("nb_read_frames") or video.get("nb_frames")
    if frame_value in (None, "N/A", ""):
        raise RuntimeError(f"MatAnyone 2 output frame count is unavailable: {path}")
    duration_value = payload.get("format", {}).get("duration", 0)
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "frame_count": int(frame_value),
        "frame_rate": str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
        "duration_us": max(0, round(float(duration_value) * 1_000_000)),
        "pixel_format": str(video.get("pix_fmt") or "unknown"),
    }


def _decode_check(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to decode-check MatAnyone 2 outputs")
    try:
        completed = run_process(
            (ffmpeg, "-v", "error", "-i", str(path.resolve()), "-map", "0:v:0", "-f", "null", "-"),
            timeout_seconds=300,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
        )
    except WorkerProcessError as exc:
        raise RuntimeError(f"MatAnyone 2 output failed decode: {exc}") from exc
    if completed.exit_code != 0:
        raise RuntimeError(f"MatAnyone 2 output failed decode: {completed.stderr[-2000:]}")


def _output_ref(path: Path) -> dict[str, Any]:
    metadata = _probe_media(path)
    _decode_check(path)
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        **metadata,
    }


def _parse_rate(value: object) -> Fraction | None:
    try:
        text = str(value)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _is_gray_pixel_format(value: object) -> bool:
    normalized = str(value or "").lower()
    return normalized.startswith("gray") or normalized in {"yuv400p", "yuvj400p"}


def _validate_output_bounds(job: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> None:
    input_video = job.get("input_video")
    source_range = job.get("source_range")
    if not isinstance(input_video, dict) or not isinstance(source_range, dict):
        raise RuntimeError("MatAnyone 2 output bounds cannot be checked without input metadata")
    expected_width = int(input_video.get("width", 0))
    expected_height = int(input_video.get("height", 0))
    expected_count = int(source_range.get("end_frame", -1)) - int(
        source_range.get("start_frame", -1)
    )
    expected_rate = _parse_rate(input_video.get("frame_rate"))
    if expected_width <= 0 or expected_height <= 0 or expected_count <= 0 or expected_rate is None:
        raise RuntimeError("MatAnyone 2 input metadata is insufficient for output validation")
    expected_duration_us = round(
        expected_count * expected_rate.denominator * 1_000_000 / expected_rate.numerator
    )
    duration_tolerance_us = max(
        100_000,
        round(2 * expected_rate.denominator * 1_000_000 / expected_rate.numerator),
    )
    for role, reference in outputs.items():
        if (
            int(reference.get("width", 0)) != expected_width
            or int(reference.get("height", 0)) != expected_height
        ):
            raise RuntimeError(f"MatAnyone 2 {role} output dimensions do not match the input video")
        if int(reference.get("frame_count", 0)) != expected_count:
            raise RuntimeError(
                f"MatAnyone 2 {role} output frame count does not match the approved source range"
            )
        observed_rate = _parse_rate(reference.get("frame_rate"))
        if observed_rate != expected_rate:
            raise RuntimeError(
                f"MatAnyone 2 {role} output frame rate does not match the input video"
            )
        if abs(int(reference.get("duration_us", 0)) - expected_duration_us) > duration_tolerance_us:
            raise RuntimeError(
                f"MatAnyone 2 {role} output duration does not match the source range"
            )
        if role == "alpha" and not _is_gray_pixel_format(reference.get("pixel_format")):
            raise RuntimeError("MatAnyone 2 alpha output is not an explicitly grayscale video")
        if role == "foreground" and _is_gray_pixel_format(reference.get("pixel_format")):
            raise RuntimeError("MatAnyone 2 foreground output is unexpectedly grayscale")


def _stage_bounded_input(job: dict[str, Any], output_dir: Path) -> tuple[Path, dict[str, Any]]:
    input_path = Path(str(job["input_path"])).expanduser().resolve()
    input_video = job.get("input_video")
    source_range = job.get("source_range")
    if not isinstance(input_video, dict) or not isinstance(source_range, dict):
        raise RuntimeError("MatAnyone 2 bounded input staging requires v1.1 input metadata")
    start_frame = int(source_range.get("start_frame", -1))
    end_frame = int(source_range.get("end_frame", -1))
    input_frame_count = int(input_video.get("frame_count", 0))
    if start_frame != 0:
        raise RuntimeError("MatAnyone 2 bounded input staging requires a frame-zero source range")
    if end_frame <= start_frame or end_frame > input_frame_count:
        raise RuntimeError("MatAnyone 2 bounded input staging received an invalid source range")
    if end_frame == input_frame_count:
        return input_path, {"staged": False, "path": str(input_path), "sha256": sha256(input_path)}

    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = str(job["input_sha256"])
    staged_path = output_dir / f"bounded-input-{source_hash[:16]}-{start_frame}-{end_frame}.mp4"

    def valid_staged(path: Path) -> bool:
        try:
            metadata = _probe_media(path)
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            metadata["width"] == int(input_video["width"])
            and metadata["height"] == int(input_video["height"])
            and metadata["frame_count"] == end_frame - start_frame
            and _parse_rate(metadata["frame_rate"]) == _parse_rate(input_video["frame_rate"])
        )

    if staged_path.is_file() and valid_staged(staged_path):
        return staged_path, {
            "staged": True,
            "path": str(staged_path),
            "sha256": sha256(staged_path),
            "source_range": {"start_frame": start_frame, "end_frame": end_frame},
        }

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to stage a bounded MatAnyone 2 input")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{staged_path.stem}.", suffix=".mp4", dir=output_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        completed = run_process(
            (
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(input_path),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS",
                "-frames:v",
                str(end_frame - start_frame),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-map_metadata",
                "-1",
                "-y",
                str(temporary),
            ),
            cwd=output_dir,
            timeout_seconds=600,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=256 * 1024,
        )
        if completed.exit_code != 0:
            raise RuntimeError(
                f"bounded MatAnyone 2 input staging failed: {completed.stderr[-2000:]}"
            )
        if not valid_staged(temporary):
            raise RuntimeError("bounded MatAnyone 2 input staging produced invalid media")
        os.replace(temporary, staged_path)
    finally:
        temporary.unlink(missing_ok=True)
    return staged_path, {
        "staged": True,
        "path": str(staged_path),
        "sha256": sha256(staged_path),
        "source_range": {"start_frame": start_frame, "end_frame": end_frame},
    }


def _pending_verification() -> dict[str, str]:
    return {
        "status": "pending",
        "foreground_role": "pending",
        "alpha_role": "pending",
        "alpha_polarity": "unknown",
        "dimensions": "pass",
        "frame_count": "pass",
        "decode": "pass",
        "contrasting_background": "pending",
    }


def _pending_diagnostics(start_frame: int, end_frame: int) -> dict[str, Any]:
    middle = start_frame + max(0, (end_frame - start_frame - 1) // 2)
    return {
        "review_frame_indices": sorted({start_frame, middle, end_frame - 1}),
        "warnings": [
            "Foreground and alpha roles require independent contrasting-background review.",
            "Matte stability requires human inspection of hair, fingers, clothing, holes, blur, "
            "entry/exit, and temporal edges.",
        ],
        "stability": {
            "status": "pending",
            "hair": "pending",
            "fingers": "pending",
            "clothing": "pending",
            "holes": "pending",
            "transparent_regions": "pending",
            "fast_motion": "pending",
            "motion_blur": "pending",
            "entry_exit": "pending",
            "temporal_edges": "pending",
        },
    }


def run(job: dict[str, Any]) -> dict[str, Any]:
    validate_job(job, require_files=True)
    _require_live_contract(job)
    input_path = Path(str(job["input_path"])).expanduser().resolve()
    mask_path = Path(str(job["initial_mask_path"])).expanduser().resolve()
    output_dir = Path(str(job["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    processing_input_path, input_stage = _stage_bounded_input(job, output_dir)
    processor = _processor(job)
    parameters = job.get("parameters", {}) if isinstance(job.get("parameters"), dict) else {}
    process_kwargs: dict[str, Any] = {
        "input_path": str(processing_input_path),
        "mask_path": str(mask_path),
        "output_path": str(output_dir),
    }
    for key in ("max_size", "n_warmup", "r_erode", "r_dilate"):
        value = parameters.get(key, job.get(key))
        if value is not None:
            process_kwargs[key] = int(value)
    returned = processor.process_video(**process_kwargs)
    foreground, alpha = _output_paths(returned, output_dir)
    version = str(job.get("schema_version", "1.0"))
    if version == CONTRACT_VERSION:
        foreground_ref = _output_ref(foreground)
        alpha_ref = _output_ref(alpha)
        output_refs = {"foreground": foreground_ref, "alpha": alpha_ref}
        _validate_output_bounds(job, output_refs)
        raw_path = output_dir / "raw-worker-metadata.json"
        raw_metadata = {
            "worker": WORKER_VERSION,
            "job_id": job["job_id"],
            "returned": str(returned),
            "input_stage": input_stage,
            "foreground": foreground_ref,
            "alpha": alpha_ref,
        }
        atomic_json_write(raw_path, raw_metadata)
        runtime = job["runtime"]
        assert isinstance(runtime, dict)
        start_frame, end_frame, source_range = _source_range(job)
        result: dict[str, Any] = {
            "schema_version": CONTRACT_VERSION,
            "job_id": job["job_id"],
            "status": "complete",
            "worker": "matanyone2",
            "config_sha256": job["config_sha256"],
            "project_id": job["project_id"],
            "revision_id": job["revision_id"],
            "input_path": str(input_path),
            "input_sha256": sha256(input_path),
            "input": job["input"],
            "input_video": job["input_video"],
            "source_range": source_range,
            "initial_mask_path": str(mask_path),
            "initial_mask_sha256": sha256(mask_path),
            "initial_mask": job["initial_mask"],
            "mask_approval": job["mask_approval"],
            "output_dir": str(output_dir),
            "foreground_path": str(foreground),
            "alpha_path": str(alpha),
            "media_outputs": [str(foreground), str(alpha)],
            "outputs": output_refs,
            "output_contract": job["output_contract"],
            "verification": _pending_verification(),
            "diagnostics": _pending_diagnostics(start_frame, end_frame),
            "upstream_return": str(returned),
            "model_id": str(job.get("model_id", "PeiqingYang/MatAnyone2")),
            "device": str(runtime.get("device") or job.get("device") or "cuda:0"),
            "software": {
                "worker": WORKER_VERSION,
                "matanyone2": package_version("matanyone2"),
                "upstream_repository": OFFICIAL_REPOSITORY,
                "upstream_commit": runtime.get("upstream_commit"),
                "checkpoint_id": runtime.get("checkpoint_id"),
                "checkpoint_sha256": runtime.get("checkpoint_sha256"),
                "python": sys.version.split()[0],
                "pytorch": package_version("torch"),
                "cuda": runtime.get("cuda"),
                "device": runtime.get("device"),
                "process_adapter": PROCESS_ADAPTER_VERSION,
            },
            "raw_worker_metadata": {
                "path": str(raw_path.resolve()),
                "sha256": sha256(raw_path),
            },
        }
    else:
        result = {
            "schema_version": "1.0",
            "job_id": job["job_id"],
            "status": "complete",
            "worker": "matanyone2",
            "input_path": str(input_path),
            "input_sha256": sha256(input_path),
            "initial_mask_path": str(mask_path),
            "initial_mask_sha256": sha256(mask_path),
            "output_dir": str(output_dir),
            "foreground_path": str(foreground),
            "alpha_path": str(alpha),
            "media_outputs": [str(path) for path in _media_candidates(output_dir)],
            "upstream_return": str(returned),
            "model_id": str(job.get("model_id", "PeiqingYang/MatAnyone2")),
            "device": str(job.get("device", "cuda:0")),
            "software": {
                "worker": WORKER_VERSION,
                "matanyone2": package_version("matanyone2"),
                "upstream_commit": package_git_commit("matanyone2"),
                "python": sys.version.split()[0],
                "process_adapter": PROCESS_ADAPTER_VERSION,
            },
        }
    result_path = output_dir / "matting-result.json"
    atomic_json_write(result_path, result)
    return result


def failure_payload(job: dict[str, Any], error: Exception) -> dict[str, Any]:
    version = str(job.get("schema_version", "1.0"))
    failure: dict[str, Any] = {
        "schema_version": version,
        "job_id": job.get("job_id", "unknown"),
        "status": "failed",
        "worker": "matanyone2",
        "error": str(error),
        "traceback": traceback.format_exc(),
    }
    if version == CONTRACT_VERSION:
        source_range = job.get("source_range", {"start_frame": 0, "end_frame": 1})
        failure.update(
            {
                "config_sha256": job.get("config_sha256", "0" * 64),
                "project_id": job.get("project_id", "unknown_project"),
                "revision_id": job.get("revision_id", "rev_001"),
                "input_path": str(job.get("input_path", "unknown")),
                "input_sha256": job.get("input_sha256"),
                "input": job.get(
                    "input",
                    {"path": str(job.get("input_path", "unknown")), "sha256": "0" * 64},
                ),
                "input_video": job.get(
                    "input_video",
                    {"width": 1, "height": 1, "frame_count": 1, "frame_rate": "1/1"},
                ),
                "source_range": source_range,
                "initial_mask_path": str(job.get("initial_mask_path", "unknown")),
                "initial_mask_sha256": job.get("initial_mask_sha256", "0" * 64),
                "initial_mask": job.get(
                    "initial_mask",
                    {
                        "path": str(job.get("initial_mask_path", "unknown")),
                        "sha256": "0" * 64,
                        "width": 1,
                        "height": 1,
                        "frame_index": 0,
                        "polarity": "white_foreground",
                        "source": "manual",
                    },
                ),
                "mask_approval": job.get(
                    "mask_approval", {"artifact_id": "unknown_mask", "sha256": "0" * 64}
                ),
                "output_dir": str(job.get("output_dir", ".")),
                "outputs": {"foreground": None, "alpha": None},
                "output_contract": job.get(
                    "output_contract",
                    {
                        "foreground_format": "video_rgb",
                        "alpha_format": "video_gray",
                        "alpha_polarity": "white_foreground",
                    },
                ),
                "verification": {
                    "status": "pending",
                    "foreground_role": "pending",
                    "alpha_role": "pending",
                    "alpha_polarity": "unknown",
                    "dimensions": "pending",
                    "frame_count": "pending",
                    "decode": "pending",
                    "contrasting_background": "pending",
                },
                "diagnostics": {
                    "review_frame_indices": [
                        int(source_range.get("start_frame", 0)),
                    ],
                    "warnings": [str(error)],
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
                },
                "software": {
                    "worker": WORKER_VERSION,
                    "python": sys.version.split()[0],
                    "process_adapter": PROCESS_ADAPTER_VERSION,
                },
                "raw_worker_metadata": {},
            }
        )
    return failure


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        loaded = json.loads(args.job.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("matting job must be a JSON object")
        job = loaded
        validate_job(job)
        if args.dry_run:
            print(json.dumps({"status": "valid", "job_id": job["job_id"]}, indent=2))
            return 0
        result = run(job)
    except Exception as exc:
        job = locals().get("job", {})
        if not isinstance(job, dict):
            job = {}
        failure = failure_payload(job, exc)
        destination = args.result or Path(job.get("output_dir", ".")) / "matting-result.json"
        atomic_json_write(destination, failure)
        print(json.dumps(failure, indent=2), file=sys.stderr)
        return 1
    if args.result:
        atomic_json_write(args.result.resolve(), result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
