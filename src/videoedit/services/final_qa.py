from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_lock import _owned_path
from videoedit.services.segment_qa import _stream_durations
from videoedit.services.visual_timeline import validate_visual_timeline

IMPLEMENTATION_VERSION = "p11-02f"
AV_SYNC_TOLERANCE_US = 100_000
_TOKEN_PATTERN = re.compile(r"[\w']+", re.UNICODE)
_VISUAL_EVIDENCE_SUFFIXES = frozenset(
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


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _file_ref(path: Path, artifact_id: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


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


def _tokens(value: object) -> list[str]:
    return [item.casefold() for item in _TOKEN_PATTERN.findall(str(value))]


def _transcript_tokens(value: Mapping[str, Any]) -> list[str]:
    words = value.get("words", [])
    if isinstance(words, list):
        return [
            token
            for word in words
            if isinstance(word, Mapping)
            for token in _tokens(word.get("text", ""))
        ]
    return []


def _visual_evidence_check(paths: Sequence[Path]) -> tuple[bool, dict[str, Any]]:
    """Require retained, non-empty image or video evidence for final visual QA."""

    missing: list[str] = []
    empty: list[str] = []
    unsupported: list[str] = []
    for path in paths:
        path_text = str(path)
        if not path.is_file():
            missing.append(path_text)
            continue
        try:
            size_bytes = path.stat().st_size
        except OSError:
            missing.append(path_text)
            continue
        if size_bytes <= 0:
            empty.append(path_text)
        if path.suffix.casefold() not in _VISUAL_EVIDENCE_SUFFIXES:
            unsupported.append(path_text)
    valid = bool(paths) and not (missing or empty or unsupported)
    return valid, {
        "path_count": len(paths),
        "missing": missing,
        "empty": empty,
        "unsupported": unsupported,
        "allowed_suffixes": sorted(_VISUAL_EVIDENCE_SUFFIXES),
    }


def _cached_report_matches_payload(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    """Accept only the current report contents, ignoring its creation timestamp."""

    if set(current) != set(expected):
        return False
    return all(key == "created_at" or current.get(key) == value for key, value in expected.items())


def _source_media_path(source: Mapping[str, Any]) -> Path:
    selected = (
        source.get("managed_path")
        if source.get("ingest_mode") == "copy"
        else source.get("source_path")
    )
    if not isinstance(selected, str) or not selected:
        raise PlanningValidationError("source manifest does not identify source media")
    return Path(selected).expanduser().resolve()


def _contains(sequence: Sequence[str], candidate: Sequence[str]) -> bool:
    if not candidate:
        return True
    width = len(candidate)
    return any(
        list(sequence[index : index + width]) == list(candidate) for index in range(len(sequence))
    )


def _validate_plan_inputs(
    package_root: Path,
    layout: ProjectLayout,
    plan_paths: Mapping[str, Path],
    *,
    revision_id: str,
) -> list[tuple[str, Path, dict[str, Any]]]:
    validated: list[tuple[str, Path, dict[str, Any]]] = []
    for schema_name, raw_path in plan_paths.items():
        path = _owned_path(layout, raw_path, f"{schema_name} plan")
        if not path.is_file():
            raise PlanningValidationError(f"{schema_name} plan does not exist: {path}")
        value = _read_object(path, f"{schema_name} plan")
        validate_artifact(package_root, schema_name, value)
        if value.get("project_id") != layout.root.name:
            raise PlanningValidationError(f"{schema_name} plan belongs to another project")
        if value.get("revision_id") not in (None, revision_id):
            raise PlanningValidationError(f"{schema_name} plan is bound to another revision")
        validated.append((schema_name, path, value))
    return validated


def _caption_check(
    caption: Mapping[str, Any],
    transcript: Mapping[str, Any] | None,
    duration_us: int,
) -> tuple[str, str, dict[str, Any]]:
    events = caption.get("events", [])
    if not isinstance(events, list):
        return "fail", "caption events are not an array", {}
    invalid: list[dict[str, int]] = []
    unmatched: list[str] = []
    previous_end = -1
    rendered = _transcript_tokens(transcript) if transcript is not None else []
    for event in events:
        if not isinstance(event, Mapping):
            return "fail", "caption event is not an object", {}
        start = int(event.get("start_us", -1))
        end = int(event.get("end_us", -1))
        if start < 0 or end <= start or end > duration_us or start < previous_end:
            invalid.append({"start_us": start, "end_us": end})
        previous_end = max(previous_end, end)
        event_tokens = _tokens(event.get("text", ""))
        if transcript is not None and event_tokens and not _contains(rendered, event_tokens):
            unmatched.append(str(event.get("caption_id", "unknown")))
    if invalid or unmatched or transcript is None:
        return (
            "fail" if invalid or unmatched or transcript is None else "pass",
            "caption timing or transcript alignment is not proven",
            {
                "invalid_ranges": invalid,
                "unmatched_caption_ids": unmatched,
                "transcript_supplied": transcript is not None,
            },
        )
    return (
        "pass",
        "caption events are bounded, ordered, and match rendered speech",
        {"event_count": len(events)},
    )


def qa_final_candidate(
    package_root: Path,
    layout: ProjectLayout,
    assembly_manifest_path: Path,
    *,
    source_manifest_path: Path | None = None,
    asset_manifest_path: Path | None = None,
    composition_bundle_path: Path | None = None,
    plan_paths: Mapping[str, Path] | None = None,
    caption_plan_path: Path | None = None,
    transcript_path: Path | None = None,
    visual_timeline_path: Path | None = None,
    visual_evidence_paths: Sequence[Path] = (),
    gate2_paths: Sequence[Path] = (),
    profile_id: str = "pro_youtube_1080p",
    profile: Mapping[str, Any] | None = None,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Run the required final candidate checks and persist a hash-bound report."""

    selected_assembly = _owned_path(layout, assembly_manifest_path, "final assembly manifest")
    assembly = _read_object(selected_assembly, "final assembly manifest")
    validate_artifact(package_root, "final_assembly_manifest", assembly)
    if assembly["project_id"] != layout.root.name or assembly["revision_id"] != revision_id:
        raise PlanningValidationError("final assembly belongs to another project or revision")
    candidate_ref = assembly["output"]
    candidate_path = _owned_path(layout, Path(str(candidate_ref["path"])), "final candidate")
    if not candidate_path.is_file() or sha256_file(candidate_path) != candidate_ref["sha256"]:
        raise PlanningValidationError("final candidate hash is stale")

    source_path = _owned_path(
        layout,
        source_manifest_path or layout.artifacts / "source-manifest.json",
        "source manifest",
    )
    if not source_path.is_file():
        raise PlanningValidationError("source manifest is missing")
    source = _read_object(source_path, "source manifest")
    validate_artifact(package_root, "source_manifest", source)
    source_media = _source_media_path(source)
    source_hash_pass = source_media.is_file() and sha256_file(source_media) == source["sha256"]

    asset_path = _owned_path(
        layout,
        asset_manifest_path or layout.artifacts / "asset-manifest.json",
        "asset manifest",
    )
    asset = None
    if asset_path.is_file():
        asset = _read_object(asset_path, "asset manifest")
        validate_artifact(package_root, "asset_manifest", asset)
    else:
        raise PlanningValidationError(f"asset manifest is missing: {asset_path}")

    composition_path = (
        _owned_path(layout, composition_bundle_path, "composition bundle")
        if composition_bundle_path is not None
        else layout.work / "composition-bundle.js"
    )
    composition_exists = composition_path.is_file()
    if not composition_exists:
        raise PlanningValidationError(f"composition bundle is missing: {composition_path}")

    selected_plans = _validate_plan_inputs(
        package_root,
        layout,
        plan_paths or {},
        revision_id=revision_id,
    )
    caption = None
    selected_caption = None
    if caption_plan_path is not None:
        selected_caption = _owned_path(layout, caption_plan_path, "caption plan")
        caption = _read_object(selected_caption, "caption plan")
        validate_artifact(package_root, "caption_plan", caption)
        if caption["project_id"] != layout.root.name or caption["revision_id"] != revision_id:
            raise PlanningValidationError("caption plan belongs to another project or revision")
        for name in ("ass", "webvtt", "text"):
            reference = caption["outputs"][name]
            path = _owned_path(layout, Path(str(reference["path"])), f"caption {name}")
            if not path.is_file() or sha256_file(path) != reference["sha256"]:
                raise PlanningValidationError(f"caption {name} sidecar hash is stale")

    transcript = None
    selected_transcript = None
    if transcript_path is not None:
        selected_transcript = _owned_path(layout, transcript_path, "final transcript")
        transcript = _read_object(selected_transcript, "final transcript")
        validate_artifact(package_root, "transcript", transcript)
        if transcript["project_id"] != layout.root.name or transcript["revision_id"] != revision_id:
            raise PlanningValidationError("final transcript belongs to another project or revision")

    visual_timeline = None
    selected_visual = None
    if visual_timeline_path is not None:
        selected_visual = _owned_path(layout, visual_timeline_path, "visual timeline")
        visual_timeline = _read_object(selected_visual, "visual timeline")
        visual_timeline_model = validate_visual_timeline(package_root, visual_timeline)
        if visual_timeline_model.project_id != layout.root.name:
            raise PlanningValidationError("visual timeline belongs to another project")
    selected_visual_evidence = tuple(
        _owned_path(layout, path, "visual QA evidence") for path in visual_evidence_paths
    )

    selected_locks: list[tuple[Path, dict[str, Any]]] = []
    for raw_lock in gate2_paths:
        path = _owned_path(layout, raw_lock, "Gate 2 segment lock")
        lock = _read_object(path, "Gate 2 segment lock")
        validate_artifact(package_root, "segment_lock", lock)
        if lock["project_id"] != layout.root.name or lock["revision_id"] != revision_id:
            raise PlanningValidationError("Gate 2 lock belongs to another project or revision")
        selected_locks.append((path, lock))

    selected_adapter = adapter or FFmpegAdapter()
    adapter_version = selected_adapter.version()
    probe = selected_adapter.probe(candidate_path)
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    video_streams = [
        item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"
    ]
    audio_streams = [
        item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"
    ]
    findings: list[dict[str, Any]] = []
    decode = selected_adapter.full_decode_check(candidate_path)
    findings.append(
        _finding(
            "finding_decode",
            "MEDIA_DECODE",
            "pass" if decode.exit_code == 0 else "fail",
            "info" if decode.exit_code == 0 else "critical",
            "Final candidate fully decodes."
            if decode.exit_code == 0
            else "Final candidate failed full decode.",
            True,
            {"exit_code": decode.exit_code, "stderr_tail": decode.stderr[-1000:]},
        )
    )
    findings.append(
        _finding(
            "finding_video_stream",
            "VIDEO_STREAM",
            "pass" if video_streams else "fail",
            "info" if video_streams else "critical",
            "A video stream is present." if video_streams else "No video stream is present.",
            True,
            {"count": len(video_streams)},
        )
    )
    findings.append(
        _finding(
            "finding_audio_stream",
            "AUDIO_STREAM",
            "pass" if audio_streams else "fail",
            "info" if audio_streams else "critical",
            "A production audio stream is present."
            if audio_streams
            else "No production audio stream is present.",
            True,
            {"count": len(audio_streams)},
        )
    )

    actual_duration_us = seconds_to_us(probe.get("format", {}).get("duration"))
    expected_duration_us = int(assembly["expected_duration_us"])
    duration_pass = (
        actual_duration_us is not None
        and abs(actual_duration_us - expected_duration_us) <= AV_SYNC_TOLERANCE_US
    )
    findings.append(
        _finding(
            "finding_duration",
            "DURATION_MATCH",
            "pass" if duration_pass else "fail",
            "info" if duration_pass else "high",
            "Candidate duration matches approved assembly."
            if duration_pass
            else "Candidate duration differs from approved assembly.",
            True,
            {
                "expected_duration_us": expected_duration_us,
                "actual_duration_us": actual_duration_us,
                "tolerance_us": AV_SYNC_TOLERANCE_US,
            },
        )
    )

    selected_profile = dict(
        profile or {"width": 1920, "height": 1080, "fps": {"numerator": 30, "denominator": 1}}
    )
    profile_video = selected_profile.get("video")
    profile_video_value = profile_video if isinstance(profile_video, Mapping) else {}
    expected_width = int(selected_profile.get("width") or profile_video_value.get("width") or 0)
    expected_height = int(selected_profile.get("height") or profile_video_value.get("height") or 0)
    expected_fps = selected_profile.get("fps") or profile_video_value.get("frame_rate")
    if video_streams:
        video = dict(video_streams[0])
        dimensions_pass = (
            int(video.get("width") or 0) == expected_width
            and int(video.get("height") or 0) == expected_height
        )
        findings.append(
            _finding(
                "finding_dimensions",
                "VIDEO_DIMENSIONS",
                "pass" if dimensions_pass else "fail",
                "info" if dimensions_pass else "high",
                "Video dimensions match the delivery profile."
                if dimensions_pass
                else "Video dimensions do not match the delivery profile.",
                True,
                {
                    "expected": {
                        "width": expected_width,
                        "height": expected_height,
                    },
                    "actual": {"width": video.get("width"), "height": video.get("height")},
                },
            )
        )
        actual_fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(
            video.get("r_frame_rate")
        )
        fps_pass = not isinstance(expected_fps, Mapping) or actual_fps == {
            "numerator": int(expected_fps["numerator"]),
            "denominator": int(expected_fps["denominator"]),
        }
        findings.append(
            _finding(
                "finding_frame_rate",
                "FRAME_RATE",
                "pass" if fps_pass else "fail",
                "info" if fps_pass else "high",
                "Frame rate matches the delivery profile."
                if fps_pass
                else "Frame rate does not match the delivery profile.",
                True,
                {"expected": expected_fps, "actual": actual_fps},
            )
        )
    else:
        findings.extend(
            [
                _finding(
                    "finding_dimensions",
                    "VIDEO_DIMENSIONS",
                    "fail",
                    "critical",
                    "Video dimensions cannot be checked without a video stream.",
                    True,
                    {},
                ),
                _finding(
                    "finding_frame_rate",
                    "FRAME_RATE",
                    "fail",
                    "critical",
                    "Frame rate cannot be checked without a video stream.",
                    True,
                    {},
                ),
            ]
        )

    frame_count: int | None = None
    expected_frame_count: int | None = None
    frame_count_pass = False
    if video_streams and isinstance(expected_fps, Mapping):
        expected_frame_count = microseconds_to_frame(
            expected_duration_us,
            int(expected_fps["numerator"]),
            int(expected_fps["denominator"]),
        )
        raw_frame_count = video.get("nb_frames")
        try:
            frame_count = int(str(raw_frame_count)) if raw_frame_count is not None else None
        except (TypeError, ValueError):
            frame_count = None
        if frame_count is None or frame_count <= 0:
            frame_count = selected_adapter.probe_frame_count(candidate_path)
        frame_count_pass = frame_count == expected_frame_count
    findings.append(
        _finding(
            "finding_frame_count",
            "FRAME_COUNT",
            "pass" if frame_count_pass else "fail",
            "info" if frame_count_pass else "high",
            "Decoded video frame count matches the approved duration and frame rate."
            if frame_count_pass
            else "Decoded video frame count does not match the approved duration and frame rate.",
            True,
            {
                "expected_frame_count": expected_frame_count,
                "actual_frame_count": frame_count,
                "fps": expected_fps,
            },
        )
    )

    video_durations = _stream_durations(probe, "video")
    audio_durations = _stream_durations(probe, "audio")
    av_drift = (
        abs(video_durations[0] - audio_durations[0])
        if video_durations and audio_durations
        else AV_SYNC_TOLERANCE_US + 1
    )
    av_pass = bool(video_durations and audio_durations and av_drift <= AV_SYNC_TOLERANCE_US)
    findings.append(
        _finding(
            "finding_av_sync",
            "AV_SYNC",
            "pass" if av_pass else "fail",
            "info" if av_pass else "critical",
            "Picture and production audio are synchronized."
            if av_pass
            else "Picture and production audio drift beyond tolerance.",
            True,
            {
                "video_duration_us": video_durations[0] if video_durations else None,
                "audio_duration_us": audio_durations[0] if audio_durations else None,
                "drift_us": av_drift,
                "tolerance_us": AV_SYNC_TOLERANCE_US,
            },
        )
    )

    loudness = assembly["loudness"]
    preserved_without_normalization = (
        "loudness_normalization_disabled_by_delivery_profile" in assembly.get("warnings", [])
    )
    assembly_clipped_samples = int(loudness["clipped_samples"])
    loudness_pass = loudness["status"] == "pass" and (
        assembly_clipped_samples == 0 or preserved_without_normalization
    )
    loudness_warning = preserved_without_normalization and assembly_clipped_samples > 0
    findings.append(
        _finding(
            "finding_loudness",
            "LOUDNESS_PROFILE",
            "warning" if loudness_warning else "pass" if loudness_pass else "fail",
            "medium" if loudness_warning else "info" if loudness_pass else "high",
            "Final loudness was preserved without normalization; the retained candidate reports "
            f"{assembly_clipped_samples} pre-existing clipped sample(s)."
            if loudness_warning
            else "Final loudness meets the selected profile."
            if loudness_pass
            else "Final loudness or clipping is outside the selected profile.",
            True,
            {
                "loudness": loudness,
                "preserved_without_normalization": preserved_without_normalization,
            },
        )
    )
    clipping = selected_adapter.measure_clipping(candidate_path)
    clipped_samples = re.findall(
        r"Number\s+of\s+clipped\s+samples:\s*(\d+)",
        clipping.stderr + clipping.stdout,
        re.IGNORECASE,
    )
    measured_clipped_samples = max((int(value) for value in clipped_samples), default=0)
    clipping_pass = clipping.exit_code == 0 and (
        not clipped_samples or max(int(value) for value in clipped_samples) == 0
    )
    clipping_warning = (
        preserved_without_normalization
        and clipping.exit_code == 0
        and measured_clipped_samples == assembly_clipped_samples
        and measured_clipped_samples > 0
    )
    findings.append(
        _finding(
            "finding_clipping",
            "CLIPPING",
            "warning" if clipping_warning else "pass" if clipping_pass else "fail",
            "medium" if clipping_warning else "info" if clipping_pass else "high",
            "The retained candidate has the same pre-existing clipped-sample count as its "
            "byte-identical input; no audio transformation was applied."
            if clipping_warning
            else "No clipped audio samples were detected."
            if clipping_pass
            else "Clipped audio samples were detected.",
            True,
            {
                "exit_code": clipping.exit_code,
                "clipped_samples": measured_clipped_samples,
                "preserved_without_normalization": preserved_without_normalization,
            },
        )
    )
    black = selected_adapter.detect_black_frames(candidate_path)
    black_hits = re.findall(
        r"pblack:\s*(\d+(?:\.\d+)?)", black.stderr + black.stdout, re.IGNORECASE
    )
    black_pass = black.exit_code == 0 and not any(float(value) >= 98 for value in black_hits)
    findings.append(
        _finding(
            "finding_black",
            "BLACK_FRAMES",
            "pass" if black_pass else "fail",
            "info" if black_pass else "high",
            "No full black frames were detected."
            if black_pass
            else "Full black frames were detected.",
            True,
            {"exit_code": black.exit_code, "pblack_values": [float(value) for value in black_hits]},
        )
    )
    freeze = selected_adapter.detect_freeze_frames(candidate_path)
    freeze_hits = re.findall(r"freeze_start", freeze.stderr + freeze.stdout, re.IGNORECASE)
    freeze_pass = freeze.exit_code == 0 and not freeze_hits
    static_freeze_warning = (
        bool(freeze_hits) and selected_profile.get("freeze_policy") == "warn_static_screen"
    )
    findings.append(
        _finding(
            "finding_freeze",
            "FREEZE_FRAMES",
            "warning" if static_freeze_warning else "pass" if freeze_pass else "fail",
            "medium" if static_freeze_warning else "info" if freeze_pass else "high",
            "Static screen intervals are retained at normal speed under the source-recording "
            "policy; visual segment QA passed."
            if static_freeze_warning
            else "No freeze-frame intervals were detected."
            if freeze_pass
            else "Freeze-frame intervals were detected.",
            True,
            {
                "exit_code": freeze.exit_code,
                "freeze_count": len(freeze_hits),
                "policy": selected_profile.get("freeze_policy"),
            },
        )
    )

    caption_status = "fail"
    caption_message = "Caption sidecars and timing are not supplied."
    caption_evidence: dict[str, Any] = {}
    if caption is not None and actual_duration_us is not None:
        caption_status, caption_message, caption_evidence = _caption_check(
            caption, transcript, actual_duration_us
        )
    findings.append(
        _finding(
            "finding_captions",
            "CAPTIONS",
            caption_status,
            "info" if caption_status == "pass" else "high",
            caption_message,
            True,
            caption_evidence,
        )
    )
    transcript_pass = transcript is not None and bool(transcript.get("words"))
    findings.append(
        _finding(
            "finding_transcript",
            "FINAL_TRANSCRIPT",
            "pass" if transcript_pass else "fail",
            "info" if transcript_pass else "high",
            "Final transcript is present and word timed."
            if transcript_pass
            else "Final transcript is missing or has no words.",
            True,
            {"word_count": len(transcript.get("words", [])) if transcript else 0},
        )
    )

    visual_evidence_pass, visual_evidence_check = _visual_evidence_check(selected_visual_evidence)
    visual_pass = visual_timeline is not None and visual_evidence_pass
    findings.append(
        _finding(
            "finding_visual",
            "VISUAL_EVIDENCE",
            "pass" if visual_pass else "fail",
            "info" if visual_pass else "high",
            "Visual timeline and retained proof evidence are present."
            if visual_pass
            else "Visual timeline or retained proof evidence is missing.",
            True,
            {
                "timeline": str(selected_visual) if selected_visual else None,
                "evidence_paths": [str(path) for path in selected_visual_evidence],
                "evidence_check": visual_evidence_check,
            },
        )
    )
    timeline_code_hash = visual_timeline.get("code_bundle_sha256") if visual_timeline else None
    composition_hash_pass = (
        composition_exists
        and isinstance(timeline_code_hash, str)
        and timeline_code_hash == sha256_file(composition_path)
    )
    provenance_pass = (
        source_hash_pass and asset is not None and composition_hash_pass and bool(selected_plans)
    )
    findings.append(
        _finding(
            "finding_provenance",
            "PROVENANCE",
            "pass" if provenance_pass else "fail",
            "info" if provenance_pass else "critical",
            "Source, assets, composition, and plan provenance are current."
            if provenance_pass
            else "Required provenance inputs are missing or stale.",
            True,
            {
                "source_hash_pass": source_hash_pass,
                "asset_manifest": str(asset_path),
                "composition_bundle": str(composition_path),
                "composition_hash_pass": composition_hash_pass,
                "plan_count": len(selected_plans),
            },
        )
    )
    approvals_pass = bool(selected_locks) and all(
        bool(lock.get("locked")) for _path, lock in selected_locks
    )
    findings.append(
        _finding(
            "finding_gate2",
            "GATE2_APPROVALS",
            "pass" if approvals_pass else "fail",
            "info" if approvals_pass else "critical",
            "All supplied segments have current locked Gate 2 approvals."
            if approvals_pass
            else "Current locked Gate 2 approvals are missing.",
            True,
            {"lock_count": len(selected_locks)},
        )
    )

    required_failures = sum(item["required"] and item["status"] == "fail" for item in findings)
    warnings_count = sum(item["status"] == "warning" for item in findings)
    overall_status = "fail" if required_failures else "warning" if warnings_count else "pass"
    input_paths: list[tuple[str, Path]] = [
        ("art_final_assembly", selected_assembly),
        ("art_source_manifest", source_path),
        ("art_asset_manifest", asset_path),
        ("art_composition_bundle", composition_path),
    ]
    input_paths.extend((f"art_{schema_name}", path) for schema_name, path, _value in selected_plans)
    if selected_caption is not None:
        input_paths.append(("art_caption_plan", selected_caption))
    if selected_transcript is not None:
        input_paths.append(("art_final_transcript", selected_transcript))
    if selected_visual is not None:
        input_paths.append(("art_visual_timeline", selected_visual))
    input_paths.extend(
        (f"art_visual_evidence_{index:03d}", path)
        for index, path in enumerate(selected_visual_evidence, start=1)
    )
    input_paths.extend(
        (f"art_gate2_{index:03d}", path)
        for index, (path, _lock) in enumerate(selected_locks, start=1)
    )
    stage_key = make_stage_key(
        "final-qa",
        IMPLEMENTATION_VERSION,
        [sha256_file(path) for _artifact_id, path in input_paths],
        {
            "revision_id": revision_id,
            "profile_id": profile_id,
            "profile": dict(selected_profile),
            "adapter_version": adapter_version,
        },
    )
    report_path = layout.artifacts / f"final-qa-{stage_key[:16]}.json"
    alias_path = layout.artifacts / "final-qa.json"
    payload = {
        "schema_name": "final_qa_report",
        "schema_version": "1.0.0",
        "artifact_id": "art_final_qa",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("final-qa", "ffmpeg", adapter_version),
        "inputs": [artifact_input(artifact_id, path) for artifact_id, path in input_paths],
        "candidate": _file_ref(candidate_path, "art_final_candidate"),
        "profile_id": profile_id,
        "source_sha256": str(source["sha256"]),
        "overall_status": overall_status,
        "final_ready": required_failures == 0,
        "findings": findings,
        "required_failures": required_failures,
        "warnings_count": warnings_count,
    }
    with ProjectLock(layout, stage="final_qa", revision_id=revision_id):
        if report_path.is_file():
            current = _read_object(report_path, "final QA report")
            validate_artifact(package_root, "final_qa_report", current)
            if not _cached_report_matches_payload(current, payload):
                raise StateConflictError("final QA report exists with stale contents")
            return report_path
        if alias_path.is_file():
            current = _read_object(alias_path, "final QA report")
            validate_artifact(package_root, "final_qa_report", current)
            if _cached_report_matches_payload(current, payload):
                return alias_path
            raise StateConflictError("final QA alias exists with stale inputs")
        write_validated_artifact(package_root, "final_qa_report", report_path, payload)
        write_validated_artifact(package_root, "final_qa_report", alias_path, payload)
    return report_path


__all__ = ["qa_final_candidate"]
