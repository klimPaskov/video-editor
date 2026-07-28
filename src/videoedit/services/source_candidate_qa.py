from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_lock import _owned_path
from videoedit.services.segment_qa import _stream_durations

IMPLEMENTATION_VERSION = "p11-source-candidate-5"
AV_SYNC_TOLERANCE_US = 100_000
_CLIPPED_SAMPLES_PATTERN = re.compile(r"Number\s+of\s+clipped\s+samples:\s*(\d+)", re.IGNORECASE)
_VISUAL_SUFFIXES = frozenset(
    {
        ".avi",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".tif",
        ".tiff",
        ".webm",
        ".webp",
        ".png",
    }
)

DEFAULT_PROFILE: dict[str, Any] = {
    "width": 2560,
    "height": 1440,
    "fps": {"numerator": 60, "denominator": 1},
    "video_codec": "h264",
    "video_encoder": "libx264",
    "qp": 0,
    "pix_fmt": "yuv420p",
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "audio_codec": "pcm_f32le",
    "audio_sample_fmt": "flt",
    "sample_rate_hz": 48000,
    "channels": 2,
}


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _finding(
    finding_id: str,
    check_code: str,
    status: str,
    severity: str,
    message: str,
    required: bool,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "check_code": check_code,
        "status": status,
        "severity": severity,
        "message": message,
        "required": required,
        "evidence": dict(evidence),
    }


def _validated_input(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    schema_name: str,
    description: str,
) -> tuple[Path, dict[str, Any]]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    value = _read_object(selected, description)
    validate_artifact(package_root, schema_name, value)
    if value.get("project_id") != layout.root.name:
        raise PlanningValidationError(f"{description} belongs to another project")
    return selected, value


def _candidate_path(layout: ProjectLayout, path: Path) -> Path:
    selected = path.expanduser().resolve()
    workspace = layout.root.parent.parent.resolve()
    allowed_roots = (layout.root.resolve(), (workspace / "outputs").resolve())
    if not any(selected == root or root in selected.parents for root in allowed_roots):
        raise PlanningValidationError(
            "source candidate must be inside the project or workspace outputs directory"
        )
    if not selected.is_file():
        raise PlanningValidationError(f"source candidate does not exist: {selected}")
    return selected


def _source_media_path(source: Mapping[str, Any]) -> Path:
    selected = (
        source.get("managed_path")
        if source.get("ingest_mode") == "copy"
        else source.get("source_path")
    )
    if not isinstance(selected, str) or not selected:
        raise PlanningValidationError("source manifest does not identify source media")
    return Path(selected).expanduser().resolve()


def _stream(probe: Mapping[str, Any], codec_type: str) -> Mapping[str, Any] | None:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return None
    return next(
        (
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == codec_type
        ),
        None,
    )


def _command_has_pair(command: Mapping[str, Any], flag: str, value: str) -> bool:
    raw_arguments = command.get("arguments")
    if not isinstance(raw_arguments, list):
        return False
    arguments = [str(item) for item in raw_arguments]
    return any(
        arguments[index] == flag and arguments[index + 1] == value
        for index in range(len(arguments) - 1)
    )


def _cached_report_matches(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if set(current) != set(expected):
        return False
    return all(key == "created_at" or current.get(key) == value for key, value in expected.items())


def _visual_evidence(paths: Sequence[Path]) -> tuple[str, dict[str, Any]]:
    missing: list[str] = []
    empty: list[str] = []
    unsupported: list[str] = []
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        if path.stat().st_size <= 0:
            empty.append(str(path))
        if path.suffix.casefold() not in _VISUAL_SUFFIXES:
            unsupported.append(str(path))
    status = "pass" if paths and not (missing or empty or unsupported) else "warning"
    return status, {
        "path_count": len(paths),
        "paths": [str(path) for path in paths],
        "missing": missing,
        "empty": empty,
        "unsupported": unsupported,
        "operator_review_required": True,
    }


def _caption_evidence(
    package_root: Path,
    layout: ProjectLayout,
    caption_path: Path,
    caption: Mapping[str, Any],
    *,
    revision_id: str,
    duration_us: int,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any]]:
    if caption.get("revision_id") != revision_id:
        raise PlanningValidationError("caption plan belongs to another revision")
    if caption.get("target_width") != width or caption.get("target_height") != height:
        raise PlanningValidationError("caption plan dimensions do not match the candidate profile")
    outputs = caption.get("outputs")
    if not isinstance(outputs, Mapping):
        raise PlanningValidationError("caption plan outputs are missing")
    output_evidence: dict[str, dict[str, Any]] = {}
    for name in ("ass", "webvtt", "text"):
        reference = outputs.get(name)
        if not isinstance(reference, Mapping):
            raise PlanningValidationError(f"caption plan {name} output is missing")
        raw_path = reference.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise PlanningValidationError(f"caption plan {name} path is missing")
        selected = _owned_path(layout, Path(raw_path), f"caption {name}")
        if not selected.is_file():
            raise PlanningValidationError(f"caption plan {name} output does not exist")
        if reference.get("sha256") != sha256_file(selected):
            raise PlanningValidationError(f"caption plan {name} output hash is stale")
        if reference.get("size_bytes") != selected.stat().st_size:
            raise PlanningValidationError(f"caption plan {name} output size is stale")
        output_evidence[name] = {
            "path": str(selected),
            "sha256": sha256_file(selected),
            "size_bytes": selected.stat().st_size,
        }
    events = caption.get("events")
    if not isinstance(events, list):
        raise PlanningValidationError("caption plan events are missing")
    timing_valid = True
    previous_end = 0
    for event in events:
        if not isinstance(event, Mapping):
            timing_valid = False
            break
        start_us = event.get("start_us")
        end_us = event.get("end_us")
        if (
            not isinstance(start_us, int)
            or not isinstance(end_us, int)
            or start_us < 0
            or end_us <= start_us
            or end_us > duration_us
            or start_us < previous_end
        ):
            timing_valid = False
            break
        previous_end = end_us
    status = "pass" if timing_valid else "fail"
    return status, {
        "path": str(caption_path),
        "sha256": sha256_file(caption_path),
        "event_count": len(events),
        "duration_us": duration_us,
        "timing_valid": timing_valid,
        "outputs": output_evidence,
        "warnings": [str(value) for value in caption.get("warnings", [])],
        "burn_in": False,
        "operator_review_required": True,
        "package_root": str(package_root),
    }


def _report_status(value: Mapping[str, Any]) -> str:
    status = str(value.get("overall_status", "warning"))
    if status == "fail":
        return "fail"
    if status == "pass" and bool(value.get("final_ready", True)):
        return "pass"
    return "warning"


def _join_warning_breakdown(joins: Mapping[str, Any]) -> dict[str, int]:
    """Summarize rendered-join diagnostics without reclassifying their status."""

    breakdown = {
        "total": 0,
        "transcript_mismatch": 0,
        "freeze_evidence": 0,
        "clipped_syllable_evidence": 0,
        "pacing_warning": 0,
        "preview_decode_failure": 0,
        "hard_failure": 0,
    }
    entries = joins.get("joins")
    if not isinstance(entries, list):
        return breakdown
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        breakdown["total"] += 1
        transcript = entry.get("transcript_check")
        if isinstance(transcript, Mapping) and any(
            transcript.get(key) for key in ("missing_words", "unexpected_words", "duplicate_words")
        ):
            breakdown["transcript_mismatch"] += 1
        audio = entry.get("audio_check")
        if isinstance(audio, Mapping) and audio.get("clipped_syllable") is True:
            breakdown["clipped_syllable_evidence"] += 1
        visual = entry.get("visual_check")
        if isinstance(visual, Mapping) and visual.get("freeze") is True:
            breakdown["freeze_evidence"] += 1
        pacing = entry.get("pacing_check")
        if isinstance(pacing, Mapping) and pacing.get("status") == "warning":
            breakdown["pacing_warning"] += 1
        preview = entry.get("preview")
        if isinstance(preview, Mapping) and preview.get("full_decode_status") not in (None, "pass"):
            breakdown["preview_decode_failure"] += 1
        if entry.get("status") == "failed" or entry.get("status") == "fail":
            breakdown["hard_failure"] += 1
    return breakdown


def _join_preview_evidence(
    layout: ProjectLayout,
    joins: Mapping[str, Any],
) -> tuple[str, dict[str, Any], list[tuple[str, Path]]]:
    """Verify every rendered join preview before source-specific QA consumes it."""

    entries = joins.get("joins")
    if not isinstance(entries, list):
        return (
            "fail",
            {
                "expected_count": 0,
                "verified_count": 0,
                "failures": [{"join_id": None, "issues": ["joins_missing"]}],
                "operator_review_required": True,
            },
            [],
        )

    summary = joins.get("summary")
    expected_count = summary.get("total_joins") if isinstance(summary, Mapping) else None
    failures: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    input_paths: list[tuple[str, Path]] = []

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            failures.append({"join_id": None, "issues": ["join_entry_not_object"]})
            continue
        join_id = str(entry.get("join_id", f"join_{index:06d}"))
        issues: list[str] = []
        preview = entry.get("preview")
        if not isinstance(preview, Mapping):
            failures.append({"join_id": join_id, "issues": ["preview_missing"]})
            continue
        preview_file = preview.get("file")
        if not isinstance(preview_file, Mapping):
            failures.append({"join_id": join_id, "issues": ["preview_file_missing"]})
            continue

        raw_path = preview_file.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            failures.append({"join_id": join_id, "issues": ["preview_path_missing"]})
            continue
        selected = _owned_path(layout, Path(raw_path), f"join preview {join_id}")
        expected_sha = preview_file.get("sha256")
        expected_size = preview_file.get("size_bytes")
        actual_sha: str | None = None
        actual_size: int | None = None
        if not selected.is_file():
            issues.append("preview_missing_on_disk")
        else:
            actual_sha = sha256_file(selected)
            actual_size = selected.stat().st_size
            if expected_sha != actual_sha:
                issues.append("preview_hash_stale")
            if expected_size != actual_size:
                issues.append("preview_size_stale")
            if actual_size <= 0:
                issues.append("preview_empty")
            input_paths.append((f"art_join_preview_{index:06d}", selected))
        if preview.get("full_decode_status") != "pass":
            issues.append("preview_full_decode_not_pass")

        fingerprint = {
            "join_id": join_id,
            "path": str(selected),
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
            "expected_size_bytes": expected_size,
            "actual_size_bytes": actual_size,
            "full_decode_status": preview.get("full_decode_status"),
        }
        if issues:
            failures.append({"join_id": join_id, "issues": issues, "fingerprint": fingerprint})
        else:
            verified.append(fingerprint)

    if not isinstance(expected_count, int) or expected_count != len(entries):
        failures.append(
            {
                "join_id": None,
                "issues": ["summary_join_count_mismatch"],
                "expected_count": expected_count,
                "actual_count": len(entries),
            }
        )
    status = "pass" if not failures else "fail"
    return (
        status,
        {
            "expected_count": expected_count,
            "preview_count": len(entries),
            "verified_count": len(verified),
            "failures": failures,
            "verified": verified,
            "operator_review_required": True,
        },
        input_paths,
    )


def qa_source_candidate(
    package_root: Path,
    layout: ProjectLayout,
    candidate_path: Path,
    *,
    source_manifest_path: Path,
    retimed_render_manifest_path: Path,
    focus_pacing_qa_path: Path,
    transcript_comparison_path: Path,
    join_qa_report_path: Path,
    segment_qa_path: Path,
    join_plan_path: Path,
    gate1_approval_path: Path,
    backup_verification_path: Path,
    visual_evidence_paths: Sequence[Path] = (),
    caption_plan_path: Path | None = None,
    profile_id: str = "profile_source_h264_qp0_pcm_f32le_2560x1440_60",
    profile: Mapping[str, Any] | None = None,
    revision_id: str = "rev_002",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Generate QA for a worker-free retimed candidate without inventing Gate 2/3 inputs."""

    candidate = _candidate_path(layout, candidate_path)
    source_path, source = _validated_input(
        package_root, layout, source_manifest_path, "source_manifest", "source manifest"
    )
    retimed_path, retimed = _validated_input(
        package_root,
        layout,
        retimed_render_manifest_path,
        "retimed_render_manifest",
        "retimed render manifest",
    )
    focus_path, focus = _validated_input(
        package_root, layout, focus_pacing_qa_path, "focus_pacing_qa", "focus pacing QA"
    )
    comparison_path, comparison = _validated_input(
        package_root,
        layout,
        transcript_comparison_path,
        "segment_transcript_comparison",
        "transcript comparison",
    )
    join_path, joins = _validated_input(
        package_root, layout, join_qa_report_path, "join_qa_report", "join QA report"
    )
    join_preview_status, join_preview_evidence, join_preview_inputs = _join_preview_evidence(
        layout, joins
    )
    segment_path, segment = _validated_input(
        package_root, layout, segment_qa_path, "segment_qa_report", "segment QA report"
    )
    join_plan_file, _join_plan = _validated_input(
        package_root, layout, join_plan_path, "join_plan", "join plan"
    )
    gate1_path, gate1 = _validated_input(
        package_root, layout, gate1_approval_path, "approval_record", "Gate 1 approval"
    )
    backup_path, backup = _validated_input(
        package_root,
        layout,
        backup_verification_path,
        "backup_verification",
        "backup verification",
    )
    selected_visual = tuple(
        _owned_path(layout, path, "visual QA evidence") for path in visual_evidence_paths
    )
    selected_caption: Path | None = None
    caption: dict[str, Any] | None = None
    if caption_plan_path is not None:
        selected_caption, caption = _validated_input(
            package_root, layout, caption_plan_path, "caption_plan", "caption plan"
        )

    source_hash = str(source["sha256"])
    source_media = _source_media_path(source)
    source_hash_pass = source_media.is_file() and sha256_file(source_media) == source_hash
    candidate_hash = sha256_file(candidate)
    output_ref = retimed.get("output")
    render_hash_match = (
        isinstance(output_ref, Mapping) and output_ref.get("sha256") == candidate_hash
    )
    expected_duration_us = int(retimed["expected_duration_us"])

    selected_adapter = adapter or FFmpegAdapter()
    adapter_version = selected_adapter.version()
    probe = selected_adapter.probe(candidate)
    video = _stream(probe, "video")
    audio = _stream(probe, "audio")
    decode = selected_adapter.full_decode_check(candidate, strict=True)

    profile_value = dict(DEFAULT_PROFILE if profile is None else profile)
    expected_fps = profile_value.get("fps")
    actual_fps: dict[str, int] | None = None
    if isinstance(video, Mapping):
        raw_rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
        if isinstance(raw_rate, str) and "/" in raw_rate:
            numerator, denominator = raw_rate.split("/", 1)
            try:
                actual_fps = {"numerator": int(numerator), "denominator": int(denominator)}
            except ValueError:
                actual_fps = None
    command = retimed.get("command")
    command_value = command if isinstance(command, Mapping) else {}
    profile_checks = {
        "video_codec": isinstance(video, Mapping)
        and video.get("codec_name") == profile_value.get("video_codec"),
        "dimensions": isinstance(video, Mapping)
        and (video.get("width"), video.get("height"))
        == (profile_value.get("width"), profile_value.get("height")),
        "frame_rate": actual_fps == expected_fps,
        "pix_fmt": isinstance(video, Mapping)
        and video.get("pix_fmt") == profile_value.get("pix_fmt"),
        "color": isinstance(video, Mapping)
        and (
            video.get("color_space"),
            video.get("color_transfer"),
            video.get("color_primaries"),
        )
        == (
            profile_value.get("color_space"),
            profile_value.get("color_transfer"),
            profile_value.get("color_primaries"),
        ),
        "audio_codec": isinstance(audio, Mapping)
        and audio.get("codec_name") == profile_value.get("audio_codec"),
        "audio_sample_fmt": isinstance(audio, Mapping)
        and audio.get("sample_fmt") == profile_value.get("audio_sample_fmt"),
        "audio_rate_channels": isinstance(audio, Mapping)
        and (audio.get("sample_rate"), audio.get("channels"))
        == (str(profile_value.get("sample_rate_hz")), profile_value.get("channels")),
        "qp_command": _command_has_pair(command_value, "-qp", str(profile_value.get("qp"))),
        "video_encoder_command": _command_has_pair(
            command_value, "-c:v", str(profile_value.get("video_encoder"))
        ),
        "audio_codec_command": _command_has_pair(
            command_value, "-c:a", str(profile_value.get("audio_codec"))
        ),
    }

    video_durations = _stream_durations(probe, "video")
    audio_durations = _stream_durations(probe, "audio")
    video_duration_us = video_durations[0] if video_durations else None
    audio_duration_us = audio_durations[0] if audio_durations else None
    av_drift_us = (
        abs(video_duration_us - audio_duration_us)
        if video_duration_us is not None and audio_duration_us is not None
        else AV_SYNC_TOLERANCE_US + 1
    )
    profile_pass = bool(video and audio and all(profile_checks.values()))
    duration_pass = (
        video_duration_us is not None
        and audio_duration_us is not None
        and abs(video_duration_us - expected_duration_us) <= AV_SYNC_TOLERANCE_US
        and abs(audio_duration_us - expected_duration_us) <= AV_SYNC_TOLERANCE_US
    )
    command_audio_preserved = bool(
        _command_has_pair(command_value, "-c:a", str(profile_value.get("audio_codec")))
        and not any(
            "norm" in str(argument).casefold()
            for argument in command_value.get("arguments", [])
            if isinstance(command_value.get("arguments"), list)
        )
    )

    findings: list[dict[str, Any]] = [
        _finding(
            "finding_source_media_decode",
            "MEDIA_DECODE",
            "pass" if decode.exit_code == 0 else "fail",
            "info" if decode.exit_code == 0 else "critical",
            "The current candidate passed typed-adapter strict full decode."
            if decode.exit_code == 0
            else "The current candidate failed typed-adapter strict full decode.",
            True,
            {"exit_code": decode.exit_code, "stderr_tail": decode.stderr[-1000:]},
        ),
        _finding(
            "finding_source_profile",
            "VIDEO_PROFILE",
            "pass" if profile_pass else "fail",
            "info" if profile_pass else "critical",
            "The current candidate matches the bound lossless picture and audio profile."
            if profile_pass
            else "The current candidate does not match the bound lossless profile.",
            True,
            {
                "checks": profile_checks,
                "video": dict(video) if isinstance(video, Mapping) else None,
                "audio": dict(audio) if isinstance(audio, Mapping) else None,
            },
        ),
        _finding(
            "finding_source_container",
            "CONTAINER_FORMAT",
            "pass"
            if "mp4" in str(probe.get("format", {}).get("format_name", "")).casefold()
            and len([item for item in probe.get("streams", []) if isinstance(item, Mapping)]) == 2
            else "fail",
            "info",
            "The candidate is an MP4 with one video and one audio stream.",
            True,
            {
                "format_name": probe.get("format", {}).get("format_name"),
                "stream_count": len(probe.get("streams", [])),
            },
        ),
        _finding(
            "finding_source_duration",
            "DURATION_SYNC",
            "pass" if duration_pass and av_drift_us <= AV_SYNC_TOLERANCE_US else "fail",
            "info" if duration_pass and av_drift_us <= AV_SYNC_TOLERANCE_US else "critical",
            "Candidate duration and A/V drift are within the bound tolerance."
            if duration_pass and av_drift_us <= AV_SYNC_TOLERANCE_US
            else "Candidate duration or A/V drift is outside the bound tolerance.",
            True,
            {
                "expected_duration_us": expected_duration_us,
                "video_duration_us": video_duration_us,
                "audio_duration_us": audio_duration_us,
                "av_drift_us": av_drift_us,
                "tolerance_us": AV_SYNC_TOLERANCE_US,
                "retimed_output_hash_match": render_hash_match,
            },
        ),
        _finding(
            "finding_source_integrity",
            "SOURCE_INTEGRITY",
            "pass" if source_hash_pass else "fail",
            "info" if source_hash_pass else "critical",
            "The immutable managed source matches the bound source-manifest hash."
            if source_hash_pass
            else "The managed source does not match the bound source-manifest hash.",
            True,
            {
                "source_path": str(source_media),
                "source_sha256": source_hash,
                "hash_match": source_hash_pass,
            },
        ),
    ]

    gate1_pass = gate1.get("decision") == "approved" and any(
        isinstance(item, Mapping) and item.get("sha256") == source_hash
        for item in gate1.get("inputs", [])
        if isinstance(gate1.get("inputs"), list)
    )
    findings.append(
        _finding(
            "finding_source_edit_approval",
            "EDIT_APPROVAL",
            "pass" if gate1_pass else "fail",
            "info" if gate1_pass else "critical",
            "Gate 1 approval is current for the supplied source hash."
            if gate1_pass
            else "Gate 1 approval is missing or not bound to the supplied source hash.",
            True,
            {
                "approval_path": str(gate1_path),
                "approval_sha256": sha256_file(gate1_path),
                "decision": gate1.get("decision"),
                "source_sha256": source_hash,
            },
        )
    )

    focus_status = _report_status(focus)
    findings.append(
        _finding(
            "finding_source_focus_pacing",
            "FOCUS_PACING",
            focus_status,
            "info" if focus_status == "pass" else "high",
            "Focus/pacing QA passes the approved prompt-writing speed-up and records no zooms."
            if focus_status == "pass"
            else "Focus/pacing QA requires review.",
            True,
            {
                "path": str(focus_path),
                "sha256": sha256_file(focus_path),
                "overall_status": focus.get("overall_status"),
                "final_ready": focus.get("final_ready"),
                "finding_count": len(focus.get("findings", [])),
            },
        )
    )

    comparison_status = "pass" if str(comparison.get("status")) == "pass" else "warning"
    findings.append(
        _finding(
            "finding_source_retranscription",
            "RETRANSCRIPTION",
            comparison_status,
            "info" if comparison_status == "pass" else "high",
            "Local Whisper re-transcription matches the intended sequence."
            if comparison_status == "pass"
            else "Local Whisper re-transcription differs; operator join review is required.",
            True,
            {
                "path": str(comparison_path),
                "sha256": sha256_file(comparison_path),
                "status": comparison.get("status"),
                "missing_count": len(comparison.get("missing_words", [])),
                "unexpected_count": len(comparison.get("unexpected_words", [])),
                "duplicate_count": len(comparison.get("duplicate_words", [])),
            },
        )
    )

    join_summary = joins.get("summary", {})
    join_failed = int(join_summary.get("failed", 0))
    join_warnings = int(join_summary.get("warnings", 0))
    join_status = "fail" if join_failed else "pass" if join_warnings == 0 else "warning"
    findings.append(
        _finding(
            "finding_source_join_review",
            "JOIN_REVIEW",
            join_status,
            "info" if join_status == "pass" else "medium",
            "All rendered joins completed with no hard failures."
            if join_status == "pass"
            else "Rendered joins contain automated or ASR warnings requiring operator review.",
            True,
            {
                "path": str(join_path),
                "sha256": sha256_file(join_path),
                "total_joins": join_summary.get("total_joins"),
                "passed": join_summary.get("passed"),
                "warnings": join_warnings,
                "failed": join_failed,
                "full_decode_fail_count": sum(
                    1
                    for item in joins.get("joins", [])
                    if isinstance(item, Mapping)
                    and item.get("preview", {}).get("full_decode_status") != "pass"
                ),
                "warning_breakdown": _join_warning_breakdown(joins),
            },
        )
    )
    findings.append(
        _finding(
            "finding_source_join_evidence",
            "JOIN_EVIDENCE",
            join_preview_status,
            "info" if join_preview_status == "pass" else "critical",
            (
                "Every rendered join preview is project-local, hash/size current, and "
                "marked full-decode pass."
            )
            if join_preview_status == "pass"
            else (
                "One or more rendered join previews are missing, stale, empty, or not "
                "full-decode pass."
            ),
            True,
            join_preview_evidence,
        )
    )

    segment_status = _report_status(segment)
    findings.append(
        _finding(
            "finding_source_segment_qa",
            "SEGMENT_QA",
            segment_status,
            "info" if segment_status == "pass" else "medium",
            "Deterministic segment QA passes with no required warnings."
            if segment_status == "pass"
            else (
                "Automated freeze, clipping, silence, ASR, duplicate, or join evidence "
                "requires review."
            ),
            True,
            {
                "path": str(segment_path),
                "sha256": sha256_file(segment_path),
                "overall_status": segment.get("overall_status"),
                "final_ready": segment.get("final_ready"),
                "summary": segment.get("summary", {}),
            },
        )
    )

    visual_status, visual_evidence = _visual_evidence(selected_visual)
    findings.append(
        _finding(
            "finding_source_visual_evidence",
            "VISUAL_EVIDENCE",
            visual_status,
            "info" if visual_status == "pass" else "medium",
            "Retained visual evidence is present; operator watch-through remains required."
            if visual_status == "pass"
            else "Visual evidence is missing or invalid.",
            True,
            visual_evidence,
        )
    )

    findings.append(
        _finding(
            "finding_source_audio_processing",
            "AUDIO_PROCESSING",
            "pass" if command_audio_preserved else "fail",
            "info" if command_audio_preserved else "critical",
            "The bound render command preserves audible PCM float audio without normalization."
            if command_audio_preserved
            else "The bound render command does not prove the requested audio profile.",
            True,
            {"command": command_value, "pitch_preservation": "audible_pitch_preserved"},
        )
    )

    if selected_caption is None or caption is None:
        caption_status = "skipped"
        caption_evidence: dict[str, Any] = {
            "caption_plan_path": None,
            "operator_action": "add captions if required",
        }
        caption_message = "Caption sidecars were not supplied for this source-specific candidate."
    else:
        caption_status, caption_evidence = _caption_evidence(
            package_root,
            layout,
            selected_caption,
            caption,
            revision_id=revision_id,
            duration_us=expected_duration_us,
            width=int(profile_value["width"]),
            height=int(profile_value["height"]),
        )
        caption_message = (
            "Word-timed caption sidecars match the candidate duration and profile."
            if caption_status == "pass"
            else "Caption sidecar timing is invalid for the candidate duration."
        )
    findings.append(
        _finding(
            "finding_source_captions",
            "CAPTIONS",
            caption_status,
            "info" if caption_status in {"pass", "skipped"} else "high",
            caption_message,
            True,
            caption_evidence,
        )
    )

    backup_items = backup.get("items", [])
    backup_pass = (
        backup.get("status") == "pass"
        and isinstance(backup_items, list)
        and all(
            isinstance(item, Mapping)
            and item.get("status") == "pass"
            and item.get("source_sha256") == item.get("backup_sha256")
            for item in backup_items
        )
    )
    findings.append(
        _finding(
            "finding_source_backup",
            "BACKUP_VERIFICATION",
            "pass" if backup_pass else "fail",
            "info" if backup_pass else "critical",
            "Source and candidate backup pairs pass hash verification."
            if backup_pass
            else "Backup verification is missing or failed.",
            True,
            {
                "path": str(backup_path),
                "sha256": sha256_file(backup_path),
                "items": len(backup_items),
            },
        )
    )

    findings.extend(
        [
            _finding(
                "finding_source_gate3",
                "GATE3_APPROVAL",
                "skipped",
                "high",
                "Gate 3 operator approval has not been issued and cannot be inferred.",
                True,
                {"candidate_sha256": candidate_hash, "approval_path": None},
            ),
            _finding(
                "finding_source_workers",
                "WORKER_SCOPE",
                "pass",
                "info",
                "SAM 3.1 and MatAnyone 2 were not invoked for this worker-free candidate.",
                False,
                {"sam3_invoked": False, "matanyone2_invoked": False},
            ),
        ]
    )

    required_failures = sum(1 for item in findings if item["required"] and item["status"] == "fail")
    warnings_count = sum(1 for item in findings if item["status"] == "warning")
    final_ready = required_failures == 0 and all(
        item["status"] == "pass" for item in findings if item["required"]
    )
    overall_status = "fail" if required_failures else "warning" if not final_ready else "pass"

    input_paths = [
        ("art_source_manifest", source_path),
        ("art_retimed_render_manifest", retimed_path),
        ("art_focus_pacing_qa", focus_path),
        ("art_transcript_comparison", comparison_path),
        ("art_join_qa_report", join_path),
        ("art_segment_qa", segment_path),
        ("art_join_plan", join_plan_file),
        ("art_gate1_approval", gate1_path),
        ("art_backup_verification", backup_path),
    ]
    input_paths.extend(
        (f"art_visual_evidence_{index:03d}", path)
        for index, path in enumerate(selected_visual, start=1)
    )
    input_paths.extend(join_preview_inputs)
    if selected_caption is not None:
        input_paths.append(("art_caption_plan", selected_caption))
    stage_key = make_stage_key(
        "source-candidate-final-qa",
        IMPLEMENTATION_VERSION,
        [sha256_file(candidate), *(sha256_file(path) for _id, path in input_paths)],
        {
            "revision_id": revision_id,
            "profile_id": profile_id,
            "profile": profile_value,
            "adapter_version": adapter_version,
            "join_preview_evidence": join_preview_evidence,
        },
    )
    report_path = layout.review / f"final-qa-source-{stage_key[:16]}.json"
    payload: dict[str, Any] = {
        "schema_name": "final_qa_report",
        "schema_version": "1.0.0",
        "artifact_id": "art_final_qa_source_candidate",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("source-candidate-final-qa", "ffmpeg+evidence", adapter_version),
        "inputs": [artifact_input(artifact_id, path) for artifact_id, path in input_paths],
        "candidate": {
            "artifact_id": "art_source_candidate",
            "path": str(candidate),
            "sha256": candidate_hash,
            "size_bytes": candidate.stat().st_size,
        },
        "profile_id": profile_id,
        "source_sha256": source_hash,
        "overall_status": overall_status,
        "final_ready": final_ready,
        "findings": findings,
        "required_failures": required_failures,
        "warnings_count": warnings_count,
    }
    with ProjectLock(layout, stage="source_candidate_final_qa", revision_id=revision_id):
        if report_path.is_file():
            current = _read_object(report_path, "source candidate final QA report")
            validate_artifact(package_root, "final_qa_report", current)
            if not _cached_report_matches(current, payload):
                raise StateConflictError(
                    "source candidate final QA report exists with stale contents"
                )
            return report_path
        write_validated_artifact(package_root, "final_qa_report", report_path, payload)
    return report_path


__all__ = ["DEFAULT_PROFILE", "qa_source_candidate"]
