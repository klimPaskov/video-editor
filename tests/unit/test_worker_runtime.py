from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import StaleApprovalError, StateConflictError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import initialize_project
from videoedit.services.worker_runtime import (
    approve_worker_runtime,
    runtime_identity_sha256,
    validate_worker_runtime_approval,
)

ROOT = Path(__file__).resolve().parents[2]


def _identity(checkpoint_sha256: str) -> dict[str, str]:
    return {
        "worker": "sam3",
        "upstream_repository": "https://github.com/facebookresearch/sam3",
        "upstream_commit": "46957e47805eaa273f4aa7bbbd25a88bca9108ce",
        "checkpoint_id": "facebook/sam3.1/sam3.1_multiplex.pt",
        "checkpoint_sha256": checkpoint_sha256,
        "license_id": "meta-sam-2025-11-19",
        "python": "3.12",
        "pytorch": "2.7.0+cu126",
        "cuda": "12.6",
        "device": "cuda:0",
    }


def test_runtime_approval_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "worker_runtime_fixture")
    checkpoint_hash = "a" * 64
    approval_path = approve_worker_runtime(
        ROOT,
        layout,
        worker="sam3",
        upstream_commit=_identity(checkpoint_hash)["upstream_commit"],
        checkpoint_id=_identity(checkpoint_hash)["checkpoint_id"],
        checkpoint_sha256=checkpoint_hash,
        pytorch="2.7.0+cu126",
        cuda="12.6",
        device="cuda:0",
        actor="operator@example.test",
        role="licence-owner",
        reason="Accepted fixture identity",
    )
    second = approve_worker_runtime(
        ROOT,
        layout,
        worker="sam3",
        upstream_commit=_identity(checkpoint_hash)["upstream_commit"],
        checkpoint_id=_identity(checkpoint_hash)["checkpoint_id"],
        checkpoint_sha256=checkpoint_hash,
        pytorch="2.7.0+cu126",
        cuda="12.6",
        device="cuda:0",
        actor="operator@example.test",
        role="licence-owner",
        reason="Accepted fixture identity",
    )
    assert second == approval_path
    payload = json.loads(approval_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "worker_runtime_approval", payload)
    assert payload["identity_sha256"] == runtime_identity_sha256(_identity(checkpoint_hash))
    reference = validate_worker_runtime_approval(
        ROOT,
        layout,
        approval_path,
        worker="sam3",
        upstream_commit=_identity(checkpoint_hash)["upstream_commit"],
        checkpoint_id=_identity(checkpoint_hash)["checkpoint_id"],
        checkpoint_sha256=checkpoint_hash,
        pytorch="2.7.0+cu126",
        cuda="12.6",
        device="cuda:0",
    )
    assert reference["path"] == str(approval_path.resolve())


def test_runtime_approval_rejects_stale_identity_and_conflicting_reviewer(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "worker_runtime_conflict")
    checkpoint_hash = "b" * 64
    kwargs = {
        "worker": "matanyone2",
        "upstream_commit": "d3bb5a1ebedf259a5453c6d168e6840fff85581e",
        "checkpoint_id": "matanyone2.pth",
        "checkpoint_sha256": checkpoint_hash,
        "pytorch": "fixture-pytorch",
        "cuda": "fixture-cuda",
        "device": "cuda:0",
        "actor": "operator@example.test",
        "role": "licence-owner",
        "reason": "Accepted fixture identity",
    }
    approval_path = approve_worker_runtime(ROOT, layout, **kwargs)
    (layout.config / "project.yaml").write_text("project: changed\n", encoding="utf-8")
    with pytest.raises(StaleApprovalError, match="project configuration"):
        validate_worker_runtime_approval(
            ROOT,
            layout,
            approval_path,
            worker="matanyone2",
            upstream_commit=kwargs["upstream_commit"],
            checkpoint_id=kwargs["checkpoint_id"],
            checkpoint_sha256=checkpoint_hash,
            pytorch=kwargs["pytorch"],
            cuda=kwargs["cuda"],
            device=kwargs["device"],
        )
    with pytest.raises(StaleApprovalError, match="stale"):
        validate_worker_runtime_approval(
            ROOT,
            layout,
            approval_path,
            worker="matanyone2",
            upstream_commit=kwargs["upstream_commit"],
            checkpoint_id=kwargs["checkpoint_id"],
            checkpoint_sha256="c" * 64,
            pytorch=kwargs["pytorch"],
            cuda=kwargs["cuda"],
            device=kwargs["device"],
        )
    with pytest.raises(StateConflictError, match="different identity or reviewer"):
        approve_worker_runtime(ROOT, layout, **{**kwargs, "actor": "other@example.test"})
