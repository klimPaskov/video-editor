from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from videoedit.adapters.process import LocalProcessRunner, ProcessRequest
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.segmentation import (
    SegmentationPrompt,
    build_segmentation_job,
    validate_segmentation_job,
    validate_segmentation_result,
)
from videoedit.services.worker_runtime import approve_worker_runtime


class FakeSegmentationProbe:
    def probe(self, _path: Path) -> dict[str, object]:
        return {
            "format": {"duration": "1.000000"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "30/1",
                }
            ],
        }

    def probe_frame_count(self, _path: Path) -> int:
        return 30


def test_point_prompt_requires_aligned_labels() -> None:
    with pytest.raises(ValueError, match="labels must align"):
        SegmentationPrompt(
            type="point",
            frame_index=0,
            points=[(0.5, 0.5)],
            point_labels=[],
        )


def test_build_and_validate_blocked_sam_job_without_checkpoint(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "seg_test")
    source = layout.raw / "source.mp4"
    source.write_bytes(b"immutable fixture")
    payload = build_segmentation_job(
        package_root,
        layout,
        source,
        job_id="sam-job-001",
        prompt={
            "type": "point",
            "frame_index": 2,
            "points": [(0.5, 0.5)],
            "point_labels": [1],
            "object_id": 1,
        },
        start_frame=0,
        end_frame=30,
        approval={"artifact_id": "art_effect_plan", "sha256": "0" * 64},
        upstream_commit="46957e47805eaa273f4aa7bbbd25a88bca9108ce",
        checkpoint_id="facebook/sam3.1/sam3.1_multiplex.pt",
        adapter=FakeSegmentationProbe(),
    )

    assert payload["schema_version"] == "1.1"
    assert payload["runtime"]["access"] == "blocked"
    assert payload["runtime"]["checkpoint_path"] is None
    validated = validate_segmentation_job(package_root, payload)
    assert validated is not None
    assert validated.source_range.start_frame == 0

    job_path = tmp_path / "sam-job.json"
    write_validated_artifact(package_root, "segmentation_job", job_path, payload)
    worker = package_root / "workers" / "sam3" / "run_job.py"
    result = LocalProcessRunner().run(
        ProcessRequest(
            executable=sys.executable,
            arguments=(str(worker), str(job_path), "--dry-run"),
            working_directory=package_root,
            timeout_seconds=30,
        )
    )
    assert result.exit_code != 0
    assert "runtime gate" in result.stderr


def test_build_sam_job_binds_explicit_runtime_approval(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "seg_runtime_approval")
    source = layout.raw / "source.mp4"
    checkpoint = tmp_path / "sam3.1-multiplex.pt"
    source.write_bytes(b"immutable fixture")
    checkpoint.write_bytes(b"operator checkpoint fixture")
    upstream_commit = "46957e47805eaa273f4aa7bbbd25a88bca9108ce"
    checkpoint_hash = sha256_file(checkpoint)
    runtime_approval = approve_worker_runtime(
        package_root,
        layout,
        worker="sam3",
        upstream_commit=upstream_commit,
        checkpoint_id="facebook/sam3.1/sam3.1_multiplex.pt",
        checkpoint_sha256=checkpoint_hash,
        pytorch="2.7.0+cu126",
        cuda="12.6",
        device="cuda:0",
        actor="operator@example.test",
        role="licence-owner",
        reason="Fixture acceptance only",
    )
    payload = build_segmentation_job(
        package_root,
        layout,
        source,
        job_id="sam-job-approved-001",
        prompt={
            "type": "point",
            "frame_index": 2,
            "points": [(0.5, 0.5)],
            "point_labels": [1],
            "object_id": 1,
        },
        start_frame=0,
        end_frame=30,
        approval={"artifact_id": "art_effect_plan", "sha256": "0" * 64},
        upstream_commit=upstream_commit,
        checkpoint_id="facebook/sam3.1/sam3.1_multiplex.pt",
        checkpoint_sha256=checkpoint_hash,
        checkpoint_path=checkpoint,
        pytorch="2.7.0+cu126",
        cuda="12.6",
        device="cuda:0",
        runtime_approval_path=runtime_approval,
        adapter=FakeSegmentationProbe(),
    )

    assert payload["runtime"]["access"] == "approved"
    assert payload["runtime"]["runtime_approval"]["sha256"] == sha256_file(runtime_approval)
    validated = validate_segmentation_job(package_root, payload)
    assert validated is not None
    assert validated.runtime.runtime_approval is not None


def test_legacy_worker_dry_run_is_contract_safe() -> None:
    package_root = Path(__file__).resolve().parents[2]
    worker = package_root / "workers" / "sam3" / "run_job.py"
    example = package_root / "examples" / "segmentation_job.example.json"
    result = LocalProcessRunner().run(
        ProcessRequest(
            executable=sys.executable,
            arguments=(str(worker), str(example), "--dry-run"),
            working_directory=package_root,
            timeout_seconds=30,
        )
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "valid", "job_id": "segment-ball-v1"}


def _result_payload(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    result_path = tmp_path / "segmentation-result.json"
    frames = []
    for frame_index, object_id, area, centroid in (
        (0, 1, 100, [5.0, 5.0]),
        (1, 2, 100, [5.0, 5.0]),
        (2, 2, 1000, [200.0, 5.0]),
    ):
        frames.append(
            {
                "frame_index": frame_index,
                "combined_mask_path": str(tmp_path / "masks" / f"{frame_index:06d}.png"),
                "objects": [
                    {
                        "object_id": object_id,
                        "mask_path": str(
                            tmp_path / "instances" / f"{frame_index:06d}-object-{object_id}.png"
                        ),
                        "visible": True,
                        "area_pixels": area,
                        "bbox_xywh": [0, 0, 10, 10],
                        "centroid_xy": centroid,
                        "area_ratio": 0.01,
                    }
                ],
            }
        )
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "job_id": "sam-job-001",
        "status": "complete",
        "worker": "sam3",
        "project_id": "seg_test",
        "revision_id": "rev_001",
        "input_path": str(tmp_path / "source.mp4"),
        "input_sha256": "0" * 64,
        "input": {
            "path": str(tmp_path / "source.mp4"),
            "sha256": "0" * 64,
            "size_bytes": 1,
        },
        "source_range": {"start_frame": 0, "end_frame": 3},
        "input_video": {
            "width": 320,
            "height": 180,
            "frame_count": 3,
            "frame_rate": "30/1",
        },
        "prompt": {"type": "point", "frame_index": 0},
        "mask_pattern": str(tmp_path / "masks" / "%06d.png"),
        "frame_count": 3,
        "frames": frames,
        "software": {"worker": "fixture"},
        "output": {
            "mask_format": "png_gray8",
            "lossless": True,
            "mask_pattern": str(tmp_path / "masks" / "%06d.png"),
            "mask_count": 3,
        },
        "diagnostics": {},
        "raw_worker_metadata": {},
    }
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return result_path, payload


def test_segmentation_result_reports_identity_and_geometry_warnings(tmp_path: Path) -> None:
    result_path, _ = _result_payload(tmp_path)
    validation = validate_segmentation_result(
        Path(__file__).resolve().parents[2],
        result_path,
        job={
            "input_sha256": "0" * 64,
            "source_range": {"start_frame": 0, "end_frame": 3},
            "expected_object_count": 1,
        },
        verify_files=False,
    )
    assert validation.status == "warning"
    assert not validation.is_valid
    assert validation.diagnostics["identity_warnings"]
    assert validation.diagnostics["area_jump_frames"] == [2]
    assert validation.diagnostics["centroid_jump_frames"] == [2]
    assert validation.review_frame_indices == [0, 1, 2]


def test_segmentation_result_rejects_mask_paths_outside_output_root(tmp_path: Path) -> None:
    result_path, payload = _result_payload(tmp_path)
    frames = payload["frames"]
    assert isinstance(frames, list)
    frame = frames[0]
    assert isinstance(frame, dict)
    frame["combined_mask_path"] = str(tmp_path.parent / "outside.png")
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    validation = validate_segmentation_result(
        Path(__file__).resolve().parents[2],
        result_path,
        job={
            "input_sha256": "0" * 64,
            "source_range": {"start_frame": 0, "end_frame": 3},
            "expected_object_count": 1,
        },
        verify_files=True,
    )
    assert validation.status == "fail"
    assert validation.validation["mask_files"] == "fail"
