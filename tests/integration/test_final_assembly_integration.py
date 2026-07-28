from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.final_assembly import assemble_approved_segments
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


def _write_locked_segment(
    layout: ProjectLayout,
    *,
    media_path: Path,
    segment_id: str,
    source_start_us: int,
    duration_us: int,
) -> dict[str, object]:
    revision_root = layout.revision_root("rev_001")
    source_path = revision_root / f"{segment_id}-source.marker"
    marker_path = revision_root / f"{segment_id}-markers.json"
    source_path.write_bytes(f"source marker for {segment_id}".encode())
    marker_path.write_text("{}\n", encoding="utf-8")

    media_manifest = json.loads(
        (ROOT / "examples" / "revision_media_manifest.example.json").read_text(encoding="utf-8")
    )
    media_manifest.update(
        {
            "artifact_id": f"art_{segment_id}_media_manifest",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "parent_revision_id": "rev_000",
            "source_markers": {
                "artifact_id": f"art_{segment_id}_markers",
                "path": str(marker_path.resolve()),
                "sha256": sha256_file(marker_path),
            },
            "source": {
                "artifact_id": f"art_{segment_id}_source",
                "path": str(source_path.resolve()),
                "sha256": sha256_file(source_path),
            },
            "output": {
                "artifact_id": f"art_{segment_id}_media",
                "path": str(media_path.resolve()),
                "sha256": sha256_file(media_path),
            },
            "source_duration_us": duration_us,
            "output_duration_us": duration_us,
            "removed_ranges": [],
            "keep_ranges": [{"start_us": 0, "end_us": duration_us}],
            "source_to_output_mapping": [
                {
                    "source_start_us": source_start_us,
                    "source_end_us": source_start_us + duration_us,
                    "output_start_us": 0,
                    "output_end_us": duration_us,
                }
            ],
        }
    )
    media_manifest_path = revision_root / f"{segment_id}-media-manifest.json"
    write_validated_artifact(ROOT, "revision_media_manifest", media_manifest_path, media_manifest)

    lock = json.loads((ROOT / "examples" / "segment_lock.example.json").read_text(encoding="utf-8"))
    lock.update(
        {
            "artifact_id": f"art_{segment_id}_lock",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "segment_id": segment_id,
        }
    )
    lock_path = layout.review / "gate2" / f"{segment_id}-lock.json"
    write_validated_artifact(ROOT, "segment_lock", lock_path, lock)
    return {
        "segment_id": segment_id,
        "lock_path": str(lock_path),
        "media_manifest_path": str(media_manifest_path),
        "source_range": {
            "start_us": source_start_us,
            "end_us": source_start_us + duration_us,
        },
    }


@pytest.mark.integration
def test_final_assembly_runs_typed_ffmpeg_and_retains_visual_evidence(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "final_assembly_media")
    adapter = FFmpegAdapter()
    segment_paths = [
        layout.revision_root("rev_001") / "segment-000001.mp4",
        layout.revision_root("rev_001") / "segment-000002.mp4",
    ]
    for path in segment_paths:
        adapter.generate_edit_demo_source(path)
    duration_us = round(float(adapter.probe(segment_paths[0])["format"]["duration"]) * 1_000_000)
    specs = [
        _write_locked_segment(
            layout,
            media_path=segment_paths[index],
            segment_id=f"segment_{index + 1:06d}",
            source_start_us=index * duration_us,
            duration_us=duration_us,
        )
        for index in range(2)
    ]

    manifest_path = assemble_approved_segments(ROOT, layout, specs, adapter=adapter)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "final_assembly_manifest", manifest)
    candidate = Path(str(manifest["output"]["path"]))
    assert manifest["status"] == "complete"
    assert manifest["loudness"]["status"] == "pass"
    assert abs(int(manifest["actual_duration_us"]) - 2 * duration_us) <= 50_000
    assert adapter.full_decode_check(candidate).exit_code == 0
    assert adapter.probe_frame_count(candidate) == 360
    assert [item["output_range"] for item in manifest["segments"]] == [
        {"start_us": 0, "end_us": duration_us},
        {"start_us": duration_us, "end_us": 2 * duration_us},
    ]
    contact_sheet = layout.review / "final-assembly-contact-sheet.png"
    adapter.make_contact_sheet(candidate, contact_sheet, [0, 180, 359], tile_columns=3)
    assert contact_sheet.is_file()
    assert assemble_approved_segments(ROOT, layout, specs, adapter=adapter) == manifest_path
