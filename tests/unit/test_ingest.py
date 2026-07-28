from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from videoedit.errors import (
    DiskSpaceError,
    MediaValidationError,
    SourceIntegrityError,
    StateConflictError,
)
from videoedit.services.media import ingest_and_probe, normalize_probe, preflight_disk_space
from videoedit.services.project import ProjectLock, ingest_source, initialize_project, sha256_file
from videoedit.services.stage_state import load_stage_state


class MinimalAdapter:
    ffprobe_path = "ffprobe"

    def __init__(self, *, streams: list[dict[str, object]] | None = None) -> None:
        self.streams = (
            streams
            if streams is not None
            else [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "time_base": "1/30",
                    "start_time": "0",
                    "duration": "1",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "30/1",
                    "r_frame_rate": "30/1",
                }
            ]
        )

    def probe(self, _path: Path) -> dict[str, object]:
        return {
            "format": {"format_name": "fixture", "duration": "1", "bit_rate": "1000"},
            "streams": self.streams,
        }

    def version(self, _executable: str | None = None) -> str:
        return "fixture"


def test_project_lock_rejects_active_owner_and_recovers_stale_owner(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "lock_project")
    with ProjectLock(layout, stage="outer"):
        with pytest.raises(StateConflictError):
            with ProjectLock(layout, stage="inner"):
                pass

    stale_payload = {
        "schema_name": "project_lock",
        "schema_version": "1.0.0",
        "lock_id": "stale-lock",
        "owner_id": "dead-owner",
        "pid": 2_000_000,
        "hostname": socket.gethostname(),
        "stage": "interrupted",
        "revision_id": "rev_001",
        "created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "heartbeat_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }
    layout.lock_path.write_text(json.dumps(stale_payload), encoding="utf-8")
    with ProjectLock(layout, stage="recovery", stale_after_seconds=1):
        assert layout.lock_path.is_file()
    assert not layout.lock_path.exists()
    assert list(layout.state.glob("project.lock.stale.*"))


def test_duplicate_source_reuses_immutable_copy_and_stage_cache(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "duplicate_project")
    first_source = tmp_path / "first.bin"
    second_source = tmp_path / "renamed.bin"
    first_source.write_bytes(b"same source bytes")
    second_source.write_bytes(first_source.read_bytes())
    adapter = MinimalAdapter()

    first = ingest_source(layout, first_source, adapter=adapter)
    second = ingest_source(layout, second_source, adapter=adapter)

    managed_files = [path for path in layout.raw.iterdir() if path.is_file()]
    assert len(managed_files) == 1
    assert first["managed_path"] == second["managed_path"]
    assert sha256_file(managed_files[0]) == first["sha256"]
    state = load_stage_state(Path(__file__).resolve().parents[2], layout, "ingest", "rev_001")
    assert state is not None
    assert state["status"] == "complete"
    assert state["attempt"] == 1


def test_reference_policy_registers_without_copying_source(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "reference_project")
    source = tmp_path / "reference.bin"
    source.write_bytes(b"registered source bytes")

    manifest = ingest_and_probe(
        package_root,
        layout,
        source,
        copy_source=False,
        adapter=MinimalAdapter(),
    )

    assert manifest["ingest_mode"] == "reference"
    assert manifest["managed_path"] is None
    assert not list(layout.raw.iterdir())


def test_mutated_managed_source_is_rejected(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "mutation_project")
    source = tmp_path / "source.bin"
    source.write_bytes(b"immutable bytes")
    ingest_source(layout, source, adapter=MinimalAdapter())
    managed = next(layout.raw.iterdir())
    os.chmod(managed, managed.stat().st_mode | 0o200)
    managed.write_bytes(b"tampered bytes")

    with pytest.raises(SourceIntegrityError):
        ingest_source(layout, source, adapter=MinimalAdapter())


def test_invalid_probe_persists_failed_stage_state(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "invalid_project")
    source = tmp_path / "source.bin"
    source.write_bytes(b"not a video")

    with pytest.raises(MediaValidationError, match="no video stream"):
        ingest_source(layout, source, adapter=MinimalAdapter(streams=[]))

    state = load_stage_state(Path(__file__).resolve().parents[2], layout, "ingest", "rev_001")
    assert state is not None
    assert state["status"] == "failed"
    assert state["error"]["code"] == "missing_video_stream"
    assert list(layout.staging.iterdir())


def test_probe_reports_rotation_vfr_and_missing_audio() -> None:
    payload = normalize_probe(
        {
            "format": {"format_name": "fixture", "duration": "2"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mystery",
                    "time_base": "1/30",
                    "duration": "2",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24/1",
                    "r_frame_rate": "30/1",
                    "tags": {"rotate": "90"},
                    "sample_aspect_ratio": "4:3",
                }
            ],
        },
        project_id="probe_project",
        revision_id="rev_001",
        source_sha256="a" * 64,
        config_hash="b" * 64,
        ffprobe_version="fixture",
    )

    assert payload["status"] == "warning"
    assert "variable_frame_rate" in payload["warnings"]
    assert "rotation_metadata:90" in payload["warnings"]
    assert "missing_audio_stream" in payload["warnings"]
    assert "unsupported_video_codec:mystery" in payload["warnings"]
    video = payload["streams"][0]
    assert video["rotation_degrees"] == 90
    assert video["pixel_aspect_ratio"] == "4:3"
    assert video["variable_frame_rate"] is True


def test_disk_preflight_is_typed_and_injectable(tmp_path: Path) -> None:
    class Usage:
        free = 4

    with pytest.raises(DiskSpaceError, match="insufficient disk space"):
        preflight_disk_space(tmp_path, 5, disk_usage_fn=lambda _path: Usage())
