from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import ProcessRequest
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.segmentation import (
    validate_segmentation_result,
    write_segmentation_contact_sheets,
    write_segmentation_validation,
)


@pytest.mark.integration
def test_segmentation_masks_are_validated_and_contact_sheets_are_reviewable(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "sam_review")
    source = layout.raw / "source.mp4"
    output_dir = layout.work / "sam3" / "job-001"
    mask_dir = output_dir / "masks"
    adapter = FFmpegAdapter()
    adapter.generate_demo_source(source, duration_seconds=1)
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_dir.mkdir(parents=True, exist_ok=True)
    extracted = adapter.runner.run(
        ProcessRequest(
            executable=adapter.ffmpeg_path,
            arguments=(
                "-y",
                "-i",
                str(source.resolve()),
                "-map",
                "0:v:0",
                "-vf",
                "format=gray",
                "-frames:v",
                "30",
                "-start_number",
                "0",
                str((mask_dir / "%06d.png").resolve()),
            ),
            working_directory=output_dir,
            timeout_seconds=120,
        )
    )
    assert extracted.exit_code == 0, extracted.stderr

    probe = adapter.probe(source)
    video = next(item for item in probe["streams"] if item.get("codec_type") == "video")
    frame_count = adapter.probe_frame_count(source)
    assert frame_count == 30
    input_sha256 = sha256_file(source)
    frames = []
    for index in range(frame_count):
        mask_path = mask_dir / f"{index:06d}.png"
        assert mask_path.is_file()
        frames.append(
            {
                "frame_index": index,
                "combined_mask_path": str(mask_path.resolve()),
                "combined_mask_sha256": sha256_file(mask_path),
                "objects": [
                    {
                        "object_id": 1,
                        "mask_path": str(mask_path.resolve()),
                        "mask_sha256": sha256_file(mask_path),
                        "visible": True,
                        "area_pixels": 1000,
                        "bbox_xywh": [10, 10, 50, 50],
                        "centroid_xy": [35.0, 35.0],
                        "area_ratio": 0.02,
                    }
                ],
            }
        )
    result = {
        "schema_version": "1.1",
        "job_id": "job-001",
        "status": "complete",
        "worker": "sam3",
        "project_id": "sam_review",
        "revision_id": "rev_001",
        "input_path": str(source.resolve()),
        "input_sha256": input_sha256,
        "input": {
            "path": str(source.resolve()),
            "sha256": input_sha256,
            "size_bytes": source.stat().st_size,
        },
        "source_range": {"start_frame": 0, "end_frame": frame_count},
        "input_video": {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "frame_count": frame_count,
            "frame_rate": "30/1",
        },
        "prompt": {"type": "text", "text": "subject", "frame_index": 0},
        "mask_pattern": str((mask_dir / "%06d.png").resolve()),
        "frame_count": frame_count,
        "frames": frames,
        "software": {"worker": "fixture", "upstream_commit": "fixture"},
        "output": {
            "mask_format": "png_gray8",
            "lossless": True,
            "mask_pattern": str((mask_dir / "%06d.png").resolve()),
            "mask_count": frame_count,
        },
        "diagnostics": {},
        "raw_worker_metadata": {},
    }
    result_path = output_dir / "segmentation-result.json"
    write_validated_artifact(package_root, "segmentation_result", result_path, result)

    validation = validate_segmentation_result(
        package_root,
        result_path,
        job={
            "input_sha256": input_sha256,
            "source_range": {"start_frame": 0, "end_frame": frame_count},
            "expected_object_count": 1,
            "output_dir": str(output_dir.resolve()),
        },
        adapter=adapter,
    )
    assert validation.status == "pass"
    assert validation.is_valid
    contact_sheets = write_segmentation_contact_sheets(
        layout,
        source,
        result_path,
        validation,
        adapter=adapter,
    )
    report_path = write_segmentation_validation(
        package_root,
        layout,
        result_path,
        validation,
        source_path=source,
        source_range={"start_frame": 0, "end_frame": frame_count},
        contact_sheets=contact_sheets,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "pass"
    alias = json.loads(
        (layout.artifacts / "segmentation-validation.json").read_text(encoding="utf-8")
    )
    assert alias["result"]["sha256"] == sha256_file(result_path)
    assert {Path(item["path"]).name for item in report["contact_sheets"]} == {
        "source-contact-sheet.png",
        "mask-contact-sheet.png",
    }
    assert all(Path(item["path"]).is_file() for item in report["contact_sheets"])
