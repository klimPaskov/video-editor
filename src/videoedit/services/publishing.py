from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_lock import _owned_path

PUBLISH_DELIVERY_IMPLEMENTATION_VERSION = "p11-05d"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _file_ref(path: Path, artifact_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise PlanningValidationError(f"delivery file does not exist: {path}")
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _validate_cached_delivery_manifest(
    package_root: Path,
    layout: ProjectLayout,
    current: Mapping[str, Any],
    *,
    expected_static: Mapping[str, Any],
    expected_outputs: Mapping[str, tuple[str, Path, str | None]],
    expected_provenance: Mapping[str, str],
) -> None:
    """Fail closed unless a cached delivery still matches its live outputs."""

    current_static = {key: current.get(key) for key in expected_static}
    if current_static != dict(expected_static):
        raise StateConflictError("delivery manifest exists with stale contents")

    output_entries = current.get("outputs")
    if not isinstance(output_entries, list) or len(output_entries) != len(expected_outputs):
        raise StateConflictError("delivery manifest exists with stale contents")
    expected_by_path = {
        str(path.resolve()): (role, path, known_sha)
        for role, path, known_sha in expected_outputs.values()
    }
    if len(expected_by_path) != len(expected_outputs):
        raise PlanningValidationError("delivery output paths must be unique")
    seen_paths: set[str] = set()
    for entry in output_entries:
        if not isinstance(entry, Mapping):
            raise StateConflictError("delivery manifest exists with stale contents")
        file_ref = entry.get("file")
        if not isinstance(file_ref, Mapping):
            raise StateConflictError("delivery manifest exists with stale contents")
        path_value = str(file_ref.get("path", ""))
        resolved_path = str(Path(path_value).expanduser().resolve())
        expected = expected_by_path.get(resolved_path)
        if expected is None or resolved_path in seen_paths:
            raise StateConflictError("delivery manifest exists with stale contents")
        expected_role, path, known_sha = expected
        if entry.get("role") != expected_role or path_value != resolved_path:
            raise StateConflictError("delivery manifest exists with stale contents")
        selected = _owned_path(layout, path, "cached delivery output")
        if not selected.is_file():
            raise StateConflictError("delivery manifest exists with stale contents")
        actual_sha = sha256_file(selected)
        actual_size = selected.stat().st_size
        if file_ref.get("sha256") != actual_sha or file_ref.get("size_bytes") != actual_size:
            raise StateConflictError("delivery manifest exists with stale contents")
        if known_sha is not None and actual_sha != known_sha:
            raise StateConflictError("delivery manifest exists with stale contents")
        seen_paths.add(resolved_path)

        if expected_role == "provenance":
            provenance = _read_object(selected, "cached delivery provenance")
            if provenance != dict(expected_provenance):
                raise StateConflictError("delivery manifest exists with stale contents")

    expected_checksum_files = [
        {
            "role": key,
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for key, (_role, path, _known_sha) in sorted(expected_outputs.items())
        if key != "checksum"
    ]
    checksum_path = next(
        path for key, (_role, path, _known_sha) in expected_outputs.items() if key == "checksum"
    )
    checksum = _read_object(checksum_path, "cached delivery checksum manifest")
    validate_artifact(package_root, "checksum_manifest", checksum)
    if (
        checksum.get("project_id") != layout.root.name
        or checksum.get("revision_id") != expected_static["revision_id"]
        or checksum.get("files") != expected_checksum_files
    ):
        raise StateConflictError("delivery manifest exists with stale contents")
    if seen_paths != set(expected_by_path):
        raise StateConflictError("delivery manifest exists with stale contents")


def _copy_atomic(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.staging")
    shutil.copy2(source, staging)
    os.replace(staging, destination)
    return destination


def _validate_delivery_media(
    adapter: FFmpegAdapter,
    path: Path,
    description: str,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> None:
    if not path.is_file():
        raise PlanningValidationError(f"{description} was not created: {path}")
    decode = adapter.full_decode_check(path)
    if decode.exit_code != 0:
        raise PlanningValidationError(f"{description} failed full decode: {path}")
    probe = adapter.probe(path)
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        raise PlanningValidationError(f"{description} probe has no stream list: {path}")
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
        ),
        None,
    )
    if video is None or audio is None:
        raise PlanningValidationError(f"{description} must contain video and audio: {path}")
    if expected_width is not None and int(video.get("width") or 0) != expected_width:
        raise PlanningValidationError(f"{description} width is not {expected_width}: {path}")
    if expected_height is not None and int(video.get("height") or 0) != expected_height:
        raise PlanningValidationError(f"{description} height is not {expected_height}: {path}")


def _validated_metadata_files(
    layout: ProjectLayout,
    metadata: Mapping[str, Any],
    *,
    revision_id: str,
) -> tuple[dict[str, Path], Path]:
    """Validate publishing metadata identity and every child file hash."""

    if metadata.get("project_id") != layout.root.name or metadata.get("revision_id") != revision_id:
        raise PlanningValidationError("publishing metadata belongs to another project or revision")
    captions = metadata.get("captions")
    if not isinstance(captions, Mapping):
        raise PlanningValidationError("publishing metadata captions are missing")
    caption_files: dict[str, Path] = {}
    for name in ("ass", "webvtt", "text"):
        reference = captions.get(name)
        if not isinstance(reference, Mapping):
            raise PlanningValidationError(f"publishing metadata caption {name} is missing")
        path = _owned_path(layout, Path(str(reference.get("path", ""))), f"caption {name}")
        if not path.is_file() or sha256_file(path) != reference.get("sha256"):
            raise PlanningValidationError(f"publishing metadata caption {name} is stale")
        caption_files[name] = path
    transcript_reference = metadata.get("transcript")
    if not isinstance(transcript_reference, Mapping):
        raise PlanningValidationError("publishing metadata transcript is missing")
    transcript_path = _owned_path(
        layout,
        Path(str(transcript_reference.get("path", ""))),
        "publishing transcript",
    )
    if not transcript_path.is_file() or sha256_file(transcript_path) != transcript_reference.get(
        "sha256"
    ):
        raise PlanningValidationError("publishing metadata transcript is stale")
    return caption_files, transcript_path


def write_publishing_metadata(
    package_root: Path,
    layout: ProjectLayout,
    candidate_path: Path,
    caption_plan_path: Path,
    transcript_path: Path,
    *,
    boundaries_path: Path | None = None,
    description_draft: str | None = None,
    revision_id: str = "rev_001",
) -> Path:
    """Create final caption, transcript, chapter, and description metadata."""

    candidate = _owned_path(layout, candidate_path, "final candidate")
    caption_path = _owned_path(layout, caption_plan_path, "caption plan")
    transcript_path = _owned_path(layout, transcript_path, "final transcript")
    if not candidate.is_file():
        raise PlanningValidationError("final candidate is missing")
    caption = _read_object(caption_path, "caption plan")
    transcript = _read_object(transcript_path, "final transcript")
    validate_artifact(package_root, "caption_plan", caption)
    validate_artifact(package_root, "transcript", transcript)
    if caption["project_id"] != layout.root.name or transcript["project_id"] != layout.root.name:
        raise PlanningValidationError("publishing inputs belong to another project")
    if caption["revision_id"] != revision_id or transcript["revision_id"] != revision_id:
        raise PlanningValidationError("publishing inputs belong to another revision")
    caption_refs: dict[str, dict[str, Any]] = {}
    for name in ("ass", "webvtt", "text"):
        reference = caption["outputs"][name]
        path = _owned_path(layout, Path(str(reference["path"])), f"caption {name}")
        if not path.is_file() or sha256_file(path) != reference["sha256"]:
            raise PlanningValidationError(f"caption {name} sidecar is stale")
        caption_refs[name] = _file_ref(path, f"art_caption_{name}")

    chapters: list[dict[str, Any]] = []
    warnings: list[str] = []
    if boundaries_path is not None:
        selected_boundaries = _owned_path(layout, boundaries_path, "structural boundaries")
        boundaries = _read_object(selected_boundaries, "structural boundaries")
        validate_artifact(package_root, "structural_boundaries", boundaries)
        for boundary in boundaries.get("boundaries", []):
            if not isinstance(boundary, Mapping) or boundary.get("status") != "verified":
                continue
            if boundary.get("purpose") not in {"new_chapter", "new_point"}:
                continue
            title = str(boundary.get("transcript_evidence", "Chapter")).strip()
            chapters.append(
                {
                    "start_us": int(boundary["boundary_us"]),
                    "title": title[:120] or "Chapter",
                    "source": str(boundary.get("boundary_id", "verified_boundary")),
                    "confidence": float(boundary.get("confidence", 0)),
                }
            )
    else:
        warnings.append("chapters_not_provided")
    chapters.sort(key=lambda item: int(item["start_us"]))
    description = (description_draft or str(transcript.get("text", ""))).strip()
    if not description:
        warnings.append("description_draft_empty")
    description = description[:500]
    payload: dict[str, Any] = {
        "schema_name": "publishing_metadata",
        "schema_version": "1.0.0",
        "artifact_id": "art_publishing_metadata",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "candidate_sha256": sha256_file(candidate),
        "captions": caption_refs,
        "transcript": _file_ref(transcript_path, "art_final_transcript"),
        "chapters": chapters,
        "description_draft": description,
        "warnings": warnings,
    }
    digest = make_stage_key(
        "publishing-metadata",
        "p11-06a",
        [sha256_file(candidate), sha256_file(caption_path), sha256_file(transcript_path)]
        + (
            [sha256_file(_owned_path(layout, boundaries_path, "structural boundaries"))]
            if boundaries_path
            else []
        ),
        {"revision_id": revision_id, "description": description},
    )
    output = layout.artifacts / f"publishing-metadata-{digest[:16]}.json"
    alias = layout.artifacts / "publishing-metadata.json"
    with ProjectLock(layout, stage="publishing_metadata", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "publishing metadata")
            validate_artifact(package_root, "publishing_metadata", current)
            return output
        write_validated_artifact(package_root, "publishing_metadata", output, payload)
        write_validated_artifact(package_root, "publishing_metadata", alias, payload)
    return output


def write_checksum_manifest(
    package_root: Path,
    layout: ProjectLayout,
    files: Mapping[str, Path],
    *,
    revision_id: str = "rev_001",
) -> Path:
    """Record SHA-256 and size for every published file."""

    if not files:
        raise PlanningValidationError("checksum manifest requires at least one file")
    refs = [
        {
            "role": role,
            "path": str(_owned_path(layout, path, f"checksum file {role}")),
            "sha256": sha256_file(_owned_path(layout, path, f"checksum file {role}")),
            "size_bytes": _owned_path(layout, path, f"checksum file {role}").stat().st_size,
        }
        for role, path in sorted(files.items())
    ]
    payload: dict[str, Any] = {
        "schema_name": "checksum_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_checksums",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "files": refs,
    }
    output = layout.artifacts / "checksum-manifest.json"
    write_validated_artifact(package_root, "checksum_manifest", output, payload)
    return output


def publish_delivery(
    package_root: Path,
    layout: ProjectLayout,
    gate3_path: Path,
    final_qa_path: Path,
    publishing_metadata_path: Path,
    delivery_profile_path: Path,
    *,
    derivatives: Mapping[str, tuple[int, int]] | None = None,
    adapter: FFmpegAdapter | None = None,
    revision_id: str = "rev_001",
) -> Path:
    """Publish the Gate 3-approved candidate, metadata, derivatives, and checksums."""

    selected_gate3 = _owned_path(layout, gate3_path, "Gate 3 approval")
    selected_qa = _owned_path(layout, final_qa_path, "final QA report")
    selected_metadata = _owned_path(layout, publishing_metadata_path, "publishing metadata")
    selected_profile = _owned_path(layout, delivery_profile_path, "delivery profile")
    gate3 = _read_object(selected_gate3, "Gate 3 approval")
    qa = _read_object(selected_qa, "final QA report")
    metadata = _read_object(selected_metadata, "publishing metadata")
    validate_artifact(package_root, "gate3_approval", gate3)
    validate_artifact(package_root, "final_qa_report", qa)
    validate_artifact(package_root, "publishing_metadata", metadata)
    caption_files, transcript_source = _validated_metadata_files(
        layout, metadata, revision_id=revision_id
    )
    if gate3["project_id"] != layout.root.name or qa["project_id"] != layout.root.name:
        raise PlanningValidationError("delivery evidence belongs to another project")
    if gate3["revision_id"] != revision_id or qa["revision_id"] != revision_id:
        raise PlanningValidationError("delivery evidence belongs to another revision")
    if gate3["decision"] != "approved":
        raise PlanningValidationError("delivery is blocked until Gate 3 is approved")
    if not qa["final_ready"]:
        raise PlanningValidationError("delivery is blocked because final QA is not final-ready")
    if not selected_profile.is_file():
        raise PlanningValidationError("delivery profile is missing")
    bound = gate3["bound_hashes"]
    if bound["final_qa_sha256"] != sha256_file(selected_qa):
        raise PlanningValidationError("Gate 3 final QA binding is stale")
    candidate = _owned_path(layout, Path(str(qa["candidate"]["path"])), "final candidate")
    if not candidate.is_file() or sha256_file(candidate) != bound["candidate_sha256"]:
        raise PlanningValidationError("Gate 3 candidate binding is stale")
    if metadata["candidate_sha256"] != bound["candidate_sha256"]:
        raise PlanningValidationError("publishing metadata is bound to another candidate")
    if bound["delivery_profile_sha256"] != sha256_file(selected_profile):
        raise PlanningValidationError("delivery profile binding is stale")

    selected_adapter = adapter or FFmpegAdapter()
    encoder_identity = adapter_encoder_identity(selected_adapter)
    stage_key = make_stage_key(
        "publish-delivery",
        PUBLISH_DELIVERY_IMPLEMENTATION_VERSION,
        [
            sha256_file(selected_gate3),
            sha256_file(selected_qa),
            sha256_file(selected_metadata),
            sha256_file(selected_profile),
        ],
        {
            "revision_id": revision_id,
            "config_sha256": config_sha256(layout),
            "derivatives": dict(derivatives or {}),
            "encoder": encoder_identity,
        },
    )
    # Keep each hash-bound profile in its own immutable delivery directory so a
    # later encoder choice cannot invalidate an earlier manifest's file refs.
    delivery_dir = layout.output / "delivery" / revision_id / stage_key[:16]
    manifest_path = layout.artifacts / f"delivery-manifest-{stage_key[:16]}.json"
    master_path = delivery_dir / "master.mp4"
    expected_outputs: dict[str, tuple[str, Path, str | None]] = {
        "master": (
            "master",
            master_path,
            sha256_file(candidate),
        )
    }
    for name in sorted(derivatives or {}):
        expected_outputs[f"derivative:{name}"] = (
            "derivative",
            delivery_dir / f"{name}.mp4",
            None,
        )
    for name, source in caption_files.items():
        expected_outputs[f"captions_{name}"] = (
            {
                "ass": "captions_ass",
                "webvtt": "captions_webvtt",
                "text": "transcript",
            }[name],
            delivery_dir / source.name,
            sha256_file(source),
        )
    expected_outputs["transcript"] = (
        "transcript",
        delivery_dir / "transcript.json",
        sha256_file(transcript_source),
    )
    expected_outputs["metadata"] = (
        "metadata",
        delivery_dir / "publishing-metadata.json",
        sha256_file(selected_metadata),
    )
    expected_outputs["provenance"] = (
        "provenance",
        delivery_dir / "provenance.json",
        None,
    )
    expected_outputs["checksum"] = (
        "checksum",
        delivery_dir / "checksums.json",
        None,
    )
    expected_inputs = [
        artifact_input("art_gate3_approval", selected_gate3),
        artifact_input("art_final_qa", selected_qa),
        artifact_input("art_publishing_metadata", selected_metadata),
    ]
    expected_static: dict[str, Any] = {
        "schema_name": "delivery_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_delivery",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "producer": producer("delivery", "local-filesystem", __version__),
        "inputs": expected_inputs,
        "config_sha256": config_sha256(layout),
        "final_approval_id": str(gate3["artifact_id"]),
        "qa_report_id": str(qa["artifact_id"]),
        "profile_id": selected_profile.stem,
        "source_sha256": str(qa["source_sha256"]),
        "reproducible": True,
        "missing_reproducibility_items": [],
    }
    expected_provenance = {
        "gate3_approval_sha256": sha256_file(selected_gate3),
        "final_qa_sha256": sha256_file(selected_qa),
        "publishing_metadata_sha256": sha256_file(selected_metadata),
        "delivery_profile_sha256": sha256_file(selected_profile),
        "candidate_sha256": sha256_file(candidate),
    }
    with ProjectLock(layout, stage="publish_delivery", revision_id=revision_id):
        if manifest_path.is_file():
            current = _read_object(manifest_path, "delivery manifest")
            validate_artifact(package_root, "delivery_manifest", current)
            _validate_cached_delivery_manifest(
                package_root,
                layout,
                current,
                expected_static=expected_static,
                expected_outputs=expected_outputs,
                expected_provenance=expected_provenance,
            )
            _validate_delivery_media(
                selected_adapter,
                master_path,
                "cached delivery master",
            )
            for name, dimensions in sorted((derivatives or {}).items()):
                _validate_delivery_media(
                    selected_adapter,
                    delivery_dir / f"{name}.mp4",
                    f"cached delivery derivative {name}",
                    expected_width=int(dimensions[0]),
                    expected_height=int(dimensions[1]),
                )
            return manifest_path
        delivery_dir.mkdir(parents=True, exist_ok=True)
        master = _copy_atomic(candidate, master_path)
        _validate_delivery_media(selected_adapter, master, "delivery master")
        output_files: dict[str, Path] = {"master": master}
        for name, dimensions in sorted((derivatives or {}).items()):
            if len(dimensions) != 2:
                raise PlanningValidationError(f"derivative dimensions are invalid: {name}")
            derivative_path = delivery_dir / f"{name}.mp4"
            selected_adapter.render_scaled_derivative(
                candidate,
                derivative_path,
                width=int(dimensions[0]),
                height=int(dimensions[1]),
            )
            _validate_delivery_media(
                selected_adapter,
                derivative_path,
                f"delivery derivative {name}",
                expected_width=int(dimensions[0]),
                expected_height=int(dimensions[1]),
            )
            output_files[f"derivative:{name}"] = derivative_path
        captions = metadata["captions"]
        for name in captions:
            source = caption_files[str(name)]
            output_files[f"captions_{name}"] = _copy_atomic(
                source, delivery_dir / Path(str(source.name))
            )
        output_files["transcript"] = _copy_atomic(
            transcript_source, delivery_dir / "transcript.json"
        )
        metadata_copy = _copy_atomic(selected_metadata, delivery_dir / "publishing-metadata.json")
        output_files["metadata"] = metadata_copy
        provenance = expected_outputs["provenance"][1]
        write_text_atomically(
            provenance,
            json.dumps(
                expected_provenance,
                indent=2,
            )
            + "\n",
        )
        output_files["provenance"] = provenance
        checksum_path = write_checksum_manifest(
            package_root, layout, output_files, revision_id=revision_id
        )
        checksum_copy = _copy_atomic(checksum_path, expected_outputs["checksum"][1])
        output_files["checksum"] = checksum_copy
        outputs: list[dict[str, Any]] = []
        for role, path in output_files.items():
            if role.startswith("derivative:"):
                output_role = "derivative"
            elif role.startswith("captions_"):
                output_role = {
                    "captions_ass": "captions_ass",
                    "captions_webvtt": "captions_webvtt",
                    "captions_text": "transcript",
                }.get(role, "transcript")
            elif role == "checksum":
                output_role = "checksum"
            elif role == "metadata":
                output_role = "metadata"
            else:
                output_role = role
            outputs.append(
                {
                    "role": output_role,
                    "file": {
                        "path": str(path.resolve()),
                        "sha256": sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    },
                }
            )
        payload: dict[str, Any] = {
            "schema_name": expected_static["schema_name"],
            "schema_version": expected_static["schema_version"],
            "artifact_id": expected_static["artifact_id"],
            "project_id": expected_static["project_id"],
            "revision_id": expected_static["revision_id"],
            "created_at": now_iso(),
            "producer": expected_static["producer"],
            "inputs": expected_static["inputs"],
            "config_sha256": expected_static["config_sha256"],
            "final_approval_id": expected_static["final_approval_id"],
            "qa_report_id": expected_static["qa_report_id"],
            "profile_id": expected_static["profile_id"],
            "outputs": outputs,
            "source_sha256": expected_static["source_sha256"],
            "reproducible": expected_static["reproducible"],
            "missing_reproducibility_items": expected_static["missing_reproducibility_items"],
        }
        write_validated_artifact(package_root, "delivery_manifest", manifest_path, payload)
        write_validated_artifact(
            package_root, "delivery_manifest", layout.artifacts / "delivery-manifest.json", payload
        )
    return manifest_path


__all__ = ["publish_delivery", "write_checksum_manifest", "write_publishing_metadata"]
