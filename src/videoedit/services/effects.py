from __future__ import annotations

import json
from pathlib import Path

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.artifacts import validate_artifact
from videoedit.services.matting import verify_matting_payload
from videoedit.services.project import sha256_file


def encode_segmentation_masks(
    package_root: Path,
    result_path: Path,
    output_path: Path,
    *,
    fps: int,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("segmentation result must be a JSON object")
    validate_artifact(package_root, "segmentation_result", payload)
    if payload.get("status") != "complete":
        raise ValueError("segmentation result is not complete")
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("segmentation result contains no frames")
    frame_indices = [int(frame["frame_index"]) for frame in frames]
    first_frame = min(frame_indices)
    mask_pattern = Path(str(payload["mask_pattern"]))
    selected = adapter or FFmpegAdapter()
    selected.encode_mask_sequence(
        mask_pattern,
        output_path,
        fps=fps,
        start_number=first_frame,
        frame_count=len(frames),
    )
    return output_path


def prepare_matting_overlay(
    package_root: Path,
    result_path: Path,
    output_path: Path,
    *,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matting result must be a JSON object")
    validate_artifact(package_root, "matting_result", payload)
    if payload.get("status") != "complete":
        raise ValueError("matting result is not complete")
    selected = adapter or FFmpegAdapter()
    if payload.get("schema_version") == "1.1":
        verification = payload.get("verification")
        if not isinstance(verification, dict) or verification.get("status") != "pass":
            raise ValueError(
                "matting result output roles and alpha semantics are not independently verified"
            )
        outputs = payload.get("outputs")
        if not isinstance(outputs, dict):
            raise ValueError("matting result is missing hash-bound output references")
        for role, path_field in (("foreground", "foreground_path"), ("alpha", "alpha_path")):
            reference = outputs.get(role)
            declared_path = payload.get(path_field)
            if not isinstance(reference, dict) or reference.get("path") != declared_path:
                raise ValueError(f"matting {role} output reference is not path-bound")
            declared_output_path = Path(str(declared_path))
            if not declared_output_path.is_file():
                raise ValueError(f"matting {role} output is missing: {declared_output_path}")
            if sha256_file(declared_output_path) != reference.get("sha256"):
                raise ValueError(f"matting {role} output hash does not match the result")
        verified = verify_matting_payload(package_root, payload, adapter=selected)
        verified_state = verified.get("verification")
        if not isinstance(verified_state, dict) or verified_state.get("status") != "pass":
            raise ValueError("matting output structural verification is stale or failed")
    foreground = payload.get("foreground_path")
    alpha = payload.get("alpha_path")
    if not foreground or not alpha:
        raise ValueError("matting result must identify foreground_path and alpha_path")
    selected.attach_alpha(Path(str(foreground)), Path(str(alpha)), output_path)
    return output_path
