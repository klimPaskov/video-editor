from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file


def approve_final_render(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    actor: str,
    role: str = "editor",
    reason: str = "Approved after final review",
    revision_id: str = "rev_001",
) -> Path:
    render_manifest_path = render_manifest_path.resolve()
    render_manifest = json.loads(render_manifest_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "render_manifest", render_manifest)
    qa_path = layout.artifacts / "qa-report.json"
    if not qa_path.is_file():
        raise FileNotFoundError("qa-report.json is missing. Run project QA first.")
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "qa_report", qa)
    if not qa["final_ready"]:
        raise ValueError("QA is not final-ready")
    render_hash = sha256_file(render_manifest_path)
    payload = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": "art_approval_final",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("final-approval", "human-review"),
        "inputs": [
            artifact_input(render_manifest["artifact_id"], render_manifest_path),
            artifact_input("art_qa", qa_path),
        ],
        "config_sha256": config_sha256(layout),
        "approval_id": "apr_final_001",
        "approval_type": "final",
        "actor": actor,
        "role": role,
        "decision": "approved",
        "reason": reason,
        "approved_item_type": "render_manifest",
        "approved_item_sha256": render_hash,
        "expires_at": None,
        "budget": None,
    }
    output = layout.artifacts / "final-approval.json"
    write_validated_artifact(package_root, "approval_record", output, payload)
    return output
