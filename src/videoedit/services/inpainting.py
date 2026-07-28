from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.inpainting import DisabledInpaintingAdapter
from videoedit.errors import (
    ApprovalRequiredError,
    InpaintingValidationError,
    StaleApprovalError,
    WorkerContractError,
)
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file

INPAINTING_SCHEMA_VERSION = "1.0.0"
INPAINTING_IMPLEMENTATION_VERSION = f"{__version__}:inpainting-v1"


@dataclass(frozen=True, slots=True)
class InpaintingAuthorization:
    request_sha256: str
    effect_approval: dict[str, str]
    spend_approval: dict[str, str]


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InpaintingValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise InpaintingValidationError(f"{label} must be a JSON object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise InpaintingValidationError(f"{label} escapes the project: {resolved}") from exc
    return resolved


def _file_ref(path: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise InpaintingValidationError(f"{label} is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _require_current_ref(reference: object, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(reference, Mapping):
        raise StaleApprovalError(f"{label} reference is missing")
    current = _file_ref(path, label)
    if any(reference.get(name) != current[name] for name in ("path", "sha256", "size_bytes")):
        raise StaleApprovalError(f"{label} is stale for the current file")
    return current


def _video_payload(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise InpaintingValidationError(f"mask validation is missing {field}")
    required = (
        "codec",
        "width",
        "height",
        "frame_rate",
        "pixel_format",
        "frame_count",
        "duration_us",
    )
    if any(name not in value for name in required):
        raise InpaintingValidationError(f"mask validation {field} metadata is incomplete")
    return dict(value)


def _validate_mask_validation(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    validation_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    validation = _read_object(validation_path, "mask validation")
    validate_artifact(package_root, "mask_validation", validation)
    if validation.get("status") != "complete":
        raise InpaintingValidationError("inpainting requires a complete mask validation")
    source_ref = validation.get("source")
    mask_ref = validation.get("mask")
    if not isinstance(source_ref, Mapping) or not isinstance(mask_ref, Mapping):
        raise InpaintingValidationError("mask validation is missing source or mask references")
    source_path = _owned_path(layout, Path(str(source_ref["path"])), "mask-validation source")
    mask_path = _owned_path(layout, Path(str(mask_ref["path"])), "mask-validation mask")
    if source_path != source:
        raise StaleApprovalError("mask validation source does not match the inpainting source")
    source_current = _require_current_ref(source_ref, source, "inpainting source")
    mask_current = _require_current_ref(mask_ref, mask_path, "inpainting mask")
    validation_values = validation.get("validation")
    if not isinstance(validation_values, Mapping) or any(
        value != "pass" for value in validation_values.values()
    ):
        raise InpaintingValidationError("inpainting requires every mask validation check to pass")
    source_video = _video_payload(validation, "source_video")
    mask_video = _video_payload(validation, "mask_video")
    return (
        validation,
        source_current,
        mask_current,
        {
            "source_video": source_video,
            "mask_video": mask_video,
            "mask_path": str(mask_path),
        },
    )


def _request_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "schema_name",
        "schema_version",
        "project_id",
        "revision_id",
        "request_id",
        "idempotency_key",
        "provider",
        "model",
        "prompt",
        "source",
        "mask",
        "mask_validation",
        "source_video",
        "mask_video",
        "source_range",
        "fallback",
        "network",
        "config_sha256",
    )
    return {field: payload[field] for field in fields}


def _request_output_path(layout: ProjectLayout, request_id: str, output: Path | None) -> Path:
    if output is None:
        return layout.artifacts / "inpainting" / f"{request_id}.json"
    return _owned_path(layout, output, "inpainting request output")


def plan_inpainting_request(
    package_root: Path,
    layout: ProjectLayout,
    source_path: Path,
    mask_validation_path: Path,
    *,
    start_frame: int,
    end_frame: int,
    prompt: str,
    provider: str = "disabled",
    model: str = "operator-configured",
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Persist an approval-bound, disabled-by-default inpainting proposal."""

    if start_frame < 0 or end_frame <= start_frame:
        raise InpaintingValidationError("inpainting source range must be nonempty and half-open")
    if not prompt.strip():
        raise InpaintingValidationError("inpainting prompt must not be empty")
    if not provider.strip() or not model.strip():
        raise InpaintingValidationError("inpainting provider and model must not be empty")
    source = _owned_path(layout, source_path, "inpainting source")
    validation_path = _owned_path(layout, mask_validation_path, "mask validation")
    validation, source_ref, mask_ref, video = _validate_mask_validation(
        package_root, layout, source, validation_path
    )
    source_video = video["source_video"]
    mask_video = video["mask_video"]
    source_frame_count = int(source_video["frame_count"])
    mask_frame_count = int(mask_video["frame_count"])
    if end_frame > source_frame_count or end_frame > mask_frame_count:
        raise InpaintingValidationError("inpainting source range exceeds validated mask frames")
    if int(source_video["width"]) != int(mask_video["width"]) or int(source_video["height"]) != int(
        mask_video["height"]
    ):
        raise InpaintingValidationError("inpainting source and mask dimensions differ")
    source_range = {"start_frame": start_frame, "end_frame": end_frame}
    mask_validation_ref = _file_ref(validation_path, "mask validation")
    input_hashes = [
        source_ref["sha256"],
        mask_ref["sha256"],
        mask_validation_ref["sha256"],
    ]
    stage_key = make_stage_key(
        "inpainting_request",
        INPAINTING_IMPLEMENTATION_VERSION,
        input_hashes,
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "source_range": source_range,
        },
    )
    request_id = f"inp_{stage_key[:24]}"
    output_path = _request_output_path(layout, request_id, output).resolve()
    fallback = {
        "mode": "original_shot",
        "on_uncertain": "keep_original",
        "original_visible": True,
    }
    network = {"enabled": False, "mode": "disabled"}
    binding: dict[str, Any] = {
        "schema_name": "inpainting_request",
        "schema_version": INPAINTING_SCHEMA_VERSION,
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "request_id": request_id,
        "idempotency_key": f"{layout.root.name}-{revision_id}-{request_id}",
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "source": source_ref,
        "mask": mask_ref,
        "mask_validation": mask_validation_ref,
        "source_video": source_video,
        "mask_video": mask_video,
        "source_range": source_range,
        "fallback": fallback,
        "network": network,
        "config_sha256": config_sha256(layout),
    }
    request_hash = canonical_sha256(binding)
    existing = output_path if output_path.is_file() else None
    if existing is not None:
        current = _read_object(existing, "existing inpainting request")
        validate_artifact(package_root, "inpainting_request", current)
        if current.get("request_sha256") == request_hash:
            return existing
        raise InpaintingValidationError(
            f"inpainting request output already contains a different request: {existing}"
        )
    payload: dict[str, Any] = {
        **binding,
        "artifact_id": "art_inpainting_request",
        "created_at": now_iso(),
        "producer": producer(
            "optional-inpainting", "inpainting-boundary", INPAINTING_IMPLEMENTATION_VERSION
        ),
        "inputs": [
            artifact_input("art_inpainting_source", source),
            artifact_input("art_inpainting_mask", Path(str(mask_ref["path"]))),
            artifact_input("art_inpainting_mask_validation", validation_path),
        ],
        "status": "awaiting_approval",
        "request_sha256": request_hash,
        "approvals": {"effect": None, "spend": None},
        "estimated_cost": None,
        "output": None,
        "warnings": [
            "optional_provider_disabled_by_default",
            "original_shot_is_required_fallback",
            "inpainting_request_requires_current_effect_and_spend_approval",
        ],
    }
    validate_artifact(package_root, "inpainting_request", payload)
    write_validated_artifact(package_root, "inpainting_request", output_path, payload)
    del validation
    return output_path


def _read_approval(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    *,
    request_hash: str,
    expected_type: str,
    label: str,
) -> dict[str, str]:
    approval_path = _owned_path(layout, path, label)
    approval = _read_object(approval_path, label)
    validate_artifact(package_root, "approval_record", approval)
    if approval.get("decision") != "approved":
        raise ApprovalRequiredError(f"{label} is not approved")
    approval_type = approval.get("approval_type")
    if expected_type == "edit":
        allowed_types = {"edit", "edit_batch"}
    else:
        allowed_types = {expected_type}
    if approval_type not in allowed_types:
        raise ApprovalRequiredError(f"{label} has the wrong approval type")
    if approval.get("approved_item_sha256") != request_hash:
        raise StaleApprovalError(f"{label} is stale for the current inpainting request")
    expires_at = approval.get("expires_at")
    if isinstance(expires_at, str):
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise StaleApprovalError(f"{label} has expired")
        except ValueError as exc:
            raise StaleApprovalError(f"{label} has an invalid expiry") from exc
    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise ApprovalRequiredError(f"{label} has no approval_id")
    return {"approval_id": approval_id, "sha256": sha256_file(approval_path)}


def authorize_inpainting_request(
    package_root: Path,
    layout: ProjectLayout,
    request_path: Path,
    effect_approval_path: Path,
    spend_approval_path: Path,
) -> InpaintingAuthorization:
    """Validate the two independent current approvals without submitting work."""

    resolved_request = _owned_path(layout, request_path, "inpainting request")
    request = _read_object(resolved_request, "inpainting request")
    validate_artifact(package_root, "inpainting_request", request)
    if request.get("status") != "awaiting_approval":
        raise ApprovalRequiredError("inpainting request is not awaiting approval")
    if request.get("fallback") != {
        "mode": "original_shot",
        "on_uncertain": "keep_original",
        "original_visible": True,
    }:
        raise InpaintingValidationError("inpainting must retain the original-shot fallback")
    request_hash = str(request["request_sha256"])
    effect = _read_approval(
        package_root,
        layout,
        effect_approval_path,
        request_hash=request_hash,
        expected_type="edit",
        label="inpainting effect approval",
    )
    spend = _read_approval(
        package_root,
        layout,
        spend_approval_path,
        request_hash=request_hash,
        expected_type="spend",
        label="inpainting spend approval",
    )
    return InpaintingAuthorization(request_hash, effect, spend)


def submit_inpainting_request(
    package_root: Path,
    layout: ProjectLayout,
    request_path: Path,
    effect_approval_path: Path,
    spend_approval_path: Path,
    *,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Submit only after request-bound effect and spend approvals are current."""

    authorize_inpainting_request(
        package_root,
        layout,
        request_path,
        effect_approval_path,
        spend_approval_path,
    )
    selected = adapter or DisabledInpaintingAdapter()
    response = selected.submit(request_path.resolve())
    if not isinstance(response, dict):
        raise WorkerContractError("inpainting adapter result must be a JSON object")
    request = _read_object(request_path.resolve(), "inpainting request")
    response_request_id = response.get("request_id")
    if response_request_id is not None and response_request_id != request.get("request_id"):
        raise WorkerContractError("inpainting adapter response is for a different request")
    return response


def default_inpainting_adapter() -> DisabledInpaintingAdapter:
    return DisabledInpaintingAdapter()
