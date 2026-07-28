#!/usr/bin/env python3
"""Reference adapter draft for one SAM 3.1 segmentation and tracking job.

Verify this code against the pinned official upstream revision before live use. The worker
lives outside the Python 3.11 core and emits lossless masks plus normalized JSON geometry.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import math
import os
import re
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WORKERS_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKERS_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKERS_ROOT))

from common_process import PROCESS_ADAPTER_VERSION, WorkerProcessError, run_process  # noqa: E402

CONTRACT_VERSION = "1.1"
WORKER_VERSION = "sam3-worker-v1"
OFFICIAL_REPOSITORY = "https://github.com/facebookresearch/sam3"
LEGACY_LIVE_ERROR = (
    "SAM 3.1 contract 1.0 is validation-only; live inference requires contract 1.1 "
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


def _verify_runtime_approval(
    job: dict[str, Any], runtime: dict[str, Any], *, require_files: bool
) -> None:
    reference = runtime.get("runtime_approval")
    if not isinstance(reference, dict):
        raise RuntimeError("SAM 3.1 runtime approval reference is missing")
    approval_path_value = reference.get("path")
    approval_hash = reference.get("sha256")
    if not isinstance(approval_path_value, str) or not approval_path_value:
        raise RuntimeError("SAM 3.1 runtime approval path is not declared")
    if not isinstance(approval_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", approval_hash):
        raise RuntimeError("SAM 3.1 runtime approval SHA-256 is not declared")
    if not require_files:
        return
    approval_path = Path(approval_path_value).expanduser().resolve()
    if not approval_path.is_file():
        raise FileNotFoundError(f"SAM 3.1 runtime approval is unavailable: {approval_path}")
    if sha256(approval_path) != approval_hash:
        raise RuntimeError("SAM 3.1 runtime approval SHA-256 cannot be verified")
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("SAM 3.1 runtime approval is unreadable") from exc
    if not isinstance(approval, dict):
        raise RuntimeError("SAM 3.1 runtime approval must be an object")
    identity = {
        "worker": "sam3",
        "upstream_repository": OFFICIAL_REPOSITORY,
        "upstream_commit": runtime.get("upstream_commit"),
        "checkpoint_id": runtime.get("checkpoint_id"),
        "checkpoint_sha256": runtime.get("checkpoint_sha256"),
        "license_id": "meta-sam-2025-11-19",
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
        or approval.get("worker") != "sam3"
        or approval.get("project_id") != job.get("project_id")
        or approval.get("revision_id") != job.get("revision_id")
        or approval.get("decision") != "approved"
        or approval.get("identity_sha256") != identity_hash
    ):
        raise RuntimeError("SAM 3.1 runtime approval does not match the declared identity")
    config_hash = job.get("config_sha256")
    if not _is_sha256(config_hash) or approval.get("config_sha256") != config_hash:
        raise RuntimeError("SAM 3.1 runtime approval is stale for the job configuration")
    if "expires_at" not in approval:
        raise RuntimeError("SAM 3.1 runtime approval expiry is not declared")
    expires_at = approval["expires_at"]
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise RuntimeError("SAM 3.1 runtime approval expiry is invalid")
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise RuntimeError("SAM 3.1 runtime approval has expired")
        except ValueError as exc:
            raise RuntimeError("SAM 3.1 runtime approval expiry is invalid") from exc
    for key, expected in identity.items():
        if approval.get(key) != expected:
            raise RuntimeError(f"SAM 3.1 runtime approval field does not match: {key}")


def as_numpy(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return value


def find_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def json_safe(value: Any) -> Any:
    """Keep raw predictor metadata while excluding tensor/device internals."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    device = getattr(value, "device", None)
    if shape is not None:
        return {
            "type": type(value).__name__,
            "shape": [int(item) for item in shape],
            "dtype": str(dtype) if dtype is not None else None,
            "device": str(device) if device is not None else None,
        }
    return {"type": type(value).__name__, "repr": repr(value)[:500]}


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


def source_and_range(job: dict[str, Any]) -> tuple[Path, int, int, dict[str, Any]]:
    input_value = job.get("input")
    if isinstance(input_value, dict):
        input_path = input_value.get("path")
    else:
        input_path = job.get("input_path")
    if not isinstance(input_path, str) or not input_path:
        raise ValueError("input.path or input_path is required")
    source_range = job.get("source_range")
    if isinstance(source_range, dict):
        start_frame = int(source_range.get("start_frame", -1))
        end_frame = int(source_range.get("end_frame", -1))
    else:
        start_frame = int(job.get("start_frame", 0))
        end_value = job.get("end_frame")
        end_frame = int(end_value) if end_value is not None else 2**31 - 1
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("source range must be a non-empty half-open frame range")
    normalized_range = (
        source_range
        if isinstance(source_range, dict)
        else {"start_frame": start_frame, "end_frame": end_frame}
    )
    return Path(input_path).expanduser().resolve(), start_frame, end_frame, normalized_range


def normalize_masks(output: dict[str, Any]) -> tuple[list[Any], list[int]]:
    import numpy as np

    raw_masks = find_first(
        output,
        (
            "masks",
            "binary_masks",
            "out_binary_masks",
            "pred_masks",
            "mask_logits",
        ),
    )
    if raw_masks is None:
        raise KeyError(f"No supported mask key found. Output keys: {sorted(output)}")
    array = np.asarray(as_numpy(raw_masks))
    while array.ndim > 3 and array.shape[1] == 1:
        array = array[:, 0]
    if array.ndim == 2:
        array = array[None, ...]
    if array.ndim != 3:
        raise ValueError(f"Expected masks with shape [N,H,W], got {array.shape}")

    if array.dtype == np.bool_:
        binary = array
    else:
        threshold = 0.0 if float(array.min()) < 0.0 else 0.5
        binary = array > threshold

    raw_ids = find_first(output, ("object_ids", "obj_ids", "out_obj_ids", "ids"))
    if raw_ids is None:
        object_ids = list(range(1, len(binary) + 1))
    else:
        object_ids = [int(value) for value in np.asarray(as_numpy(raw_ids)).reshape(-1)]
        if len(object_ids) != len(binary):
            object_ids = list(range(1, len(binary) + 1))
    return [mask for mask in binary], object_ids


def geometry(mask: Any) -> dict[str, Any]:
    import numpy as np

    y, x = np.where(mask)
    if len(x) == 0:
        return {"visible": False, "area_pixels": 0, "bbox_xywh": None, "centroid_xy": None}
    x0, x1 = int(x.min()), int(x.max())
    y0, y1 = int(y.min()), int(y.max())
    return {
        "visible": True,
        "area_pixels": int(mask.sum()),
        "bbox_xywh": [x0, y0, x1 - x0 + 1, y1 - y0 + 1],
        "centroid_xy": [float(x.mean()), float(y.mean())],
    }


def save_frame(
    output_dir: Path,
    frame_index: int,
    output: dict[str, Any],
    *,
    expected_shape: tuple[int, int] | None = None,
) -> dict[str, Any]:
    import numpy as np

    masks, object_ids = normalize_masks(output)
    if not masks:
        raise ValueError(f"SAM 3.1 returned no masks for frame {frame_index}")
    if expected_shape is not None and any(tuple(mask.shape) != expected_shape for mask in masks):
        raise ValueError(
            f"SAM 3.1 mask dimensions do not match the input video at frame {frame_index}"
        )
    if any(int(object_id) <= 0 for object_id in object_ids):
        raise ValueError(f"SAM 3.1 returned a non-positive object ID at frame {frame_index}")
    if len(set(object_ids)) != len(object_ids):
        raise ValueError(f"SAM 3.1 returned duplicate object IDs at frame {frame_index}")
    from PIL import Image

    combined = np.zeros_like(masks[0], dtype=np.uint8)
    instances_dir = output_dir / "instances"
    masks_dir = output_dir / "masks"
    instances_dir.mkdir(parents=True, exist_ok=True)
    masks_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for mask, object_id in zip(masks, object_ids, strict=True):
        pixels = mask.astype(np.uint8) * 255
        combined = np.maximum(combined, pixels)
        instance_path = instances_dir / f"{frame_index:06d}-object-{object_id}.png"
        Image.fromarray(pixels, mode="L").save(instance_path)
        records.append(
            {
                "object_id": object_id,
                "mask_path": str(instance_path.resolve()),
                "mask_sha256": sha256(instance_path),
                **geometry(mask),
            }
        )

    combined_path = masks_dir / f"{frame_index:06d}.png"
    Image.fromarray(combined, mode="L").save(combined_path)
    return {
        "frame_index": frame_index,
        "combined_mask_path": str(combined_path.resolve()),
        "combined_mask_sha256": sha256(combined_path),
        "objects": records,
    }


def validate_job(job: dict[str, Any], *, require_files: bool = False) -> None:
    required = ("job_id", "output_dir", "prompt")
    missing = [key for key in required if key not in job]
    if missing:
        raise ValueError(f"Missing required job fields: {', '.join(missing)}")
    version = str(job.get("schema_version", "1.0"))
    if version not in {"1.0", CONTRACT_VERSION}:
        raise ValueError(f"unsupported segmentation contract version: {version}")
    source, start_frame, end_frame, _ = source_and_range(job)
    if require_files and not source.is_file():
        raise FileNotFoundError(source)
    prompt = job["prompt"]
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be an object")
    prompt_type = str(prompt.get("type", "text"))
    prompt_frame = int(prompt.get("frame_index", 0))
    if not start_frame <= prompt_frame < end_frame:
        raise ValueError("prompt.frame_index must be inside source_range")
    if prompt_type == "text" and not prompt.get("text"):
        raise ValueError("text prompt requires prompt.text")
    if prompt_type == "point":
        points = prompt.get("points")
        labels = prompt.get("point_labels")
        if not isinstance(points, list) or not points:
            raise ValueError("point prompt requires nonempty prompt.points")
        if not isinstance(labels, list) or len(labels) != len(points):
            raise ValueError("point prompt labels must align with prompt.points")
    if prompt_type not in {"text", "point"}:
        raise ValueError(
            "the verified SAM 3.1 adapter currently supports text and point prompts; "
            f"unsupported prompt type: {prompt_type}"
        )
    if version == CONTRACT_VERSION:
        required_v11 = (
            "config_sha256",
            "project_id",
            "revision_id",
            "input",
            "input_sha256",
            "input_video",
            "source_range",
            "runtime",
            "approval",
            "start_frame",
            "end_frame",
        )
        missing_v11 = [key for key in required_v11 if key not in job]
        if missing_v11:
            raise ValueError(f"SAM 3.1 contract 1.1 is missing: {', '.join(missing_v11)}")
        if job["source_range"] != {
            "start_frame": start_frame,
            "end_frame": end_frame,
        }:
            raise ValueError("source_range must match the canonical start/end frame fields")
        input_video = job["input_video"]
        if not isinstance(input_video, dict):
            raise ValueError("input_video must be an object")
        frame_count = int(input_video.get("frame_count", 0))
        if end_frame > frame_count:
            raise ValueError("source range exceeds input video frame count")
        width = int(input_video.get("width", 0))
        height = int(input_video.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError("input_video dimensions must be positive")
        runtime = job["runtime"]
        if not isinstance(runtime, dict) or runtime.get("access") != "approved":
            raise RuntimeError("SAM 3.1 runtime gate is not approved; live inference is disabled")
        _verify_runtime_approval(job, runtime, require_files=require_files)
        checkpoint_path = runtime.get("checkpoint_path")
        checkpoint_hash = runtime.get("checkpoint_sha256")
        if not isinstance(checkpoint_path, str) or not checkpoint_path:
            raise RuntimeError("SAM 3.1 checkpoint_path is not declared")
        checkpoint = Path(checkpoint_path).expanduser().resolve()
        if require_files and not checkpoint.is_file():
            raise FileNotFoundError(f"SAM 3.1 checkpoint is unavailable: {checkpoint}")
        if require_files and (
            not isinstance(checkpoint_hash, str) or sha256(checkpoint) != checkpoint_hash
        ):
            raise RuntimeError("SAM 3.1 checkpoint SHA-256 cannot be verified")
        if not isinstance(runtime.get("upstream_commit"), str):
            raise RuntimeError("SAM 3.1 upstream commit is not pinned")
        approval = job["approval"]
        if not isinstance(approval, dict) or not isinstance(approval.get("sha256"), str):
            raise RuntimeError("SAM 3.1 effect approval is not hash-bound")
        input_value = job["input"]
        input_hash = job["input_sha256"]
        if not _is_sha256(input_hash):
            raise RuntimeError("SAM 3.1 input SHA-256 is not declared")
        if (
            not isinstance(input_value, dict)
            or not _path_matches(input_value.get("path"), source)
            or input_value.get("sha256") != input_hash
        ):
            raise RuntimeError("SAM 3.1 input path is not bound to the source range")
        if require_files and sha256(source) != input_hash:
            raise RuntimeError("source hash changed after the SAM 3.1 job was approved")


def _require_live_contract(job: dict[str, Any]) -> None:
    if str(job.get("schema_version", "1.0")) != CONTRACT_VERSION:
        raise RuntimeError(LEGACY_LIVE_ERROR)


def build_predictor(job: dict[str, Any]) -> Any:
    """Build only through an explicit, checkpoint-bound upstream API."""

    _require_live_contract(job)
    from sam3 import model_builder

    builder = getattr(model_builder, "build_sam3_multiplex_video_predictor", None)
    if builder is None:
        raise RuntimeError(
            "the accepted SAM 3.1 code does not expose build_sam3_multiplex_video_predictor"
        )
    runtime = job.get("runtime", {})
    if not isinstance(runtime, dict):
        raise RuntimeError("runtime metadata is missing")
    checkpoint_path = runtime.get("checkpoint_path")
    if not isinstance(checkpoint_path, str):
        raise RuntimeError("checkpoint_path is required for an explicit model binding")
    signature = inspect.signature(builder)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if "checkpoint_path" in signature.parameters or accepts_kwargs:
        kwargs["checkpoint_path"] = checkpoint_path
    elif "model_path" in signature.parameters:
        kwargs["model_path"] = checkpoint_path
    else:
        raise RuntimeError(
            "SAM 3.1 predictor builder does not expose an explicit checkpoint argument"
        )
    if "gpus_to_use" in signature.parameters and job.get("gpus") is not None:
        kwargs["gpus_to_use"] = job["gpus"]
    try:
        return builder(**kwargs)
    except TypeError as exc:
        raise RuntimeError(
            "SAM 3.1 predictor API rejected the declared checkpoint binding; "
            "verify the pinned upstream revision"
        ) from exc


def prompt_request(job: dict[str, Any], session_id: str) -> dict[str, Any]:
    prompt = job["prompt"]
    request: dict[str, Any] = {
        "type": "add_prompt",
        "session_id": session_id,
        "frame_index": int(prompt.get("frame_index", 0)),
    }
    prompt_type = str(prompt.get("type", "text"))
    if prompt_type == "text":
        request["text"] = str(prompt["text"])
        return request
    if prompt_type != "point":
        raise RuntimeError(f"unsupported prompt type for the verified adapter: {prompt_type}")
    import torch

    request["points"] = torch.tensor(prompt["points"], dtype=torch.float32)
    request["point_labels"] = torch.tensor(prompt["point_labels"], dtype=torch.int32)
    if prompt.get("object_id") is not None:
        request["obj_id"] = int(prompt["object_id"])
    return request


def tracking_diagnostics(
    frames: list[dict[str, Any]], expected_object_count: int
) -> dict[str, Any]:
    ordered = sorted(frames, key=lambda item: int(item["frame_index"]))
    missing_frames: list[int] = []
    identity_warnings: list[str] = []
    area_jump_frames: list[int] = []
    centroid_jump_frames: list[int] = []
    previous_ids: set[int] | None = None
    previous: dict[int, tuple[float, float, int]] = {}
    if ordered:
        expected = range(int(ordered[0]["frame_index"]), int(ordered[-1]["frame_index"]) + 1)
        present = {int(item["frame_index"]) for item in ordered}
        missing_frames = [index for index in expected if index not in present]
    for frame in ordered:
        frame_index = int(frame["frame_index"])
        visible = [item for item in frame.get("objects", []) if item.get("visible")]
        current_ids = {int(item["object_id"]) for item in visible}
        if len(current_ids) > expected_object_count:
            identity_warnings.append(f"frame {frame_index} exceeds expected object count")
        if previous_ids is not None and previous_ids - current_ids and current_ids - previous_ids:
            identity_warnings.append(f"possible identity switch at frame {frame_index}")
        previous_ids = current_ids
        for item in visible:
            centroid = item.get("centroid_xy")
            area = int(item.get("area_pixels", 0))
            if not isinstance(centroid, list | tuple) or len(centroid) != 2 or area <= 0:
                continue
            object_id = int(item["object_id"])
            prior = previous.get(object_id)
            if prior is not None:
                prior_x, prior_y, prior_area = prior
                if max(area, prior_area) / min(area, prior_area) > 2.5:
                    area_jump_frames.append(frame_index)
                if math.hypot(float(centroid[0]) - prior_x, float(centroid[1]) - prior_y) > max(
                    50.0, math.sqrt(max(area, prior_area)) * 5
                ):
                    centroid_jump_frames.append(frame_index)
            previous[object_id] = (float(centroid[0]), float(centroid[1]), area)
    return {
        "missing_frames": sorted(set(missing_frames)),
        "identity_warnings": list(dict.fromkeys(identity_warnings)),
        "area_jump_frames": sorted(set(area_jump_frames)),
        "centroid_jump_frames": sorted(set(centroid_jump_frames)),
        "leak_warnings": [],
        "status": "warning"
        if missing_frames or identity_warnings or area_jump_frames or centroid_jump_frames
        else "pass",
    }


def run(job: dict[str, Any]) -> dict[str, Any]:
    validate_job(job, require_files=True)
    _require_live_contract(job)
    input_path, minimum_frame, maximum_frame, source_range = source_and_range(job)
    output_dir = Path(job["output_dir"]).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    actual_input_hash = sha256(input_path)
    expected_input = job.get("input", {})
    if isinstance(expected_input, dict) and expected_input.get("sha256") not in (
        None,
        actual_input_hash,
    ):
        raise RuntimeError("source hash changed after the job was approved")
    input_video = job.get("input_video")
    expected_shape: tuple[int, int] | None = None
    if isinstance(input_video, dict):
        expected_shape = (int(input_video["height"]), int(input_video["width"]))

    predictor = build_predictor(job)
    session_id: str | None = None
    frames: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    try:
        start = predictor.handle_request(
            request={"type": "start_session", "resource_path": str(input_path)}
        )
        session_id = str(start["session_id"])
        add_request = prompt_request(job, session_id)
        response = predictor.handle_request(request=add_request)
        raw_outputs.append(
            {
                "operation": "add_prompt",
                "frame_index": int(add_request["frame_index"]),
                "response": json_safe(response),
            }
        )
        prompt_frame = int(add_request["frame_index"])
        if minimum_frame <= prompt_frame < maximum_frame:
            frames.append(
                save_frame(
                    output_dir,
                    prompt_frame,
                    response["outputs"],
                    expected_shape=expected_shape,
                )
            )
        for propagated in predictor.handle_stream_request(
            request={"type": "propagate_in_video", "session_id": session_id}
        ):
            frame_index = int(propagated["frame_index"])
            if frame_index < minimum_frame:
                continue
            if frame_index >= maximum_frame:
                break
            raw_outputs.append(
                {
                    "operation": "propagate_in_video",
                    "frame_index": frame_index,
                    "response": json_safe(propagated),
                }
            )
            frames.append(
                save_frame(
                    output_dir,
                    frame_index,
                    propagated["outputs"],
                    expected_shape=expected_shape,
                )
            )
    finally:
        if session_id is not None:
            try:
                predictor.handle_request(
                    request={"type": "close_session", "session_id": session_id}
                )
            except Exception:
                pass
        shutdown = getattr(predictor, "shutdown", None)
        if callable(shutdown):
            shutdown()

    deduplicated = {item["frame_index"]: item for item in frames}
    ordered_frames = [deduplicated[index] for index in sorted(deduplicated)]
    if not ordered_frames:
        raise RuntimeError("SAM 3.1 returned no mask frames for the approved source range")
    raw_metadata_path = output_dir / "raw-worker-metadata.json"
    raw_metadata = {
        "worker": "sam3",
        "job_id": job["job_id"],
        "source_range": source_range,
        "responses": raw_outputs,
    }
    atomic_json_write(raw_metadata_path, raw_metadata)
    runtime = job.get("runtime", {}) if isinstance(job.get("runtime"), dict) else {}
    diagnostics = tracking_diagnostics(
        ordered_frames,
        expected_object_count=int(job.get("expected_object_count", 1)),
    )
    review_indices = {
        minimum_frame,
        maximum_frame - 1,
        int(job["prompt"].get("frame_index", minimum_frame)),
    }
    for key in ("area_jump_frames", "centroid_jump_frames"):
        review_indices.update(int(index) for index in diagnostics[key])
    diagnostics["review_frame_indices"] = sorted(
        index for index in review_indices if minimum_frame <= index < maximum_frame
    )
    schema_version = str(job.get("schema_version", "1.0"))
    result = {
        "schema_version": schema_version,
        "job_id": job["job_id"],
        "status": "complete",
        "worker": "sam3",
        "input_path": str(input_path),
        "input_sha256": actual_input_hash,
        "input": {
            "path": str(input_path),
            "sha256": actual_input_hash,
            "size_bytes": input_path.stat().st_size,
        },
        "source_range": source_range,
        "prompt": job["prompt"],
        "mask_pattern": str((output_dir / "masks" / "%06d.png").resolve()),
        "frame_count": len(ordered_frames),
        "frames": ordered_frames,
        "software": {
            "worker": WORKER_VERSION,
            "sam3": package_version("sam3"),
            "upstream_repository": OFFICIAL_REPOSITORY,
            "upstream_commit": runtime.get("upstream_commit") or package_git_commit("sam3"),
            "checkpoint_id": runtime.get("checkpoint_id"),
            "checkpoint_sha256": runtime.get("checkpoint_sha256"),
            "python": sys.version.split()[0],
            "pytorch": package_version("torch"),
            "device": str(os.environ.get("CUDA_VISIBLE_DEVICES", "cuda")),
            "process_adapter": PROCESS_ADAPTER_VERSION,
        },
        "output": {
            "mask_format": "png_gray8",
            "lossless": True,
            "mask_pattern": str((output_dir / "masks" / "%06d.png").resolve()),
            "mask_count": len(ordered_frames),
            "raw_metadata_path": str(raw_metadata_path.resolve()),
            "raw_metadata_sha256": sha256(raw_metadata_path),
        },
        "diagnostics": diagnostics,
        "raw_worker_metadata": {
            "path": str(raw_metadata_path.resolve()),
            "sha256": sha256(raw_metadata_path),
        },
    }
    if schema_version == CONTRACT_VERSION:
        result["project_id"] = job["project_id"]
        result["revision_id"] = job["revision_id"]
        result["input_video"] = job["input_video"]
    result_path = output_dir / "segmentation-result.json"
    atomic_json_write(result_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    validate_job(job)
    if args.dry_run:
        print(json.dumps({"status": "valid", "job_id": job["job_id"]}, indent=2))
        return 0
    try:
        result = run(job)
    except Exception as exc:
        failure = {
            "schema_version": str(job.get("schema_version", "1.0")),
            "job_id": job.get("job_id", "unknown"),
            "status": "failed",
            "worker": "sam3",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        destination = args.result or Path(job.get("output_dir", ".")) / "segmentation-result.json"
        atomic_json_write(destination, failure)
        print(json.dumps(failure, indent=2), file=sys.stderr)
        return 1
    if args.result:
        atomic_json_write(args.result, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
