from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    job_path = Path(sys.argv[1]).resolve()
    job: dict[str, Any] = json.loads(job_path.read_text(encoding="utf-8"))
    runtime = job.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("access") != "approved":
        print("fake SAM 3.1 runtime gate is not approved", file=sys.stderr)
        return 1
    if not isinstance(runtime.get("runtime_approval"), dict):
        print("fake SAM 3.1 runtime approval is missing", file=sys.stderr)
        return 1

    source_range = job["source_range"]
    start_frame = int(source_range["start_frame"])
    end_frame = int(source_range["end_frame"])
    output_dir = Path(job["output_dir"]).resolve()
    mask_pattern = str((output_dir / "masks" / "%06d.png").resolve())
    frames = [
        {
            "frame_index": frame_index,
            "combined_mask_path": str((output_dir / "masks" / f"{frame_index:06d}.png").resolve()),
            "objects": [
                {
                    "object_id": 1,
                    "mask_path": str(
                        (output_dir / "instances" / f"{frame_index:06d}-object-1.png").resolve()
                    ),
                    "visible": True,
                    "area_pixels": 4,
                    "bbox_xywh": [1, 1, 2, 2],
                    "centroid_xy": [1.5, 1.5],
                    "area_ratio": 0.25,
                }
            ],
        }
        for frame_index in range(start_frame, end_frame)
    ]
    result = {
        "schema_version": job["schema_version"],
        "job_id": job["job_id"],
        "status": "complete",
        "worker": "sam3",
        "project_id": job["project_id"],
        "revision_id": job["revision_id"],
        "input_path": job["input_path"],
        "input_sha256": job["input_sha256"],
        "input": job["input"],
        "source_range": source_range,
        "input_video": job["input_video"],
        "prompt": job["prompt"],
        "mask_pattern": mask_pattern,
        "frame_count": len(frames),
        "frames": frames,
        "software": {
            "worker": "fake-sam3-worker-v1",
            "upstream_commit": runtime["upstream_commit"],
            "checkpoint_id": runtime["checkpoint_id"],
            "checkpoint_sha256": runtime["checkpoint_sha256"],
        },
        "output": {
            "mask_format": "png_gray8",
            "lossless": True,
            "mask_pattern": mask_pattern,
            "mask_count": len(frames),
        },
        "diagnostics": {
            "missing_frames": [],
            "identity_warnings": [],
            "area_jump_frames": [],
            "centroid_jump_frames": [],
            "leak_warnings": [],
            "review_frame_indices": [start_frame, end_frame - 1],
            "status": "pass",
        },
        "raw_worker_metadata": {"fixture": True},
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
