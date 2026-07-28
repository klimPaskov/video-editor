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
        print("fake MatAnyone 2 runtime gate is not approved", file=sys.stderr)
        return 1
    if not isinstance(runtime.get("runtime_approval"), dict):
        print("fake MatAnyone 2 runtime approval is missing", file=sys.stderr)
        return 1

    source_range = job["source_range"]
    input_video = job["input_video"]
    start_frame = int(source_range["start_frame"])
    end_frame = int(source_range["end_frame"])
    output_dir = str(Path(job["output_dir"]).resolve())
    foreground = f"{output_dir}/subject_fgr.mp4"
    alpha = f"{output_dir}/subject_pha.mp4"
    output_ref = {
        "path": foreground,
        "sha256": "3" * 64,
        "width": int(input_video["width"]),
        "height": int(input_video["height"]),
        "frame_count": end_frame - start_frame,
        "frame_rate": input_video["frame_rate"],
        "duration_us": int(input_video.get("duration_us", 0)),
        "pixel_format": "yuv420p",
    }
    alpha_ref = {**output_ref, "path": alpha, "sha256": "4" * 64, "pixel_format": "gray"}
    result = {
        "schema_version": job["schema_version"],
        "job_id": job["job_id"],
        "status": "complete",
        "worker": "matanyone2",
        "config_sha256": job["config_sha256"],
        "project_id": job["project_id"],
        "revision_id": job["revision_id"],
        "input_path": job["input_path"],
        "input_sha256": job["input_sha256"],
        "input": job["input"],
        "input_video": input_video,
        "source_range": source_range,
        "initial_mask_path": job["initial_mask_path"],
        "initial_mask_sha256": job["initial_mask_sha256"],
        "initial_mask": job["initial_mask"],
        "mask_approval": job["mask_approval"],
        "output_dir": output_dir,
        "foreground_path": foreground,
        "alpha_path": alpha,
        "media_outputs": [foreground, alpha],
        "outputs": {"foreground": output_ref, "alpha": alpha_ref},
        "output_contract": job["output_contract"],
        "verification": {
            "status": "pending",
            "foreground_role": "pending",
            "alpha_role": "pending",
            "alpha_polarity": "unknown",
            "dimensions": "pass",
            "frame_count": "pass",
            "decode": "pending",
            "contrasting_background": "pending",
        },
        "diagnostics": {
            "review_frame_indices": [start_frame, end_frame - 1],
            "warnings": ["Fake worker output requires human semantic verification."],
            "stability": {
                "status": "pending",
                "hair": "pending",
                "fingers": "pending",
                "clothing": "pending",
                "holes": "pending",
                "transparent_regions": "pending",
                "fast_motion": "pending",
                "motion_blur": "pending",
                "entry_exit": "pending",
                "temporal_edges": "pending",
            },
        },
        "upstream_return": "fake",
        "model_id": "PeiqingYang/MatAnyone2",
        "device": runtime["device"],
        "software": {
            "worker": "fake-matanyone2-worker-v1",
            "upstream_commit": runtime["upstream_commit"],
            "checkpoint_id": runtime["checkpoint_id"],
            "checkpoint_sha256": runtime["checkpoint_sha256"],
        },
        "raw_worker_metadata": {"fixture": True},
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
