from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.inpainting import CommandInpaintingAdapter
from videoedit.adapters.process import ProcessRequest, ProcessResult
from videoedit.errors import DependencyUnavailableError, StaleApprovalError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.inpainting import (
    authorize_inpainting_request,
    plan_inpainting_request,
    submit_inpainting_request,
)
from videoedit.services.project import ProjectLayout, sha256_file


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _file_ref(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path, Path]:
    layout = ProjectLayout(tmp_path / "project")
    source = layout.raw / "source.mp4"
    mask = layout.work / "mask.mkv"
    source.parent.mkdir(parents=True)
    mask.parent.mkdir(parents=True)
    source.write_bytes(b"source-fixture")
    mask.write_bytes(b"mask-fixture")
    source_ref = _file_ref(source)
    mask_ref = _file_ref(mask)
    validation = {
        "schema_name": "mask_validation",
        "schema_version": "1.0.0",
        "artifact_id": "art_mask_validation",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-24T10:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "mask-validation",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "inputs": [
            {"artifact_id": "art_source", "sha256": source_ref["sha256"]},
            {"artifact_id": "art_mask", "sha256": mask_ref["sha256"]},
        ],
        "config_sha256": "0" * 64,
        "status": "complete",
        "source": source_ref,
        "mask": mask_ref,
        "source_video": {
            "codec": "h264",
            "width": 1280,
            "height": 720,
            "frame_rate": {"numerator": 30, "denominator": 1},
            "pixel_format": "yuv420p",
            "frame_count": 30,
            "duration_us": 1_000_000,
        },
        "mask_video": {
            "codec": "ffv1",
            "width": 1280,
            "height": 720,
            "frame_rate": {"numerator": 30, "denominator": 1},
            "pixel_format": "gray",
            "frame_count": 30,
            "duration_us": 1_000_000,
            "lossless": True,
        },
        "configuration": {
            "expected_polarity": "white_foreground",
            "sample_frame_indices": [0, 15, 29],
        },
        "mask_statistics": {
            "min": 0.0,
            "max": 255.0,
            "mean": 5.0,
            "polarity": "white_foreground",
            "sampled_frames": 3,
        },
        "validation": {
            "full_decode": "pass",
            "lossless": "pass",
            "pixel_format": "pass",
            "dimensions": "pass",
            "frame_count": "pass",
            "frame_rate": "pass",
            "range": "pass",
            "polarity": "pass",
            "duration": "pass",
        },
        "commands": [],
        "warnings": [],
    }
    validation_path = layout.work / "mask-validation.json"
    write_validated_artifact(package_root(), "mask_validation", validation_path, validation)
    return layout, source, mask, validation_path


def _approval(
    layout: ProjectLayout,
    filename: str,
    approval_id: str,
    approval_type: str,
    request_hash: str,
) -> Path:
    payload = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": f"art_{approval_id}",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-24T10:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture-approval",
            "adapter": "human-review",
            "adapter_version": "1",
        },
        "inputs": [{"artifact_id": "art_inpainting_request", "sha256": request_hash}],
        "config_sha256": "0" * 64,
        "approval_id": approval_id,
        "approval_type": approval_type,
        "actor": "fixture-reviewer",
        "role": "editor",
        "decision": "approved",
        "reason": "Fixture boundary test only",
        "approved_item_type": "inpainting_request",
        "approved_item_sha256": request_hash,
        "expires_at": None,
        "budget": {"currency": "USD", "maximum_amount": "1.00"}
        if approval_type == "spend"
        else None,
    }
    path = layout.artifacts / filename
    write_validated_artifact(package_root(), "approval_record", path, payload)
    return path


def test_plan_inpainting_is_hash_bound_idempotent_and_keeps_original_fallback(
    tmp_path: Path,
) -> None:
    layout, source, _mask, validation = _fixture(tmp_path)

    first = plan_inpainting_request(
        package_root(),
        layout,
        source,
        validation,
        start_frame=0,
        end_frame=30,
        prompt="Fill the reviewed object region.",
    )
    first_hash = sha256_file(first)
    second = plan_inpainting_request(
        package_root(),
        layout,
        source,
        validation,
        start_frame=0,
        end_frame=30,
        prompt="Fill the reviewed object region.",
    )

    payload = json.loads(first.read_text(encoding="utf-8"))
    assert second == first
    assert sha256_file(second) == first_hash
    assert payload["status"] == "awaiting_approval"
    assert payload["network"] == {"enabled": False, "mode": "disabled"}
    assert payload["fallback"] == {
        "mode": "original_shot",
        "on_uncertain": "keep_original",
        "original_visible": True,
    }


def test_inpainting_authorization_requires_current_effect_and_spend_approvals(
    tmp_path: Path,
) -> None:
    layout, source, _mask, validation = _fixture(tmp_path)
    request_path = plan_inpainting_request(
        package_root(),
        layout,
        source,
        validation,
        start_frame=0,
        end_frame=30,
        prompt="Fill the reviewed object region.",
    )
    request_hash = json.loads(request_path.read_text(encoding="utf-8"))["request_sha256"]
    effect = _approval(layout, "effect-approval.json", "apr_inpaint_effect", "edit", request_hash)
    spend = _approval(layout, "spend-approval.json", "apr_inpaint_spend", "spend", request_hash)

    authorized = authorize_inpainting_request(package_root(), layout, request_path, effect, spend)
    assert authorized.request_sha256 == request_hash
    assert authorized.effect_approval["approval_id"] == "apr_inpaint_effect"
    assert authorized.spend_approval["approval_id"] == "apr_inpaint_spend"

    with pytest.raises(DependencyUnavailableError, match="disabled"):
        submit_inpainting_request(package_root(), layout, request_path, effect, spend)


def test_inpainting_rejects_stale_mask_and_stale_approval(tmp_path: Path) -> None:
    layout, source, mask, validation = _fixture(tmp_path)
    request_path = plan_inpainting_request(
        package_root(),
        layout,
        source,
        validation,
        start_frame=0,
        end_frame=30,
        prompt="Fill the reviewed object region.",
    )
    mask.write_bytes(b"changed-mask")
    with pytest.raises(StaleApprovalError, match="inpainting mask"):
        plan_inpainting_request(
            package_root(),
            layout,
            source,
            validation,
            start_frame=0,
            end_frame=30,
            prompt="Fill the reviewed object region.",
            output=layout.work / "changed-request.json",
        )

    request = json.loads(request_path.read_text(encoding="utf-8"))
    effect = _approval(layout, "effect-approval.json", "apr_inpaint_effect", "edit", "1" * 64)
    spend = _approval(
        layout,
        "spend-approval.json",
        "apr_inpaint_spend",
        "spend",
        request["request_sha256"],
    )
    with pytest.raises(StaleApprovalError, match="effect approval"):
        authorize_inpainting_request(package_root(), layout, request_path, effect, spend)


class _FakeRunner:
    def __init__(self) -> None:
        self.request: ProcessRequest | None = None

    def run(self, request: ProcessRequest) -> ProcessResult:
        self.request = request
        return ProcessResult(
            arguments=(request.executable, *request.arguments),
            exit_code=0,
            stdout=json.dumps({"request_id": "inp_request_001", "status": "submitted"}),
            stderr="",
            elapsed_ms=1,
        )


def test_command_inpainting_adapter_is_process_typed_and_network_gated(
    tmp_path: Path,
) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    fake = _FakeRunner()
    adapter = CommandInpaintingAdapter(
        ("provider-cli", "submit"), network_enabled=True, runner=fake
    )

    response = adapter.submit(request)

    assert response["status"] == "submitted"
    assert fake.request is not None
    assert fake.request.arguments[-1] == str(request.resolve())
