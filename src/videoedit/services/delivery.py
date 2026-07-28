from __future__ import annotations

import json
import shutil
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


def build_delivery(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    profile_id: str = "pro_youtube_1080p",
    revision_id: str = "rev_001",
) -> Path:
    render_manifest_path = render_manifest_path.resolve()
    render_manifest = json.loads(render_manifest_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "render_manifest", render_manifest)
    qa_path = layout.artifacts / "qa-report.json"
    approval_path = layout.artifacts / "final-approval.json"
    source_path = layout.artifacts / "source-manifest.json"
    for required in (qa_path, approval_path, source_path):
        if not required.is_file():
            raise FileNotFoundError(required)
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    source = json.loads(source_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "qa_report", qa)
    validate_artifact(package_root, "approval_record", approval)
    validate_artifact(package_root, "source_manifest", source)
    if not qa["final_ready"]:
        raise ValueError("delivery is blocked because QA is not final-ready")
    if approval["decision"] != "approved" or approval["approval_type"] != "final":
        raise ValueError("delivery is blocked because final approval is missing")
    if approval["approved_item_sha256"] != sha256_file(render_manifest_path):
        raise ValueError("final approval is stale for the selected render")

    media_path = Path(render_manifest["output"]["path"])
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    delivery_dir = layout.output / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    master_path = delivery_dir / f"{layout.root.name}-master.mp4"
    shutil.copy2(media_path, master_path)
    qa_copy = delivery_dir / "qa-report.json"
    shutil.copy2(qa_path, qa_copy)
    provenance = delivery_dir / "provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "render_manifest": render_manifest,
                "qa_report_sha256": sha256_file(qa_path),
                "final_approval": approval,
                "source_sha256": source["sha256"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    outputs = []
    for role, path in (
        ("master", master_path),
        ("qa_report", qa_copy),
        ("provenance", provenance),
    ):
        outputs.append(
            {
                "role": role,
                "file": {
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                },
            }
        )
    payload = {
        "schema_name": "delivery_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_delivery",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("delivery", "local-filesystem"),
        "inputs": [
            artifact_input(render_manifest["artifact_id"], render_manifest_path),
            artifact_input("art_qa", qa_path),
            artifact_input("art_approval_final", approval_path),
        ],
        "config_sha256": config_sha256(layout),
        "final_approval_id": approval["approval_id"],
        "qa_report_id": qa["artifact_id"],
        "profile_id": profile_id,
        "outputs": outputs,
        "source_sha256": source["sha256"],
        "reproducible": True,
        "missing_reproducibility_items": [],
    }
    output = layout.artifacts / "delivery-manifest.json"
    write_validated_artifact(package_root, "delivery_manifest", output, payload)
    return output
