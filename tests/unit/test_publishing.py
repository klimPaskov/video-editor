from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit import __version__
from videoedit.adapters.process import ProcessResult
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import (
    config_sha256,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.publishing import (
    _validate_cached_delivery_manifest,
    _validated_metadata_files,
    publish_delivery,
)

ROOT = Path(__file__).resolve().parents[2]


class _PublishingAdapter:
    video_codec = "libx264"
    video_bitrate_bps = 4_000_000

    def encoder_identity(self) -> dict[str, object]:
        return {
            "video_codec": self.video_codec,
            "video_bitrate_bps": self.video_bitrate_bps,
        }

    def full_decode_check(self, path: Path) -> ProcessResult:
        return ProcessResult((str(path),), 0, "", "", 0)

    def probe(self, path: Path) -> dict[str, object]:
        return {
            "streams": [
                {"codec_type": "video", "width": 640, "height": 360},
                {"codec_type": "audio"},
            ]
        }


def _reference(path: Path, artifact_id: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _metadata(layout, *, project_id: str | None = None, revision_id: str = "rev_001") -> dict:
    delivery = layout.root / "delivery"
    delivery.mkdir(exist_ok=True)
    caption_paths = {
        name: delivery / f"captions.{suffix}"
        for name, suffix in (("ass", "ass"), ("webvtt", "vtt"), ("text", "txt"))
    }
    for path in caption_paths.values():
        path.write_text(path.name, encoding="utf-8")
    transcript = delivery / "transcript.json"
    transcript.write_text('{"words": []}\n', encoding="utf-8")
    return {
        "project_id": project_id or layout.root.name,
        "revision_id": revision_id,
        "captions": {
            name: _reference(path, f"caption_{name}") for name, path in caption_paths.items()
        },
        "transcript": _reference(transcript, "final_transcript"),
    }


def test_publishing_metadata_child_hashes_are_verified(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "publish_test")
    metadata = _metadata(layout)

    caption_files, transcript = _validated_metadata_files(layout, metadata, revision_id="rev_001")

    assert set(caption_files) == {"ass", "webvtt", "text"}
    assert transcript.name == "transcript.json"

    Path(str(metadata["captions"]["ass"]["path"])).write_text("changed\n", encoding="utf-8")
    with pytest.raises(PlanningValidationError, match="caption ass is stale"):
        _validated_metadata_files(layout, metadata, revision_id="rev_001")


def test_publishing_metadata_identity_is_bound_to_project_and_revision(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "publish_identity")
    foreign_project = _metadata(layout, project_id="another_project")
    with pytest.raises(PlanningValidationError, match="another project or revision"):
        _validated_metadata_files(layout, foreign_project, revision_id="rev_001")

    foreign_revision = _metadata(layout, revision_id="rev_002")
    with pytest.raises(PlanningValidationError, match="another project or revision"):
        _validated_metadata_files(layout, foreign_revision, revision_id="rev_001")


def test_cached_delivery_manifest_rejects_stale_static_or_output_bindings(
    tmp_path: Path,
) -> None:
    layout = initialize_project(tmp_path, "delivery_cache")
    delivery_dir = layout.output / "delivery" / "rev_001" / "stage"
    delivery_dir.mkdir(parents=True)
    master = delivery_dir / "master.mp4"
    master.write_bytes(b"master")
    provenance = delivery_dir / "provenance.json"
    expected_provenance = {
        "gate3_approval_sha256": "a" * 64,
        "final_qa_sha256": "b" * 64,
        "publishing_metadata_sha256": "c" * 64,
        "delivery_profile_sha256": "d" * 64,
        "candidate_sha256": sha256_file(master),
    }
    provenance.write_text(json.dumps(expected_provenance, indent=2) + "\n", encoding="utf-8")
    checksum = delivery_dir / "checksums.json"
    expected_outputs = {
        "master": ("master", master, sha256_file(master)),
        "provenance": (
            "provenance",
            provenance,
            None,
        ),
        "checksum": ("checksum", checksum, None),
    }
    checksum_files = [
        {
            "role": key,
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for key, (_role, path, _known_sha) in sorted(expected_outputs.items())
        if key != "checksum"
    ]
    write_validated_artifact(
        ROOT,
        "checksum_manifest",
        checksum,
        {
            "schema_name": "checksum_manifest",
            "schema_version": "1.0.0",
            "artifact_id": "art_checksums",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "created_at": "2026-07-25T23:00:00Z",
            "files": checksum_files,
        },
    )
    expected_outputs["checksum"] = (
        "checksum",
        checksum,
        None,
    )
    expected_static = {
        "schema_name": "delivery_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_delivery",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "producer": producer("delivery", "local-filesystem", __version__),
        "inputs": [
            {"artifact_id": "art_gate3", "sha256": "e" * 64},
            {"artifact_id": "art_qa", "sha256": "f" * 64},
        ],
        "config_sha256": config_sha256(layout),
        "final_approval_id": "apr_gate3_001",
        "qa_report_id": "art_qa",
        "profile_id": "profile",
        "source_sha256": "1" * 64,
        "reproducible": True,
        "missing_reproducibility_items": [],
    }
    manifest = {
        **expected_static,
        "created_at": "2026-07-25T23:00:00Z",
        "outputs": [
            {
                "role": role,
                "file": {
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                },
            }
            for role, path, _known_sha in expected_outputs.values()
        ],
    }
    validate_artifact(ROOT, "delivery_manifest", manifest)
    _validate_cached_delivery_manifest(
        ROOT,
        layout,
        manifest,
        expected_static=expected_static,
        expected_outputs=expected_outputs,
        expected_provenance=expected_provenance,
    )

    stale_static = dict(manifest)
    stale_static["profile_id"] = "stale_profile"
    with pytest.raises(StateConflictError, match="delivery manifest exists with stale contents"):
        _validate_cached_delivery_manifest(
            ROOT,
            layout,
            stale_static,
            expected_static=expected_static,
            expected_outputs=expected_outputs,
            expected_provenance=expected_provenance,
        )

    master.write_bytes(b"replacement")
    with pytest.raises(StateConflictError, match="delivery manifest exists with stale contents"):
        _validate_cached_delivery_manifest(
            ROOT,
            layout,
            manifest,
            expected_static=expected_static,
            expected_outputs=expected_outputs,
            expected_provenance=expected_provenance,
        )


def test_publish_delivery_writes_schema_valid_manifest_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    layout = initialize_project(tmp_path, "publish_delivery")
    candidate = layout.output / "candidate.mp4"
    candidate.write_bytes(b"candidate media fixture")
    captions_dir = layout.artifacts / "captions"
    captions_dir.mkdir(parents=True)
    caption_paths = {
        name: captions_dir / suffix
        for name, suffix in (
            ("ass", "captions.ass"),
            ("webvtt", "captions.vtt"),
            ("text", "captions.txt"),
        )
    }
    for path in caption_paths.values():
        path.write_text(path.name, encoding="utf-8")
    transcript = layout.artifacts / "transcript.json"
    transcript.write_text('{"words": []}\n', encoding="utf-8")
    metadata_path = layout.artifacts / "publishing-metadata.json"
    metadata = {
        "schema_name": "publishing_metadata",
        "schema_version": "1.0.0",
        "artifact_id": "art_publishing_metadata",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-25T23:00:00Z",
        "candidate_sha256": sha256_file(candidate),
        "captions": {
            name: _reference(path, f"art_caption_{name}") for name, path in caption_paths.items()
        },
        "transcript": _reference(transcript, "art_final_transcript"),
        "chapters": [],
        "description_draft": "Delivery cache test.",
        "warnings": [],
    }
    write_validated_artifact(ROOT, "publishing_metadata", metadata_path, metadata)

    qa_path = layout.artifacts / "final-qa.json"
    qa = {
        "schema_name": "final_qa_report",
        "schema_version": "1.0.0",
        "artifact_id": "art_final_qa",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-25T23:00:00Z",
        "producer": producer("final-qa", "fixture", "1"),
        "inputs": [{"artifact_id": "art_final_assembly", "sha256": "1" * 64}],
        "candidate": {
            "artifact_id": "art_final_candidate",
            "path": str(candidate.resolve()),
            "sha256": sha256_file(candidate),
            "size_bytes": candidate.stat().st_size,
        },
        "profile_id": "profile",
        "source_sha256": "2" * 64,
        "overall_status": "pass",
        "final_ready": True,
        "findings": [
            {
                "finding_id": "finding_decode",
                "check_code": "MEDIA_DECODE",
                "status": "pass",
                "severity": "info",
                "message": "Fixture media decodes.",
                "required": True,
                "evidence": {"exit_code": 0},
            }
        ],
        "required_failures": 0,
        "warnings_count": 0,
    }
    write_validated_artifact(ROOT, "final_qa_report", qa_path, qa)

    profile_path = layout.config / "delivery-profile.yaml"
    profile_path.write_text("profile: fixture\n", encoding="utf-8")
    gate3_path = layout.review / "gate3-approval.json"
    gate3 = json.loads((ROOT / "examples" / "gate3_approval.example.json").read_text())
    gate3.update(
        {
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "bound_hashes": {
                **gate3["bound_hashes"],
                "candidate_sha256": sha256_file(candidate),
                "final_qa_sha256": sha256_file(qa_path),
                "delivery_profile_sha256": sha256_file(profile_path),
            },
        }
    )
    write_validated_artifact(ROOT, "gate3_approval", gate3_path, gate3)

    adapter = _PublishingAdapter()
    manifest_path = publish_delivery(
        ROOT,
        layout,
        gate3_path,
        qa_path,
        metadata_path,
        profile_path,
        adapter=adapter,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "delivery_manifest", manifest)
    assert (
        publish_delivery(
            ROOT,
            layout,
            gate3_path,
            qa_path,
            metadata_path,
            profile_path,
            adapter=adapter,
        )
        == manifest_path
    )

    tampered = dict(manifest)
    tampered["profile_id"] = "tampered_profile"
    write_validated_artifact(ROOT, "delivery_manifest", manifest_path, tampered)
    with pytest.raises(StateConflictError, match="delivery manifest exists with stale contents"):
        publish_delivery(
            ROOT,
            layout,
            gate3_path,
            qa_path,
            metadata_path,
            profile_path,
            adapter=adapter,
        )
