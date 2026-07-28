from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.render_manifest_bridge import (
    write_revision_render_manifest,
    write_revision_retimed_render_manifest,
)

ROOT = Path(__file__).resolve().parents[2]


def _render_manifest(project_id: str, output: Path, revision_id: str = "rev_001") -> dict:
    return {
        "schema_name": "render_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_render_parent",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": "2026-07-27T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "inputs": [
            {"artifact_id": "art_source", "sha256": "a" * 64},
        ],
        "config_sha256": "b" * 64,
        "render_type": "final",
        "composition_artifact_id": "art_timeline",
        "expected_duration_us": 5_000_000,
        "actual_duration_us": 5_000_000,
        "output": {
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
            "size_bytes": output.stat().st_size,
        },
        "video": {
            "codec": "h264",
            "width": 1280,
            "height": 720,
            "frame_rate": {"numerator": 30, "denominator": 1},
            "pixel_format": "yuv420p",
        },
        "audio": {"codec": "pcm_f32le", "sample_rate_hz": 48000, "channels": 2},
        "commands": [
            {
                "executable": "ffmpeg",
                "arguments": ["ffmpeg", "-i", "source.mp4"],
                "working_directory": str(output.parent),
                "exit_code": 0,
                "elapsed_ms": 1,
                "version": "fixture",
            }
        ],
        "warnings": [],
    }


def _revision_media(project_id: str, output: Path) -> dict:
    return {
        "schema_name": "revision_media_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_revision_media_rev_002",
        "project_id": project_id,
        "revision_id": "rev_002",
        "parent_revision_id": "rev_001",
        "created_at": "2026-07-27T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "source_markers": {
            "artifact_id": "art_markers",
            "path": "markers.json",
            "sha256": "c" * 64,
        },
        "source": {"artifact_id": "art_source", "path": "source.mp4", "sha256": "d" * 64},
        "output": {
            "artifact_id": "art_output",
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
        },
        "source_duration_us": 6_000_000,
        "output_duration_us": 5_000_000,
        "removed_ranges": [{"start_us": 2_000_000, "end_us": 3_000_000}],
        "keep_ranges": [
            {"start_us": 0, "end_us": 2_000_000},
            {"start_us": 3_000_000, "end_us": 6_000_000},
        ],
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": 2_000_000,
                "output_start_us": 0,
                "output_end_us": 2_000_000,
            },
            {
                "source_start_us": 3_000_000,
                "source_end_us": 6_000_000,
                "output_start_us": 2_000_000,
                "output_end_us": 5_000_000,
            },
        ],
        "warnings": [],
        "status": "complete",
    }


def _retimed_render_manifest(project_id: str, output: Path) -> dict:
    return {
        "schema_name": "retimed_render_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_retimed_render",
        "project_id": project_id,
        "revision_id": "rev_001",
        "created_at": "2026-07-27T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "inputs": [{"artifact_id": "art_source", "sha256": "a" * 64}],
        "config_sha256": "b" * 64,
        "expected_duration_us": 6_000_000,
        "output": {
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
            "size_bytes": output.stat().st_size,
        },
        "video": {
            "width": 2560,
            "height": 1440,
            "frame_rate": {"numerator": 60, "denominator": 1},
            "duration_us": 6_000_000,
        },
        "audio": {"duration_us": 6_000_000, "sample_rate_hz": 48000, "channels": 2},
        "validation": {"full_decode": "pass", "duration": "pass", "av_sync": "pass"},
        "command": {"executable": "ffmpeg", "arguments": ["ffmpeg", "-qp", "0"]},
    }


def test_revision_render_manifest_bridge_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    package_root = ROOT
    layout = initialize_project(tmp_path, "bridge_test")
    output = layout.output / "candidate.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"candidate")
    base_path = layout.artifacts / "base-render.json"
    media_path = layout.artifacts / "revision-media.json"
    write_validated_artifact(
        package_root,
        "render_manifest",
        base_path,
        _render_manifest(layout.root.name, output),
    )
    write_validated_artifact(
        package_root,
        "revision_media_manifest",
        media_path,
        _revision_media(layout.root.name, output),
    )

    result = write_revision_render_manifest(
        package_root, layout, base_path, media_path, revision_id="rev_002"
    )
    second = write_revision_render_manifest(
        package_root, layout, base_path, media_path, revision_id="rev_002"
    )
    assert result == second
    payload = json.loads(result.read_text(encoding="utf-8"))
    validate_artifact(package_root, "render_manifest", payload)
    assert payload["revision_id"] == "rev_002"
    assert payload["expected_duration_us"] == 5_000_000
    assert payload["output"]["sha256"] == sha256_file(output)
    assert (
        payload["source_to_output_mapping"]
        == _revision_media(layout.root.name, output)["source_to_output_mapping"]
    )


def test_revision_render_manifest_bridge_accepts_lossless_recut_path_and_duration(
    tmp_path: Path,
) -> None:
    package_root = ROOT
    layout = initialize_project(tmp_path, "bridge_recut_test")
    parent_output = layout.output / "parent.mp4"
    revision_output = layout.revisions / "rev_002" / "outputs" / "recut.mp4"
    parent_output.parent.mkdir(parents=True, exist_ok=True)
    revision_output.parent.mkdir(parents=True, exist_ok=True)
    parent_output.write_bytes(b"parent")
    revision_output.write_bytes(b"revision")

    base = _render_manifest(layout.root.name, parent_output)
    base["expected_duration_us"] = 6_000_000
    base["actual_duration_us"] = 6_000_000
    base_path = layout.artifacts / "base-render.json"
    media_path = layout.artifacts / "revision-media.json"
    write_validated_artifact(package_root, "render_manifest", base_path, base)
    write_validated_artifact(
        package_root,
        "revision_media_manifest",
        media_path,
        _revision_media(layout.root.name, revision_output),
    )

    result = write_revision_render_manifest(
        package_root, layout, base_path, media_path, revision_id="rev_002"
    )
    payload = json.loads(result.read_text(encoding="utf-8"))
    validate_artifact(package_root, "render_manifest", payload)
    assert payload["expected_duration_us"] == 5_000_000
    assert payload["actual_duration_us"] == 5_000_000
    assert payload["output"]["path"] == str(revision_output.resolve())
    assert "revision_output_path_changed" in payload["warnings"]
    assert "revision_duration_changed" in payload["warnings"]


def test_revision_retimed_manifest_bridge_updates_recut_output_reference(tmp_path: Path) -> None:
    package_root = ROOT
    layout = initialize_project(tmp_path, "retimed_bridge_recut_test")
    parent_output = layout.output / "parent.mp4"
    revision_output = layout.revisions / "rev_002" / "outputs" / "recut.mp4"
    parent_output.parent.mkdir(parents=True, exist_ok=True)
    revision_output.parent.mkdir(parents=True, exist_ok=True)
    parent_output.write_bytes(b"parent")
    revision_output.write_bytes(b"revision")
    base_path = layout.artifacts / "base-retimed-render.json"
    media_path = layout.artifacts / "revision-media.json"
    write_validated_artifact(
        package_root,
        "retimed_render_manifest",
        base_path,
        _retimed_render_manifest(layout.root.name, parent_output),
    )
    write_validated_artifact(
        package_root,
        "revision_media_manifest",
        media_path,
        _revision_media(layout.root.name, revision_output),
    )

    result = write_revision_retimed_render_manifest(
        package_root, layout, base_path, media_path, revision_id="rev_002"
    )
    payload = json.loads(result.read_text(encoding="utf-8"))
    validate_artifact(package_root, "retimed_render_manifest", payload)
    assert payload["revision_id"] == "rev_002"
    assert payload["expected_duration_us"] == 5_000_000
    assert payload["output"]["path"] == str(revision_output.resolve())
