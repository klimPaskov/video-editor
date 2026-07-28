from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import LocalProcessRunner, ProcessRequest, ProcessResult
from videoedit.adapters.worker import WorkerAdapter
from videoedit.errors import ApprovalRequiredError, MaskValidationError, WorkerProcessError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.effects import prepare_matting_overlay
from videoedit.services.foreground import AlphaStatistics
from videoedit.services.matting import (
    build_matting_job,
    build_matting_quality_review,
    render_matting_contrast_previews,
    validate_initial_mask_alignment,
    validate_matting_job,
    verify_matting_payload,
    verify_matting_result,
)
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.worker_runtime import approve_worker_runtime, runtime_identity_sha256


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _v11_job(tmp_path: Path, *, access: str = "approved") -> tuple[Path, dict[str, object]]:
    package_root = _package_root()
    payload = json.loads(
        (package_root / "examples" / "matting_job_v1_1.example.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    payload["job_id"] = "matte-contract-test"
    source_path = (tmp_path / "source.mp4").resolve()
    mask_path = (tmp_path / "person-first-frame.png").resolve()
    payload["input_path"] = str(source_path)
    input_ref = payload["input"]
    assert isinstance(input_ref, dict)
    input_ref["path"] = str(source_path)
    payload["initial_mask_path"] = str(mask_path)
    initial_mask = payload["initial_mask"]
    assert isinstance(initial_mask, dict)
    initial_mask["path"] = str(mask_path)
    payload["output_dir"] = str((tmp_path / "matting-output").resolve())
    runtime = payload["runtime"]
    assert isinstance(runtime, dict)
    runtime["access"] = access
    if access == "approved":
        runtime["checkpoint_path"] = str((tmp_path / "checkpoint.pt").resolve())
        runtime["checkpoint_sha256"] = "5" * 64
        runtime_approval_path = tmp_path / "matanyone-runtime-approval.json"
        runtime_approval = json.loads(
            (package_root / "examples" / "worker_runtime_approval.example.json").read_text(
                encoding="utf-8"
            )
        )
        runtime_approval["project_id"] = "matting_example"
        runtime_approval["worker"] = "matanyone2"
        runtime_approval["upstream_repository"] = "https://github.com/pq-yang/MatAnyone2"
        runtime_approval["upstream_commit"] = str(runtime.get("upstream_commit"))
        runtime_approval["checkpoint_id"] = "matanyone2.pth"
        runtime_approval["checkpoint_sha256"] = "5" * 64
        runtime_approval["license_id"] = "ntu-s-lab-1.0"
        runtime_approval["python"] = "3.10"
        runtime_approval["pytorch"] = str(runtime.get("pytorch", "pending"))
        runtime_approval["cuda"] = str(runtime.get("cuda", "pending"))
        runtime_approval["device"] = str(runtime.get("device", "cuda:0"))
        runtime_approval["identity_sha256"] = runtime_identity_sha256(
            {
                "worker": "matanyone2",
                "upstream_repository": runtime_approval["upstream_repository"],
                "upstream_commit": runtime_approval["upstream_commit"],
                "checkpoint_id": runtime_approval["checkpoint_id"],
                "checkpoint_sha256": runtime_approval["checkpoint_sha256"],
                "license_id": runtime_approval["license_id"],
                "python": runtime_approval["python"],
                "pytorch": runtime_approval["pytorch"],
                "cuda": runtime_approval["cuda"],
                "device": runtime_approval["device"],
            }
        )
        write_validated_artifact(
            package_root, "worker_runtime_approval", runtime_approval_path, runtime_approval
        )
        runtime["runtime_approval"] = {
            "artifact_id": runtime_approval["artifact_id"],
            "path": str(runtime_approval_path.resolve()),
            "sha256": sha256_file(runtime_approval_path),
        }
    else:
        runtime["checkpoint_path"] = None
        runtime["checkpoint_sha256"] = None
        runtime["runtime_approval"] = None
    job_path = tmp_path / "matting-job.json"
    write_validated_artifact(package_root, "matting_job", job_path, payload)
    return job_path, payload


class FakeMattingProbe:
    def __init__(
        self,
        *,
        mask_frame_count: int = 1,
        mask_polarity: tuple[float, float, float] = (0.0, 255.0, 20.0),
        mask_codec: str = "png",
        mask_pixel_format: str = "gray",
        mask_has_audio: bool = False,
    ) -> None:
        self.mask_frame_count = mask_frame_count
        self.mask_polarity = mask_polarity
        self.mask_codec = mask_codec
        self.mask_pixel_format = mask_pixel_format
        self.mask_has_audio = mask_has_audio

    def version(self) -> str:
        return "fake-ffmpeg"

    def probe(self, path: Path) -> dict[str, object]:
        if path.suffix.lower() == ".mp4":
            streams: list[dict[str, object]] = [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "30/1",
                    "r_frame_rate": "30/1",
                    "pix_fmt": "yuv420p",
                    "duration": "1.000000",
                }
            ]
            return {"streams": streams, "format": {"duration": "1.000000"}}
        streams = [
            {
                "codec_type": "video",
                "codec_name": self.mask_codec,
                "width": 320,
                "height": 180,
                "avg_frame_rate": "1/1",
                "r_frame_rate": "1/1",
                "pix_fmt": self.mask_pixel_format,
                "duration": "0.000000",
            }
        ]
        if self.mask_has_audio:
            streams.append({"codec_type": "audio", "codec_name": "pcm_s16le"})
        return {"streams": streams, "format": {"duration": "0.000000"}}

    def probe_frame_count(self, path: Path) -> int:
        return 30 if path.suffix.lower() == ".mp4" else self.mask_frame_count

    def measure_mask(self, _path: Path, *, frame_index: int | None = None) -> ProcessResult:
        minimum, maximum, mean = self.mask_polarity
        return ProcessResult(
            arguments=("ffmpeg", "-vf", "signalstats"),
            exit_code=0,
            stdout=(
                f"lavfi.signalstats.YMIN={minimum} "
                f"lavfi.signalstats.YMAX={maximum} "
                f"lavfi.signalstats.YAVG={mean}"
            ),
            stderr="",
            elapsed_ms=1,
        )

    def full_decode_check(self, _path: Path) -> ProcessResult:
        return ProcessResult(
            arguments=("ffmpeg", "-v", "error"),
            exit_code=0,
            stdout="",
            stderr="",
            elapsed_ms=1,
        )


class FakeMattingOutputProbe:
    def __init__(self, *, alpha_pixel_format: str = "gray", decode_code: int = 0) -> None:
        self.alpha_pixel_format = alpha_pixel_format
        self.decode_code = decode_code

    def probe(self, path: Path) -> dict[str, object]:
        is_alpha = "pha" in path.name.lower() or "alpha" in path.name.lower()
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "ffv1" if is_alpha else "h264",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "30/1",
                    "r_frame_rate": "30/1",
                    "pix_fmt": self.alpha_pixel_format if is_alpha else "yuv420p",
                    "duration": "1.000000",
                }
            ],
            "format": {"duration": "1.000000"},
        }

    def probe_frame_count(self, _path: Path) -> int:
        return 30

    def full_decode_check(self, _path: Path) -> ProcessResult:
        return ProcessResult(
            arguments=("ffmpeg", "-v", "error"),
            exit_code=self.decode_code,
            stdout="",
            stderr="decode failure" if self.decode_code else "",
            elapsed_ms=1,
        )

    def measure_mask(self, _path: Path, *, frame_index: int | None = None) -> ProcessResult:
        return ProcessResult(
            arguments=("ffmpeg", "-vf", "signalstats"),
            exit_code=0,
            stdout=(
                "lavfi.signalstats.YMIN=0 lavfi.signalstats.YMAX=255 lavfi.signalstats.YAVG=100"
            ),
            stderr="",
            elapsed_ms=1,
        )

    def version(self) -> str:
        return "fake-ffmpeg"


class FakeContrastAdapter(FakeMattingOutputProbe):
    def __init__(self) -> None:
        super().__init__()
        self.colors: list[str] = []

    def render_contrasting_background(
        self,
        _foreground: Path,
        _alpha: Path,
        output: Path,
        *,
        color: str,
    ) -> ProcessResult:
        self.colors.append(color)
        output.write_bytes(f"{color} preview".encode())
        return ProcessResult(
            arguments=("ffmpeg", "-filter_complex", f"color={color}"),
            exit_code=0,
            stdout="",
            stderr="",
            elapsed_ms=1,
        )

    def make_contact_sheet(
        self,
        _source: Path,
        output: Path,
        _frame_indices: list[int],
        *,
        scale_width: int = 320,
        tile_columns: int | None = None,
    ) -> ProcessResult:
        output.write_bytes(b"contact sheet")
        return ProcessResult(
            arguments=("ffmpeg", "-vf", "tile"),
            exit_code=0,
            stdout="",
            stderr="",
            elapsed_ms=1,
        )


def _result_payload_with_outputs(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    package_root = _package_root()
    payload = json.loads(
        (package_root / "examples" / "matting_result_v1_1.example.json").read_text(encoding="utf-8")
    )
    foreground = tmp_path / "subject-fgr.mp4"
    alpha = tmp_path / "subject-pha.mp4"
    source = tmp_path / "source.mp4"
    foreground.write_bytes(b"foreground fixture")
    alpha.write_bytes(b"alpha fixture")
    source.write_bytes(b"source fixture")
    payload["input_path"] = str(source)
    payload["input_sha256"] = sha256_file(source)
    input_ref = payload["input"]
    assert isinstance(input_ref, dict)
    input_ref["path"] = str(source)
    input_ref["sha256"] = sha256_file(source)
    payload["output_dir"] = str(tmp_path)
    payload["foreground_path"] = str(foreground)
    payload["alpha_path"] = str(alpha)
    payload["media_outputs"] = [str(foreground), str(alpha)]
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    foreground_ref = outputs["foreground"]
    alpha_ref = outputs["alpha"]
    assert isinstance(foreground_ref, dict)
    assert isinstance(alpha_ref, dict)
    foreground_ref["path"] = str(foreground)
    foreground_ref["sha256"] = sha256_file(foreground)
    alpha_ref["path"] = str(alpha)
    alpha_ref["sha256"] = sha256_file(alpha)
    result_path = tmp_path / "matting-result.json"
    write_validated_artifact(package_root, "matting_result", result_path, payload)
    return result_path, payload


def test_initial_mask_validation_requires_one_lossless_gray_frame() -> None:
    source_probe = FakeMattingProbe().probe(Path("source.mp4"))
    mask_probe = FakeMattingProbe().probe(Path("mask.png"))
    validation = validate_initial_mask_alignment(
        source_probe,
        mask_probe,
        source_frame_count=30,
        mask_frame_count=1,
        mask_samples=[AlphaStatistics(minimum=0, maximum=255, mean=20)],
    )

    assert validation.is_valid
    assert validation.validation["frame_count"] == "pass"
    assert validation.validation["polarity"] == "pass"
    assert validation.mask_statistics["polarity"] == "white_foreground"


def test_initial_mask_validation_rejects_ambiguous_or_multi_frame_masks() -> None:
    source_probe = FakeMattingProbe().probe(Path("source.mp4"))
    mask_probe = FakeMattingProbe(
        mask_frame_count=2,
        mask_codec="h264",
        mask_pixel_format="yuv420p",
    ).probe(Path("mask.png"))
    validation = validate_initial_mask_alignment(
        source_probe,
        mask_probe,
        source_frame_count=30,
        mask_frame_count=2,
        mask_samples=[AlphaStatistics(minimum=60, maximum=190, mean=110)],
    )

    assert not validation.is_valid
    assert validation.validation["frame_count"] == "fail"
    assert validation.validation["polarity"] == "fail"
    assert validation.validation["lossless"] == "fail"


def test_build_matting_job_binds_validated_mask_and_stays_runtime_blocked(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    layout = initialize_project(tmp_path, "matting_builder")
    source = layout.work / "source.mp4"
    mask = layout.work / "person-first-frame.png"
    source.write_bytes(b"source fixture")
    mask.write_bytes(b"mask fixture")

    payload = build_matting_job(
        package_root,
        layout,
        source,
        mask,
        job_id="matte-job-001",
        mask_source="manual",
        mask_approval={"artifact_id": "person_mask_review", "sha256": "5" * 64},
        approval={"artifact_id": "effect_plan", "sha256": "2" * 64},
        upstream_commit="d3bb5a1ebedf259a5453c6d168e6840fff85581e",
        checkpoint_id="matanyone2.pth",
        adapter=FakeMattingProbe(),
    )

    assert payload["schema_version"] == "1.1"
    assert payload["runtime"]["access"] == "blocked"
    assert payload["initial_mask"]["source"] == "manual"
    assert payload["initial_mask_validation"]["status"] == "pass"
    validated = validate_matting_job(package_root, payload)
    assert validated.source_range.start_frame == 0
    assert validated.initial_mask.frame_index == 0

    job_path = tmp_path / "matting-job.json"
    write_validated_artifact(package_root, "matting_job", job_path, payload)
    assert job_path.is_file()


def test_build_matting_job_binds_explicit_runtime_approval(tmp_path: Path) -> None:
    package_root = _package_root()
    layout = initialize_project(tmp_path, "matting_runtime_approval")
    source = layout.work / "source.mp4"
    mask = layout.work / "person-first-frame.png"
    checkpoint = tmp_path / "matanyone2.pth"
    source.write_bytes(b"source fixture")
    mask.write_bytes(b"mask fixture")
    checkpoint.write_bytes(b"operator checkpoint fixture")
    upstream_commit = "d3bb5a1ebedf259a5453c6d168e6840fff85581e"
    checkpoint_hash = sha256_file(checkpoint)
    runtime_approval = approve_worker_runtime(
        package_root,
        layout,
        worker="matanyone2",
        upstream_commit=upstream_commit,
        checkpoint_id="matanyone2.pth",
        checkpoint_sha256=checkpoint_hash,
        pytorch="fixture-pytorch",
        cuda="fixture-cuda",
        device="cuda:0",
        actor="operator@example.test",
        role="licence-owner",
        reason="Fixture acceptance only",
    )
    payload = build_matting_job(
        package_root,
        layout,
        source,
        mask,
        job_id="matte-job-approved-001",
        mask_source="manual",
        mask_approval={"artifact_id": "person_mask_review", "sha256": "5" * 64},
        approval={"artifact_id": "effect_plan", "sha256": "2" * 64},
        upstream_commit=upstream_commit,
        checkpoint_id="matanyone2.pth",
        checkpoint_sha256=checkpoint_hash,
        checkpoint_path=checkpoint,
        runtime_approval_path=runtime_approval,
        runtime_access="approved",
        pytorch="fixture-pytorch",
        cuda="fixture-cuda",
        device="cuda:0",
        adapter=FakeMattingProbe(),
    )

    assert payload["runtime"]["access"] == "approved"
    assert payload["runtime"]["runtime_approval"]["sha256"] == sha256_file(runtime_approval)
    validated = validate_matting_job(package_root, payload)
    assert validated.runtime.runtime_approval is not None


def test_build_matting_job_requires_mask_approval_and_project_owned_mask(tmp_path: Path) -> None:
    package_root = _package_root()
    layout = initialize_project(tmp_path, "matting_approval")
    source = layout.work / "source.mp4"
    mask = layout.work / "person-first-frame.png"
    source.write_bytes(b"source fixture")
    mask.write_bytes(b"mask fixture")
    with pytest.raises(ApprovalRequiredError, match="mask approval"):
        build_matting_job(
            package_root,
            layout,
            source,
            mask,
            job_id="matte-job-002",
            mask_source="manual",
            mask_approval={"artifact_id": "missing"},
            approval={"artifact_id": "effect_plan", "sha256": "2" * 64},
            adapter=FakeMattingProbe(),
        )
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside mask")
    with pytest.raises(MaskValidationError, match="inside the project"):
        build_matting_job(
            package_root,
            layout,
            source,
            outside,
            job_id="matte-job-003",
            mask_source="manual",
            mask_approval={"artifact_id": "person_mask_review", "sha256": "5" * 64},
            approval={"artifact_id": "effect_plan", "sha256": "2" * 64},
            adapter=FakeMattingProbe(),
        )


def test_matting_output_verifier_proves_roles_and_alpha_polarity_before_contrast_review(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    _, payload = _result_payload_with_outputs(tmp_path)
    verified = verify_matting_payload(
        package_root,
        payload,
        adapter=FakeMattingOutputProbe(),
    )

    verification = verified["verification"]
    assert isinstance(verification, dict)
    assert verification["status"] == "pending"
    assert verification["foreground_role"] == "pass"
    assert verification["alpha_role"] == "pass"
    assert verification["alpha_polarity"] == "white_foreground"
    assert verification["dimensions"] == "pass"
    assert verification["frame_count"] == "pass"
    assert verification["decode"] == "pass"
    assert verification["contrasting_background"] == "pending"


def test_matting_output_verifier_rejects_color_alpha_output(tmp_path: Path) -> None:
    package_root = _package_root()
    _, payload = _result_payload_with_outputs(tmp_path)
    verified = verify_matting_payload(
        package_root,
        payload,
        adapter=FakeMattingOutputProbe(alpha_pixel_format="yuv420p"),
    )

    verification = verified["verification"]
    assert isinstance(verification, dict)
    assert verification["status"] == "fail"
    assert verification["alpha_role"] == "fail"
    assert verification["alpha_polarity"] == "unknown"


def test_matting_output_verifier_writes_new_hash_bound_revision(tmp_path: Path) -> None:
    package_root = _package_root()
    result_path, _ = _result_payload_with_outputs(tmp_path)
    verified_path = verify_matting_result(
        package_root,
        result_path,
        adapter=FakeMattingOutputProbe(),
    )

    assert verified_path != result_path
    assert result_path.is_file()
    verified = json.loads(verified_path.read_text(encoding="utf-8"))
    from videoedit.services.artifacts import validate_artifact

    validate_artifact(package_root, "matting_result", verified)
    assert verified["verification"]["alpha_role"] == "pass"


def test_matting_contrast_review_is_atomic_and_pending_operator_comparison(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    result_path, _ = _result_payload_with_outputs(tmp_path)
    adapter = FakeContrastAdapter()

    manifest_path = render_matting_contrast_previews(
        package_root,
        result_path,
        adapter=adapter,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    from videoedit.services.artifacts import validate_artifact

    validate_artifact(package_root, "matting_contrast_review", manifest)
    assert manifest["status"] == "pending"
    assert manifest["validation"]["preview_distinct"] == "pass"
    assert manifest["validation"]["background_comparison"] == "pending"
    assert adapter.colors == ["black", "white"]
    assert manifest_path.parent.name.startswith("matting-result-contrast-")
    for reference in (
        manifest["source_contact_sheet"],
        manifest["previews"]["black"]["preview"],
        manifest["previews"]["black"]["contact_sheet"],
        manifest["previews"]["white"]["preview"],
        manifest["previews"]["white"]["contact_sheet"],
    ):
        assert Path(reference["path"]).is_file()


def test_ffmpeg_contrast_preview_rejects_unapproved_background_color(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="color must be black or white"):
        FFmpegAdapter().render_contrasting_background(
            tmp_path / "foreground.mov",
            tmp_path / "alpha.mkv",
            tmp_path / "preview.mp4",
            color="gray",
        )


def test_matting_quality_review_records_alpha_evidence_and_pending_categories(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    result_path, _ = _result_payload_with_outputs(tmp_path)
    adapter = FakeContrastAdapter()
    contrast_path = render_matting_contrast_previews(
        package_root,
        result_path,
        adapter=adapter,
    )

    quality_path = build_matting_quality_review(
        package_root,
        result_path,
        contrast_path,
        adapter=adapter,
    )

    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    from videoedit.services.artifacts import validate_artifact

    validate_artifact(package_root, "matting_quality_review", quality)
    assert quality["status"] == "pending"
    assert quality["validation"]["alpha_decode"] == "pass"
    assert quality["validation"]["alpha_range"] == "pass"
    assert quality["validation"]["frame_coverage"] == "pass"
    assert quality["validation"]["semantic_categories"] == "pending"
    assert len(quality["alpha_samples"]) >= 3
    assert set(quality["category_reviews"]) == {
        "hair",
        "fingers",
        "clothing",
        "holes",
        "transparent_regions",
        "fast_motion",
        "motion_blur",
        "entry_exit",
        "temporal_edges",
    }
    assert all(review["status"] == "pending" for review in quality["category_reviews"].values())


def test_fake_matanyone_worker_round_trip_is_schema_valid(tmp_path: Path) -> None:
    package_root = _package_root()
    job_path, job = _v11_job(tmp_path)
    result_path = tmp_path / "result" / "matting-result.json"
    worker = package_root / "tests" / "fixtures" / "fake_matanyone2_worker.py"
    result = WorkerAdapter(
        [sys.executable, str(worker)],
        job_schema=package_root / "schemas" / "matting_job.schema.json",
        result_schema=package_root / "schemas" / "matting_result.schema.json",
        timeout_seconds=30,
    ).run(job_path, result_path=result_path)

    assert result["status"] == "complete"
    assert result["job_id"] == job["job_id"]
    assert result["verification"]["status"] == "pending"
    assert result_path.is_file()


def test_fake_matanyone_worker_rejects_unapproved_runtime(tmp_path: Path) -> None:
    package_root = _package_root()
    job_path, _ = _v11_job(tmp_path, access="blocked")
    worker = package_root / "tests" / "fixtures" / "fake_matanyone2_worker.py"
    with pytest.raises(WorkerProcessError, match="runtime gate"):
        WorkerAdapter(
            [sys.executable, str(worker)],
            job_schema=package_root / "schemas" / "matting_job.schema.json",
            result_schema=package_root / "schemas" / "matting_result.schema.json",
            timeout_seconds=30,
        ).run(job_path)


def test_real_matanyone_worker_runtime_guard_rejects_stale_configuration_and_expiry(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    _, job = _v11_job(tmp_path)
    namespace = runpy.run_path(
        str(package_root / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_runtime_guard_test",
    )
    verify_runtime_approval = namespace["_verify_runtime_approval"]
    runtime = job["runtime"]
    assert isinstance(runtime, dict)
    job["project_id"] = "matting_example"

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


def test_real_matanyone_worker_dry_run_fails_closed_at_runtime_gate(tmp_path: Path) -> None:
    package_root = _package_root()
    job_path, job = _v11_job(tmp_path, access="blocked")
    worker = package_root / "workers" / "matanyone2" / "run_job.py"
    result = LocalProcessRunner().run(
        ProcessRequest(
            executable=sys.executable,
            arguments=(str(worker), str(job_path), "--dry-run"),
            working_directory=tmp_path,
            timeout_seconds=30,
        )
    )
    assert result.exit_code != 0
    assert "runtime gate" in result.stderr
    failure_path = Path(str(job["output_dir"])) / "matting-result.json"
    assert failure_path.is_file()
    from videoedit.services.artifacts import validate_artifact

    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "matting_result", failure)


def test_matanyone_v11_example_normalizes_portable_paths_before_runtime_gate() -> None:
    package_root = _package_root()
    job = json.loads(
        (package_root / "examples" / "matting_job_v1_1.example.json").read_text(encoding="utf-8")
    )
    namespace = runpy.run_path(
        str(package_root / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_portable_example_guard_test",
    )
    with pytest.raises(RuntimeError, match="runtime gate"):
        namespace["validate_job"](job)


def test_real_matanyone_worker_stages_partial_ranges_and_reuses_valid_stage(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    source = tmp_path / "source.mp4"
    adapter = FFmpegAdapter()
    adapter.generate_edit_demo_source(source)
    probe = adapter.probe(source)
    video = next(
        item
        for item in probe["streams"]
        if isinstance(item, dict) and item["codec_type"] == "video"
    )
    frame_count = adapter.probe_frame_count(source)
    assert frame_count is not None and frame_count > 30
    job = {
        "input_path": str(source),
        "input_sha256": sha256_file(source),
        "input_video": {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "frame_count": frame_count,
            "frame_rate": str(video["avg_frame_rate"]),
        },
        "source_range": {"start_frame": 0, "end_frame": 30},
    }
    namespace = runpy.run_path(
        str(package_root / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_bounded_input_stage_test",
    )
    stage = namespace["_stage_bounded_input"]
    output_dir = tmp_path / "output"
    staged_path, first_metadata = stage(job, output_dir)
    assert first_metadata["staged"] is True
    assert adapter.probe_frame_count(staged_path) == 30
    staged_probe = adapter.probe(staged_path)
    assert not any(item.get("codec_type") == "audio" for item in staged_probe["streams"])
    assert sha256_file(source) == job["input_sha256"]

    reused_path, second_metadata = stage(job, output_dir)
    assert reused_path == staged_path
    assert second_metadata == first_metadata


def test_real_matanyone_worker_rejects_output_bounds_that_do_not_match_job() -> None:
    package_root = _package_root()
    namespace = runpy.run_path(
        str(package_root / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_output_bound_guard_test",
    )
    validate_output_bounds = namespace["_validate_output_bounds"]
    job = {
        "input_video": {
            "width": 320,
            "height": 180,
            "frame_count": 30,
            "frame_rate": "30/1",
        },
        "source_range": {"start_frame": 0, "end_frame": 30},
    }
    foreground = {
        "width": 320,
        "height": 180,
        "frame_count": 29,
        "frame_rate": "30/1",
        "duration_us": 966667,
        "pixel_format": "yuv420p",
    }
    alpha = {**foreground, "frame_count": 30, "duration_us": 1000000, "pixel_format": "gray"}
    with pytest.raises(RuntimeError, match="frame count"):
        validate_output_bounds(job, {"foreground": foreground, "alpha": alpha})

    valid_foreground = {**foreground, "frame_count": 30, "duration_us": 1000000}
    valid_alpha = {**alpha, "duration_us": 1000000}
    with pytest.raises(RuntimeError, match="dimensions"):
        validate_output_bounds(
            job,
            {"foreground": {**valid_foreground, "width": 640}, "alpha": valid_alpha},
        )
    with pytest.raises(RuntimeError, match="alpha output"):
        validate_output_bounds(
            job,
            {"foreground": valid_foreground, "alpha": {**valid_alpha, "pixel_format": "yuv420p"}},
        )


def test_real_matanyone_worker_rejects_legacy_live_contract_before_upstream_import(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    source = tmp_path / "source.mp4"
    mask = tmp_path / "person-first-frame.png"
    source.write_bytes(b"legacy matting source")
    mask.write_bytes(b"legacy first-frame mask")
    job = {
        "schema_version": "1.0",
        "job_id": "legacy-matting-job",
        "input_path": str(source.resolve()),
        "initial_mask_path": str(mask.resolve()),
        "output_dir": str((tmp_path / "output").resolve()),
    }
    namespace = runpy.run_path(
        str(package_root / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_legacy_live_guard_test",
    )
    with pytest.raises(RuntimeError, match=r"validation-only.*local checkpoint"):
        namespace["run"](job)


def test_unverified_v11_matte_cannot_be_consumed(tmp_path: Path) -> None:
    package_root = _package_root()
    payload = json.loads(
        (package_root / "examples" / "matting_result_v1_1.example.json").read_text(encoding="utf-8")
    )
    result_path = tmp_path / "matting-result.json"
    write_validated_artifact(package_root, "matting_result", result_path, payload)
    with pytest.raises(ValueError, match="not independently verified"):
        prepare_matting_overlay(package_root, result_path, tmp_path / "overlay.mov")


def test_verified_v11_matte_requires_current_output_hashes(tmp_path: Path) -> None:
    package_root = _package_root()
    payload = json.loads(
        (package_root / "examples" / "matting_result_v1_1.example.json").read_text(encoding="utf-8")
    )
    foreground = tmp_path / "subject_fgr.mp4"
    alpha = tmp_path / "subject_pha.mp4"
    foreground.write_bytes(b"foreground fixture")
    alpha.write_bytes(b"alpha fixture")
    payload["output_dir"] = str(tmp_path)
    payload["foreground_path"] = str(foreground)
    payload["alpha_path"] = str(alpha)
    payload["media_outputs"] = [str(foreground), str(alpha)]
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    foreground_ref = outputs["foreground"]
    alpha_ref = outputs["alpha"]
    assert isinstance(foreground_ref, dict)
    assert isinstance(alpha_ref, dict)
    foreground_ref["path"] = str(foreground)
    foreground_ref["sha256"] = sha256_file(foreground)
    alpha_ref["path"] = str(alpha)
    alpha_ref["sha256"] = sha256_file(alpha)
    verification = payload["verification"]
    assert isinstance(verification, dict)
    for key in (
        "status",
        "foreground_role",
        "alpha_role",
        "dimensions",
        "frame_count",
        "decode",
        "contrasting_background",
    ):
        verification[key] = "pass"
    verification["alpha_polarity"] = "white_foreground"
    result_path = tmp_path / "matting-result.json"
    write_validated_artifact(package_root, "matting_result", result_path, payload)

    class FakeFFmpeg(FakeMattingOutputProbe):
        def attach_alpha(self, foreground_path: Path, alpha_path: Path, output: Path) -> None:
            assert foreground_path == foreground
            assert alpha_path == alpha
            output.write_bytes(b"attached")

    output = tmp_path / "overlay.mov"
    assert (
        prepare_matting_overlay(package_root, result_path, output, adapter=FakeFFmpeg()) == output
    )
    assert output.read_bytes() == b"attached"


def test_legacy_matting_example_remains_schema_valid() -> None:
    package_root = _package_root()
    for name in ("matting_job.example.json", "matting_result.example.json"):
        payload = json.loads((package_root / "examples" / name).read_text(encoding="utf-8"))
        schema_name = "matting_job" if "job" in name else "matting_result"
        from videoedit.services.artifacts import validate_artifact

        validate_artifact(package_root, schema_name, payload)
