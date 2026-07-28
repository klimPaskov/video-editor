from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout


def _finding(
    finding_id: str,
    check_code: str,
    status: str,
    severity: str,
    message: str,
    evidence: dict[str, Any],
    required: bool,
    repair_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "check_code": check_code,
        "status": status,
        "severity": severity,
        "message": message,
        "time_range": None,
        "evidence": evidence,
        "repair_hint": repair_hint,
        "required": required,
    }


def qa_render(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    profile_id: str = "pro_youtube_1080p",
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    render_manifest_path = render_manifest_path.resolve()
    render_manifest = json.loads(render_manifest_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "render_manifest", render_manifest)
    media_path = Path(render_manifest["output"]["path"])
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    adapter = adapter or FFmpegAdapter()
    probe = adapter.probe(media_path)
    streams = probe.get("streams", [])
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    findings: list[dict[str, Any]] = []

    decode_result = adapter.full_decode_check(media_path)
    findings.append(
        _finding(
            "qaf_decode",
            "MEDIA_DECODE",
            "pass" if decode_result.exit_code == 0 else "fail",
            "info" if decode_result.exit_code == 0 else "critical",
            "Full output decoded successfully"
            if decode_result.exit_code == 0
            else "FFmpeg reported an error while decoding the complete output",
            {
                "exit_code": decode_result.exit_code,
                "stderr_tail": decode_result.stderr[-1000:],
            },
            True,
            None if decode_result.exit_code == 0 else "Inspect the render log and rerender",
        )
    )
    findings.append(
        _finding(
            "qaf_video",
            "VIDEO_STREAM",
            "pass" if video_streams else "fail",
            "info" if video_streams else "critical",
            "A video stream is present" if video_streams else "No video stream was found",
            {"count": len(video_streams)},
            True,
            None if video_streams else "Rerender with a mapped video stream",
        )
    )
    findings.append(
        _finding(
            "qaf_audio",
            "AUDIO_STREAM",
            "pass" if audio_streams else "fail",
            "info" if audio_streams else "high",
            "An audio stream is present" if audio_streams else "No audio stream was found",
            {"count": len(audio_streams)},
            True,
            None if audio_streams else "Rerender with the program audio or explicit silence",
        )
    )

    actual_duration_us = seconds_to_us(probe.get("format", {}).get("duration"))
    expected_duration_us = int(render_manifest["expected_duration_us"])
    drift_us = (
        abs(actual_duration_us - expected_duration_us)
        if actual_duration_us is not None
        else expected_duration_us
    )
    duration_pass = actual_duration_us is not None and drift_us <= 100_000
    findings.append(
        _finding(
            "qaf_duration",
            "DURATION_MATCH",
            "pass" if duration_pass else "fail",
            "info" if duration_pass else "high",
            "Output duration matches the approved timeline"
            if duration_pass
            else "Output duration differs from the approved timeline",
            {
                "expected_duration_us": expected_duration_us,
                "actual_duration_us": actual_duration_us,
                "absolute_difference_us": drift_us,
                "maximum_difference_us": 100_000,
            },
            True,
            None if duration_pass else "Check trim boundaries and concat timestamps",
        )
    )

    if video_streams:
        stream = video_streams[0]
        expected_video = render_manifest["video"]
        dimensions_pass = int(stream.get("width") or 0) == int(expected_video["width"]) and int(
            stream.get("height") or 0
        ) == int(expected_video["height"])
        findings.append(
            _finding(
                "qaf_dimensions",
                "VIDEO_DIMENSIONS",
                "pass" if dimensions_pass else "fail",
                "info" if dimensions_pass else "high",
                "Output dimensions match the render manifest"
                if dimensions_pass
                else "Output dimensions do not match the render manifest",
                {
                    "expected_width": expected_video["width"],
                    "expected_height": expected_video["height"],
                    "actual_width": stream.get("width"),
                    "actual_height": stream.get("height"),
                },
                True,
                None if dimensions_pass else "Check scale and composition settings",
            )
        )

    required_failures = sum(1 for item in findings if item["required"] and item["status"] == "fail")
    warnings_count = sum(1 for item in findings if item["status"] == "warning")
    overall_status = "fail" if required_failures else ("warning" if warnings_count else "pass")
    payload = {
        "schema_name": "qa_report",
        "schema_version": "1.0.0",
        "artifact_id": "art_qa",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("media-qa", "ffmpeg", adapter.version()),
        "inputs": [artifact_input(render_manifest["artifact_id"], render_manifest_path)],
        "config_sha256": config_sha256(layout),
        "render_artifact_id": render_manifest["artifact_id"],
        "profile_id": profile_id,
        "overall_status": overall_status,
        "final_ready": required_failures == 0,
        "findings": findings,
        "required_failures": required_failures,
        "warnings_count": warnings_count,
    }
    output = layout.artifacts / "qa-report.json"
    write_validated_artifact(package_root, "qa_report", output, payload)
    return output


def basic_media_qa(
    path: Path,
    report_path: Path,
    *,
    adapter: FFmpegAdapter | None = None,
) -> dict[str, Any]:
    """Small standalone probe for media outside a managed project."""

    selected_adapter = adapter or FFmpegAdapter()
    probe = selected_adapter.probe(path)
    streams = probe.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    decode = selected_adapter.full_decode_check(path)
    findings: list[dict[str, str]] = []
    if not video_streams:
        findings.append({"severity": "error", "message": "No video stream found"})
    if not audio_streams:
        findings.append({"severity": "warning", "message": "No audio stream found"})
    if decode.exit_code != 0:
        findings.append({"severity": "error", "message": "Full media decode failed"})
    duration = probe.get("format", {}).get("duration")
    if duration is None:
        findings.append({"severity": "error", "message": "Container duration is missing"})
    status = "fail" if any(item["severity"] == "error" for item in findings) else "pass"
    report = {
        "schema_version": "1.0",
        "path": str(path),
        "status": status,
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "duration_seconds": duration,
        "findings": findings,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
