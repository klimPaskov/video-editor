from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from videoedit.adapters.worker import WorkerAdapter
from videoedit.errors import WorkerContractError, WorkerProcessError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.project import sha256_file
from videoedit.services.worker_runtime import runtime_identity_sha256


def _fake_sam_job(tmp_path: Path, *, access: str = "approved") -> tuple[Path, dict[str, object]]:
    package_root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake segmentation source")
    checkpoint = tmp_path / "sam3.1_multiplex.pt"
    checkpoint.write_bytes(b"fake checkpoint")
    source_hash = sha256_file(source)
    checkpoint_hash = sha256_file(checkpoint)
    output_dir = tmp_path / "sam3" / "fake-job"
    runtime_approval_path = tmp_path / "sam-runtime-approval.json"
    runtime_approval = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "worker_runtime_approval.example.json"
        ).read_text(encoding="utf-8")
    )
    runtime_approval["project_id"] = "fake_sam"
    runtime_approval["checkpoint_sha256"] = checkpoint_hash
    runtime_approval["pytorch"] = "2.7"
    runtime_approval["cuda"] = "12.6"
    runtime_approval["device"] = "fixture"
    runtime_approval["identity_sha256"] = runtime_identity_sha256(
        {
            "worker": "sam3",
            "upstream_repository": "https://github.com/facebookresearch/sam3",
            "upstream_commit": runtime_approval["upstream_commit"],
            "checkpoint_id": runtime_approval["checkpoint_id"],
            "checkpoint_sha256": checkpoint_hash,
            "license_id": runtime_approval["license_id"],
            "python": runtime_approval["python"],
            "pytorch": runtime_approval["pytorch"],
            "cuda": runtime_approval["cuda"],
            "device": runtime_approval["device"],
        }
    )
    write_validated_artifact(
        Path(__file__).resolve().parents[2],
        "worker_runtime_approval",
        runtime_approval_path,
        runtime_approval,
    )
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "job_id": "fake-sam-job-001",
        "config_sha256": "0" * 64,
        "project_id": "fake_sam",
        "revision_id": "rev_001",
        "input_path": str(source.resolve()),
        "input_sha256": source_hash,
        "input": {
            "path": str(source.resolve()),
            "sha256": source_hash,
            "size_bytes": source.stat().st_size,
        },
        "input_video": {
            "width": 4,
            "height": 4,
            "frame_count": 3,
            "frame_rate": "30/1",
            "duration_us": 100_000,
        },
        "output_dir": str(output_dir.resolve()),
        "source_range": {"start_frame": 0, "end_frame": 3},
        "prompt": {"type": "text", "text": "the test object", "frame_index": 0},
        "expected_object_count": 1,
        "output_contract": {
            "mask_format": "png_gray8",
            "lossless": True,
            "polarity": "white_foreground",
        },
        "approval": {"artifact_id": "effect_plan", "sha256": "1" * 64},
        "worker": {
            "name": "sam3",
            "contract_version": "1.1",
            "implementation_version": "videoedit:segmentation-v1",
        },
        "runtime": {
            "upstream_repository": "https://github.com/facebookresearch/sam3",
            "upstream_commit": "46957e47805eaa273f4aa7bbbd25a88bca9108ce",
            "checkpoint_id": "facebook/sam3.1/sam3.1_multiplex.pt",
            "checkpoint_sha256": checkpoint_hash if access == "approved" else None,
            "checkpoint_path": str(checkpoint.resolve()) if access == "approved" else None,
            "python": "3.12",
            "pytorch": "2.7",
            "cuda": "12.6",
            "device": "fixture",
            "access": access,
            "runtime_approval": (
                {
                    "artifact_id": runtime_approval["artifact_id"],
                    "path": str(runtime_approval_path.resolve()),
                    "sha256": sha256_file(runtime_approval_path),
                }
                if access == "approved"
                else None
            ),
        },
        "start_frame": 0,
        "end_frame": 3,
        "gpus": [0],
    }
    job_path = tmp_path / "fake-sam-job.json"
    write_validated_artifact(package_root, "segmentation_job", job_path, payload)
    return job_path, payload


def test_fake_sam_worker_round_trip_is_schema_valid_and_hash_bound(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    job_path, job = _fake_sam_job(tmp_path)
    result_path = tmp_path / "result" / "segmentation-result.json"
    worker = package_root / "tests" / "fixtures" / "fake_sam3_worker.py"
    result = WorkerAdapter(
        [sys.executable, str(worker)],
        job_schema=package_root / "schemas" / "segmentation_job.schema.json",
        result_schema=package_root / "schemas" / "segmentation_result.schema.json",
        timeout_seconds=30,
    ).run(job_path, result_path=result_path)

    assert result["status"] == "complete"
    assert result["job_id"] == job["job_id"]
    assert result["input_sha256"] == job["input_sha256"]
    assert result["frame_count"] == 3
    assert result_path.is_file()
    assert json.loads(result_path.read_text(encoding="utf-8")) == result


def test_fake_sam_worker_rejects_unapproved_runtime(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    job_path, _ = _fake_sam_job(tmp_path, access="blocked")
    worker = package_root / "tests" / "fixtures" / "fake_sam3_worker.py"
    with pytest.raises(WorkerProcessError, match="runtime gate"):
        WorkerAdapter(
            [sys.executable, str(worker)],
            job_schema=package_root / "schemas" / "segmentation_job.schema.json",
            result_schema=package_root / "schemas" / "segmentation_result.schema.json",
            timeout_seconds=30,
        ).run(job_path)


def test_real_sam_worker_runtime_guard_rejects_stale_configuration_and_expiry(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    _, job = _fake_sam_job(tmp_path)
    namespace = runpy.run_path(
        str(package_root / "workers" / "sam3" / "run_job.py"),
        run_name="sam3_runtime_guard_test",
    )
    verify_runtime_approval = namespace["_verify_runtime_approval"]
    runtime = job["runtime"]
    assert isinstance(runtime, dict)

    job["config_sha256"] = "1" * 64
    with pytest.raises(RuntimeError, match="stale for the job configuration"):
        verify_runtime_approval(job, runtime, require_files=True)

    job["config_sha256"] = "0" * 64
    approval_ref = runtime["runtime_approval"]
    assert isinstance(approval_ref, dict)
    approval_path = Path(str(approval_ref["path"]))
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["expires_at"] = "2020-01-01T00:00:00Z"
    write_validated_artifact(package_root, "worker_runtime_approval", approval_path, approval)
    approval_ref["sha256"] = sha256_file(approval_path)
    with pytest.raises(RuntimeError, match="has expired"):
        verify_runtime_approval(job, runtime, require_files=True)


def test_real_sam_worker_rejects_unbounded_range_and_stale_source_hash(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(
        str(package_root / "workers" / "sam3" / "run_job.py"),
        run_name="sam3_input_bound_guard_test",
    )
    validate_job = namespace["validate_job"]

    range_root = tmp_path / "range"
    range_root.mkdir()
    _, range_job = _fake_sam_job(range_root)
    range_job["source_range"] = {"start_frame": 0, "end_frame": 4}
    range_job["start_frame"] = 0
    range_job["end_frame"] = 4
    with pytest.raises(ValueError, match="source range exceeds input video frame count"):
        validate_job(range_job)

    hash_root = tmp_path / "hash"
    hash_root.mkdir()
    _, hash_job = _fake_sam_job(hash_root)
    hash_job["input_sha256"] = "f" * 64
    input_ref = hash_job["input"]
    assert isinstance(input_ref, dict)
    input_ref["sha256"] = "f" * 64
    with pytest.raises(RuntimeError, match="source hash changed"):
        validate_job(hash_job, require_files=True)


def test_sam_v11_portable_input_path_reaches_runtime_gate(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(
        str(package_root / "workers" / "sam3" / "run_job.py"),
        run_name="sam3_portable_input_guard_test",
    )
    _, job = _fake_sam_job(tmp_path, access="blocked")
    input_ref = job["input"]
    assert isinstance(input_ref, dict)
    input_ref["path"] = "/absolute/path/to/input.mp4"
    with pytest.raises(RuntimeError, match="runtime gate"):
        namespace["validate_job"](job)


def test_real_sam_worker_rejects_empty_and_mismatched_masks(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(
        str(package_root / "workers" / "sam3" / "run_job.py"),
        run_name="sam3_mask_shape_guard_test",
    )
    save_frame = namespace["save_frame"]
    import numpy as np

    with pytest.raises(ValueError, match="returned no masks"):
        save_frame(tmp_path / "empty", 0, {"masks": np.empty((0, 4, 4), dtype=bool)})
    with pytest.raises(ValueError, match="mask dimensions"):
        save_frame(
            tmp_path / "mismatch",
            0,
            {"masks": np.ones((1, 2, 2), dtype=bool)},
            expected_shape=(4, 4),
        )


def test_real_sam_worker_rejects_legacy_live_contract_before_upstream_import(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source.mp4"
    source.write_bytes(b"legacy segmentation source")
    job = {
        "schema_version": "1.0",
        "job_id": "legacy-sam-job",
        "input_path": str(source.resolve()),
        "output_dir": str((tmp_path / "output").resolve()),
        "prompt": {"text": "the test object", "frame_index": 0},
        "start_frame": 0,
        "end_frame": 1,
    }
    namespace = runpy.run_path(
        str(package_root / "workers" / "sam3" / "run_job.py"),
        run_name="sam3_legacy_live_guard_test",
    )
    with pytest.raises(RuntimeError, match=r"validation-only.*local checkpoint"):
        namespace["run"](job)


def test_worker_adapter_reads_json(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        "import json,sys; print(json.dumps({"
        "'schema_version':'1.0','job_id':'test','status':'complete',"
        "'job':sys.argv[1]}))",
        encoding="utf-8",
    )
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema_version": "1.0", "job_id": "test"}), encoding="utf-8")
    payload = WorkerAdapter(f"{sys.executable} {script}").run(job)
    assert payload["status"] == "complete"
    assert payload["job"] == str(job.resolve())


def test_worker_adapter_rejects_stale_result_version(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        "import json; print(json.dumps({'schema_version':'2.0','job_id':'test'}))",
        encoding="utf-8",
    )
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema_version": "1.0", "job_id": "test"}), encoding="utf-8")
    with pytest.raises(WorkerContractError, match="schema_version"):
        WorkerAdapter([sys.executable, str(script)]).run(job)
