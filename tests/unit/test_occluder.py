from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import StaleApprovalError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.occluder import append_occluder_video_layer
from videoedit.services.project import sha256_file


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ref(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    source = tmp_path / "source.mp4"
    result = tmp_path / "segmentation-result.json"
    validation = tmp_path / "segmentation-validation.json"
    review = tmp_path / "object-track-review.json"
    mask = tmp_path / "masks" / "000000.png"
    staged_mask = tmp_path / "staging" / "000000.png"
    output_media = tmp_path / "occluder.mov"
    for path, content in (
        (source, b"source"),
        (result, b"result"),
        (validation, b"validation"),
        (review, b"review"),
        (mask, b"mask"),
        (staged_mask, b"mask"),
        (output_media, b"occluder"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    manifest = tmp_path / "occluder-manifest.json"
    payload = {
        "schema_name": "occluder_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_object_occluder_1",
        "project_id": "occluder_test",
        "revision_id": "rev_001",
        "created_at": "2026-07-24T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "object-occluder",
            "adapter": "ffmpeg",
            "adapter_version": "fixture",
        },
        "inputs": [
            {"artifact_id": "art_source", "sha256": sha256_file(source)},
            {"artifact_id": "art_segmentation_result", "sha256": sha256_file(result)},
            {"artifact_id": "art_segmentation_validation", "sha256": sha256_file(validation)},
            {"artifact_id": "art_object_track_review", "sha256": sha256_file(review)},
        ],
        "status": "complete",
        "source": _ref(source),
        "source_range": {"start_frame": 0, "end_frame": 1},
        "track": {
            "source_result": _ref(result),
            "segmentation_validation": _ref(validation),
            "review": _ref(review),
            "object_id": 1,
            "decision": "approved",
            "findings": {
                "identity": "pass",
                "continuity": "pass",
                "geometry": "pass",
                "occlusion": "pass",
            },
        },
        "mask": {
            "pattern": str((tmp_path / "staging" / "%06d.png").resolve()),
            "encoding": "png_gray8",
            "polarity": "white_foreground",
            "frames": [
                {
                    "frame_index": 0,
                    "visible": True,
                    "source": _ref(mask),
                    "staged": _ref(staged_mask),
                }
            ],
        },
        "output": _ref(output_media),
        "video": {
            "codec": "prores",
            "width": 640,
            "height": 360,
            "frame_rate": {"numerator": 30, "denominator": 1},
            "pixel_format": "yuva444p10le",
            "frame_count": 1,
            "duration_us": 33333,
        },
        "alpha": {
            "min": 0,
            "max": 255,
            "mean": 5,
            "polarity": "mixed",
            "sampled_frames": 1,
        },
        "validation": {
            "source_identity": "pass",
            "source_range": "pass",
            "track_review": "pass",
            "mask_sequence": "pass",
            "full_decode": "pass",
            "alpha_plane": "pass",
            "alpha_range": "pass",
            "alpha_polarity": "pass",
            "dimensions": "pass",
            "frame_count": "pass",
            "frame_rate": "pass",
            "duration": "pass",
        },
        "layer": {
            "layer_id": "tracked-occluder",
            "asset_id": "asset_tracked-occluder",
            "role": "front",
            "z_index": 40,
            "start_frame": 0,
            "duration_frames": 1,
        },
        "fallback": {"mode": "original_shot", "on_uncertain": "keep_original"},
        "commands": [],
        "warnings": [],
    }
    validate_artifact(package_root(), "occluder_manifest", payload)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "project_id": "occluder_test",
                "width": 640,
                "height": 360,
                "fps": 30,
                "duration_frames": 10,
                "background": {"kind": "solid", "value": "#000000"},
                "layers": [],
                "audio": [],
                "captions": [],
            }
        ),
        encoding="utf-8",
    )
    return timeline, manifest, output_media, result, validation


def test_append_occluder_adds_hash_bound_transparent_front_layer(tmp_path: Path) -> None:
    timeline, manifest, output_media, _result, _validation = _write_fixture(tmp_path)
    output = tmp_path / "timeline-with-occluder.json"

    append_occluder_video_layer(
        package_root(),
        timeline,
        manifest,
        "generated/occluder_test/occluder.mov",
        output,
        asset_sha256=sha256_file(output_media),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    layer = payload["layers"][0]
    assert layer["role"] == "front"
    assert layer["transparent"] is True
    assert layer["muted"] is True
    assert layer["source_from_frame"] == 0
    assert payload["assets"][0]["role"] == "front"


def test_append_occluder_rejects_stale_output_media(tmp_path: Path) -> None:
    timeline, manifest, output_media, _result, _validation = _write_fixture(tmp_path)
    output_media.write_bytes(b"changed")

    with pytest.raises(StaleApprovalError, match="occluder output"):
        append_occluder_video_layer(
            package_root(),
            timeline,
            manifest,
            "generated/occluder_test/occluder.mov",
            tmp_path / "timeline-with-occluder.json",
        )
