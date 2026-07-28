from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import ProcessResult
from videoedit.errors import PlanningValidationError, StateConflictError, VideoeditError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.rendering import parse_clipped_samples
from videoedit.services.silence import parse_silencedetect_detailed

IMPLEMENTATION_VERSION = "p10-06e"
DURATION_TOLERANCE_US = 100_000
_BLACK_FRAME_PATTERN = re.compile(r"pblack\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_FREEZE_PATTERN = re.compile(r"freeze_start\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _file_ref(artifact_id: str, path: Path) -> dict[str, Any]:
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
    evidence: dict[str, Any],
    required: bool,
    repair_hint: str | None = None,
    *,
    time_range: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "check_code": check_code,
        "status": status,
        "severity": severity,
        "message": message,
        "time_range": time_range,
        "evidence": evidence,
        "required": required,
        "repair_hint": repair_hint,
    }


def _process_evidence(result: ProcessResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "stderr_tail": result.stderr[-1000:],
        "stdout_tail": result.stdout[-1000:],
    }


def _probe_duration(probe: Mapping[str, Any]) -> int | None:
    format_value = probe.get("format")
    if isinstance(format_value, Mapping):
        duration_us = seconds_to_us(format_value.get("duration"))
        if duration_us is not None:
            return duration_us
    return None


def _stream_durations(probe: Mapping[str, Any], stream_type: str) -> list[int]:
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        return []
    durations: list[int] = []
    for stream in streams:
        if not isinstance(stream, Mapping) or stream.get("codec_type") != stream_type:
            continue
        duration_us = seconds_to_us(stream.get("duration"))
        if duration_us is not None:
            durations.append(duration_us)
    return durations


def _tokens(text: object) -> list[str]:
    return _TOKEN_PATTERN.findall(str(text).lower())


def _contiguous(sequence: Sequence[str], candidate: Sequence[str]) -> bool:
    if not candidate:
        return True
    width = len(candidate)
    return any(
        list(sequence[index : index + width]) == list(candidate) for index in range(len(sequence))
    )


def _caption_drift(
    caption_plan: Mapping[str, Any],
    output_duration_us: int,
    rendered_sequence: Sequence[str],
) -> tuple[str, str, dict[str, Any]]:
    events = caption_plan.get("events", [])
    if not isinstance(events, list):
        return "fail", "caption events are not an array", {}
    invalid_ranges: list[dict[str, int]] = []
    unmatched: list[str] = []
    for event in events:
        if not isinstance(event, Mapping):
            return "fail", "caption event is not an object", {}
        start_us = int(str(event.get("start_us", -1)))
        end_us = int(str(event.get("end_us", -1)))
        if start_us < 0 or end_us <= start_us or end_us > output_duration_us:
            invalid_ranges.append({"start_us": start_us, "end_us": end_us})
        event_tokens = _tokens(event.get("text", ""))
        if event_tokens and not _contiguous(rendered_sequence, event_tokens):
            unmatched.append(str(event.get("caption_id", "<unknown>")))
    if invalid_ranges or unmatched:
        return (
            "fail",
            "caption timing or text does not match the rendered speech",
            {"invalid_ranges": invalid_ranges, "unmatched_caption_ids": unmatched},
        )
    return (
        "pass",
        "caption events remain within the rendered speech timeline",
        {"event_count": len(events)},
    )


def _cached_report(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    report_path: Path,
    ffmpeg_version: str,
    media_manifest_path: Path,
    output_path: Path,
    comparison_path: Path | None,
    caption_path: Path | None,
    join_path: Path | None,
) -> Path | None:
    if not report_path.is_file():
        return None
    report = _read_object(report_path, "segment QA report")
    validate_artifact(package_root, "segment_qa_report", report)
    if report.get("project_id") != layout.root.name or report.get("revision_id") != revision_id:
        return None
    report_producer = report.get("producer")
    if not isinstance(report_producer, Mapping):
        return None
    if (
        report_producer.get("adapter") != "ffmpeg"
        or report_producer.get("adapter_version") != ffmpeg_version
    ):
        return None
    references: list[tuple[str, Path | None]] = [
        ("media", output_path),
        ("comparison", comparison_path),
        ("caption_plan", caption_path),
    ]
    for name, path in references:
        reference = report[name]
        if path is None:
            if reference is not None:
                return None
            continue
        if not path.is_file() or not isinstance(reference, Mapping):
            return None
        if (
            reference.get("path") != str(path.resolve())
            or reference.get("sha256") != sha256_file(path)
            or reference.get("size_bytes") != path.stat().st_size
        ):
            return None
    input_paths = {
        "revision_media_manifest": media_manifest_path,
        "transcript_comparison": comparison_path,
        "caption_plan": caption_path,
        "join_qa_report": join_path,
    }
    inputs = report.get("inputs")
    if not isinstance(inputs, list):
        return None
    input_ids = [
        str(item.get("artifact_id"))
        for item in inputs
        if isinstance(item, Mapping) and item.get("artifact_id") is not None
    ]
    expected_ids = {name for name, path in input_paths.items() if path is not None}
    if len(input_ids) != len(set(input_ids)) or set(input_ids) != expected_ids:
        return None
    for artifact_id, path in input_paths.items():
        if path is None:
            continue
        if not path.is_file():
            return None
        matches = [
            item
            for item in inputs
            if isinstance(item, Mapping) and item.get("artifact_id") == artifact_id
        ]
        if len(matches) != 1 or matches[0].get("sha256") != sha256_file(path):
            return None
    return report_path


def _validate_input_artifact(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    schema_name: str,
    description: str,
) -> dict[str, Any]:
    value = _read_object(path, description)
    validate_artifact(package_root, schema_name, value)
    if value.get("project_id") != layout.root.name:
        raise PlanningValidationError(f"{description} belongs to another project")
    return value


def qa_segment_revision(
    package_root: Path,
    layout: ProjectLayout,
    revision_media_path: Path,
    *,
    comparison_path: Path | None = None,
    caption_plan_path: Path | None = None,
    join_report_path: Path | None = None,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Run deterministic media, speech, caption, and boundary QA for a revision."""

    selected_media_path = _owned_path(layout, revision_media_path, "revision media manifest")
    media_manifest = _validate_input_artifact(
        package_root,
        layout,
        selected_media_path,
        "revision_media_manifest",
        "revision media manifest",
    )
    revision_id = str(media_manifest["revision_id"])
    selected_comparison = (
        _owned_path(layout, comparison_path, "transcript comparison")
        if comparison_path is not None
        else None
    )
    selected_caption = (
        _owned_path(layout, caption_plan_path, "caption plan") if caption_plan_path else None
    )
    selected_join = (
        _owned_path(layout, join_report_path, "join QA report") if join_report_path else None
    )
    comparison = (
        _validate_input_artifact(
            package_root,
            layout,
            selected_comparison,
            "segment_transcript_comparison",
            "transcript comparison",
        )
        if selected_comparison is not None and selected_comparison.is_file()
        else None
    )
    caption_plan = (
        _validate_input_artifact(
            package_root, layout, selected_caption, "caption_plan", "caption plan"
        )
        if selected_caption is not None and selected_caption.is_file()
        else None
    )
    if selected_join is not None and not selected_join.is_file():
        raise PlanningValidationError(f"join QA report does not exist: {selected_join}")
    if selected_join is not None:
        join_report = _validate_input_artifact(
            package_root, layout, selected_join, "join_qa_report", "join QA report"
        )
    else:
        join_report = None
    output_path = _owned_path(
        layout, Path(str(media_manifest["output"]["path"])), "revision output"
    )
    if not output_path.is_file() or sha256_file(output_path) != media_manifest["output"]["sha256"]:
        raise PlanningValidationError("revision output hash is stale")
    selected_adapter = adapter or FFmpegAdapter()
    ffmpeg_version = selected_adapter.version()
    report_path = layout.revision_root(revision_id) / "segment-qa.json"

    with ProjectLock(layout, stage="segment_qa", revision_id=revision_id):
        if _cached_report(
            package_root,
            layout,
            revision_id,
            report_path,
            ffmpeg_version,
            selected_media_path,
            output_path,
            selected_comparison,
            selected_caption,
            selected_join,
        ):
            return report_path
        if report_path.exists():
            raise StateConflictError("segment QA report exists but is stale")
        input_hashes = [sha256_file(selected_media_path)]
        if selected_comparison is not None:
            input_hashes.append(sha256_file(selected_comparison))
        if selected_caption is not None:
            input_hashes.append(sha256_file(selected_caption))
        if selected_join is not None:
            input_hashes.append(sha256_file(selected_join))
        stage_key = make_stage_key(
            "segment-qa",
            IMPLEMENTATION_VERSION,
            input_hashes,
            {
                "revision_id": revision_id,
                "duration_tolerance_us": DURATION_TOLERANCE_US,
                "adapter": "ffmpeg",
                "adapter_version": ffmpeg_version,
            },
        )
        staging_root = layout.staging / "segment-qa" / f"{revision_id}-{stage_key[:16]}"
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)

        output_duration_us = int(media_manifest["output_duration_us"])
        source_duration_us = int(media_manifest["source_duration_us"])
        findings: list[dict[str, Any]] = []
        probe = selected_adapter.probe(output_path)
        streams = probe.get("streams", [])
        if not isinstance(streams, list):
            streams = []
        video_streams = [
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == "video"
        ]
        audio_streams = [
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == "audio"
        ]

        decode = selected_adapter.full_decode_check(output_path)
        findings.append(
            _finding(
                "finding_media_decode",
                "MEDIA_DECODE",
                "pass" if decode.exit_code == 0 else "fail",
                "info" if decode.exit_code == 0 else "critical",
                "The complete revision output decoded successfully"
                if decode.exit_code == 0
                else "The revision output failed full decode",
                _process_evidence(decode),
                True,
                None if decode.exit_code == 0 else "Repair the render before Gate 2",
            )
        )
        findings.append(
            _finding(
                "finding_video_stream",
                "VIDEO_STREAM",
                "pass" if video_streams else "fail",
                "info" if video_streams else "critical",
                "A video stream is present" if video_streams else "The output has no video stream",
                {"count": len(video_streams)},
                True,
                "Render a video stream before review" if not video_streams else None,
            )
        )
        findings.append(
            _finding(
                "finding_audio_stream",
                "AUDIO_STREAM",
                "pass" if audio_streams else "fail",
                "info" if audio_streams else "critical",
                "An audio stream is present" if audio_streams else "The output has no audio stream",
                {"count": len(audio_streams)},
                True,
                "Map program audio before review" if not audio_streams else None,
            )
        )
        container_duration_us = _probe_duration(probe)
        duration_ok = (
            container_duration_us is not None
            and abs(container_duration_us - output_duration_us) <= DURATION_TOLERANCE_US
        )
        findings.append(
            _finding(
                "finding_duration",
                "DURATION_MATCH",
                "pass" if duration_ok else "fail",
                "info" if duration_ok else "high",
                "Output duration matches the revision manifest"
                if duration_ok
                else "Output duration differs from the revision manifest",
                {
                    "manifest_duration_us": output_duration_us,
                    "container_duration_us": container_duration_us,
                    "tolerance_us": DURATION_TOLERANCE_US,
                },
                True,
            )
        )
        stream_durations = [
            *_stream_durations(probe, "video"),
            *_stream_durations(probe, "audio"),
        ]
        av_sync_ok = (
            bool(stream_durations)
            and container_duration_us is not None
            and all(
                abs(duration - container_duration_us) <= DURATION_TOLERANCE_US
                for duration in stream_durations
            )
        )
        findings.append(
            _finding(
                "finding_av_sync",
                "AV_SYNC",
                "pass" if av_sync_ok else "fail",
                "info" if av_sync_ok else "high",
                "Audio and video durations remain synchronized"
                if av_sync_ok
                else "Audio/video duration evidence is missing or drifts beyond tolerance",
                {"stream_durations_us": stream_durations, "tolerance_us": DURATION_TOLERANCE_US},
                True,
            )
        )

        try:
            black_result = selected_adapter.detect_black_frames(output_path)
            black_values = [
                float(value) for value in _BLACK_FRAME_PATTERN.findall(black_result.stderr)
            ]
            black_detected = any(value >= 98.0 for value in black_values)
            findings.append(
                _finding(
                    "finding_black_frames",
                    "BLACK_FRAMES",
                    "fail" if black_detected else "pass",
                    "high" if black_detected else "info",
                    "Black-frame evidence exceeds the threshold"
                    if black_detected
                    else "No full black frames were detected",
                    {**_process_evidence(black_result), "pblack_values": black_values},
                    True,
                )
            )
        except VideoeditError as exc:
            findings.append(
                _finding(
                    "finding_black_frames",
                    "BLACK_FRAMES",
                    "fail",
                    "high",
                    "Black-frame detection did not produce evidence",
                    {"error": exc.message},
                    True,
                    "Rerun black-frame detection before Gate 2",
                )
            )
        try:
            freeze_result = selected_adapter.detect_freeze_frames(output_path)
            freeze_values = _FREEZE_PATTERN.findall(freeze_result.stderr)
            freeze_detected = bool(freeze_values)
            findings.append(
                _finding(
                    "finding_freeze_frames",
                    "FREEZE_FRAMES",
                    "warning" if freeze_detected else "pass",
                    "medium" if freeze_detected else "info",
                    (
                        "Automated freeze-frame evidence was detected; classify each interval as "
                        "intentional static screen state or a visual defect"
                        if freeze_detected
                        else "No freeze-frame intervals were detected"
                    ),
                    {**_process_evidence(freeze_result), "freeze_starts": freeze_values},
                    True,
                    (
                        "Review the listed intervals against the join previews before Gate 3"
                        if freeze_detected
                        else None
                    ),
                )
            )
        except VideoeditError as exc:
            findings.append(
                _finding(
                    "finding_freeze_frames",
                    "FREEZE_FRAMES",
                    "fail",
                    "high",
                    "Freeze-frame detection did not produce evidence",
                    {"error": exc.message},
                    True,
                    "Rerun freeze-frame detection before Gate 2",
                )
            )
        try:
            clipping_result = selected_adapter.measure_clipping(output_path)
            clipped_samples = parse_clipped_samples(clipping_result)
            findings.append(
                _finding(
                    "finding_clipping",
                    "CLIPPING",
                    "warning" if clipped_samples > 0 else "pass",
                    "medium" if clipped_samples > 0 else "info",
                    (
                        "Automated clipping evidence was detected; operator must verify whether "
                        "the sample is an audible defect"
                        if clipped_samples > 0
                        else "No clipped audio samples were detected"
                    ),
                    {**_process_evidence(clipping_result), "clipped_samples": clipped_samples},
                    True,
                    (
                        "Inspect the affected audio around the reported sample before Gate 3"
                        if clipped_samples > 0
                        else None
                    ),
                )
            )
        except VideoeditError as exc:
            findings.append(
                _finding(
                    "finding_clipping",
                    "CLIPPING",
                    "fail",
                    "high",
                    "Audio clipping detection did not produce evidence",
                    {"error": exc.message},
                    True,
                    "Rerun clipping detection before Gate 2",
                )
            )

        try:
            silence_result = selected_adapter.detect_silence_result(
                output_path, threshold_db=-38.0, minimum_duration_us=650_000
            )
            parsed_silence = parse_silencedetect_detailed(silence_result.stderr, output_duration_us)
            rendered_words = comparison.get("rendered_words", []) if comparison else []
            word_starts = [
                int(item["start_us"]) for item in rendered_words if isinstance(item, Mapping)
            ]
            word_ends = [
                int(item["end_us"]) for item in rendered_words if isinstance(item, Mapping)
            ]
            first_word = min(word_starts) if word_starts else None
            last_word = max(word_ends) if word_ends else None
            interior = [
                {"start_us": start_us, "end_us": end_us}
                for start_us, end_us in parsed_silence.intervals
                if first_word is not None
                and last_word is not None
                and start_us > first_word
                and end_us < last_word
            ]
            findings.append(
                _finding(
                    "finding_dead_air",
                    "DEAD_AIR",
                    "warning" if interior else "pass",
                    "medium" if interior else "info",
                    (
                        "Automated silence evidence falls between rendered words and requires "
                        "operator classification"
                        if interior
                        else "Silence intervals do not create interior dead air"
                    ),
                    {
                        **_process_evidence(silence_result),
                        "intervals": [
                            {"start_us": start_us, "end_us": end_us}
                            for start_us, end_us in parsed_silence.intervals
                        ],
                        "interior_intervals": interior,
                        "warnings": parsed_silence.warnings,
                    },
                    True,
                    (
                        "Review each interval as dead air or an intentional pause; apply a "
                        "bounded fix marker, then re-render and re-transcribe before Gate 3"
                        if interior
                        else None
                    ),
                    time_range=interior[0] if interior else None,
                )
            )
        except VideoeditError as exc:
            findings.append(
                _finding(
                    "finding_dead_air",
                    "DEAD_AIR",
                    "fail",
                    "high",
                    "Silence detection did not produce evidence",
                    {"error": exc.message},
                    True,
                    "Rerun silence detection before Gate 2",
                )
            )

        rendered_sequence = comparison.get("rendered_sequence", []) if comparison else []
        if comparison is None:
            for code, message in (
                ("TRANSCRIPT_SEQUENCE", "Rendered transcript comparison is missing"),
                ("DUPLICATE_PHRASES", "Duplicate-phrase evidence is missing"),
            ):
                findings.append(
                    _finding(
                        f"finding_{code.lower()}",
                        code,
                        "fail",
                        "high",
                        message,
                        {},
                        True,
                        "Re-transcribe the rendered segment before Gate 2",
                    )
                )
        else:
            sequence_pass = (
                comparison.get("sequence_status") == "pass"
                and not comparison.get("missing_words")
                and not comparison.get("unexpected_words")
            )
            comparison_uncertain = comparison.get("status") == "warning"
            sequence_status = (
                "pass" if sequence_pass else "warning" if comparison_uncertain else "fail"
            )
            findings.append(
                _finding(
                    "finding_transcript_sequence",
                    "TRANSCRIPT_SEQUENCE",
                    sequence_status,
                    "info" if sequence_pass else "medium" if comparison_uncertain else "high",
                    "Rendered speech matches intended speech after the approved cut"
                    if sequence_pass
                    else (
                        "Local ASR differs from intended speech; operator review is required"
                        if comparison_uncertain
                        else "Rendered speech differs from intended speech"
                    ),
                    {
                        "expected_sequence": comparison.get("expected_sequence", []),
                        "rendered_sequence": comparison.get("rendered_sequence", []),
                        "missing_words": comparison.get("missing_words", []),
                        "unexpected_words": comparison.get("unexpected_words", []),
                    },
                    True,
                )
            )
            duplicate_words = comparison.get("duplicate_words", [])
            duplicate_uncertain = comparison.get("status") == "warning"
            findings.append(
                _finding(
                    "finding_duplicate_phrases",
                    "DUPLICATE_PHRASES",
                    ("warning" if duplicate_uncertain else "fail") if duplicate_words else "pass",
                    ("medium" if duplicate_uncertain else "high") if duplicate_words else "info",
                    (
                        "ASR duplicate-word evidence requires operator review"
                        if duplicate_words and duplicate_uncertain
                        else "Duplicate words remain in the rendered speech"
                        if duplicate_words
                        else "No duplicate words remain in the rendered speech"
                    ),
                    {"duplicate_words": duplicate_words},
                    True,
                )
            )

        if caption_plan is None:
            findings.append(
                _finding(
                    "finding_caption_drift",
                    "CAPTION_DRIFT",
                    "skipped",
                    "low",
                    "Caption drift check was skipped because no caption plan was supplied",
                    {},
                    False,
                )
            )
        else:
            caption_status, caption_message, caption_evidence = _caption_drift(
                caption_plan,
                output_duration_us,
                [str(value) for value in rendered_sequence if isinstance(value, str)],
            )
            findings.append(
                _finding(
                    "finding_caption_drift",
                    "CAPTION_DRIFT",
                    caption_status,
                    "info" if caption_status == "pass" else "high",
                    caption_message,
                    caption_evidence,
                    True,
                )
            )

        removed_ranges = media_manifest.get("removed_ranges", [])
        if removed_ranges and join_report is None:
            findings.append(
                _finding(
                    "finding_join_boundaries",
                    "JOIN_BOUNDARIES",
                    "warning",
                    "high",
                    "The recut has joins but no rendered join QA report was supplied",
                    {"join_count": len(removed_ranges)},
                    True,
                    "Render and inspect each join with the join QA service",
                )
            )
        elif join_report is None:
            findings.append(
                _finding(
                    "finding_join_boundaries",
                    "JOIN_BOUNDARIES",
                    "pass",
                    "info",
                    "No structural recut joins were present",
                    {"join_count": 0},
                    True,
                )
            )
        else:
            join_status = str(join_report.get("overall_status", "fail"))
            findings.append(
                _finding(
                    "finding_join_boundaries",
                    "JOIN_BOUNDARIES",
                    join_status if join_status in {"pass", "warning", "fail"} else "fail",
                    "info" if join_status == "pass" else "high",
                    "Rendered join QA passed"
                    if join_status == "pass"
                    else "Rendered join QA needs repair",
                    {"overall_status": join_status},
                    True,
                )
            )

        required_failures = sum(
            1 for item in findings if item["required"] and item["status"] != "pass"
        )
        counts = {
            "total": len(findings),
            "passed": sum(item["status"] == "pass" for item in findings),
            "warnings": sum(item["status"] == "warning" for item in findings),
            "failed": sum(item["status"] == "fail" for item in findings),
            "skipped": sum(item["status"] == "skipped" for item in findings),
            "required_failures": required_failures,
        }
        overall_status = (
            "fail" if counts["failed"] else ("warning" if required_failures else "pass")
        )
        report: dict[str, Any] = {
            "schema_name": "segment_qa_report",
            "schema_version": "1.0.0",
            "artifact_id": f"art_segment_qa_{revision_id}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "created_at": now_iso(),
            "producer": producer("segment-qa", "ffmpeg", ffmpeg_version),
            "inputs": [artifact_input("revision_media_manifest", selected_media_path)],
            "scope": {
                "source_range": {"start_us": 0, "end_us": source_duration_us},
                "output_range": {"start_us": 0, "end_us": output_duration_us},
            },
            "media": _file_ref("revision_recut_media", output_path),
            "comparison": _file_ref("transcript_comparison", selected_comparison)
            if selected_comparison is not None
            else None,
            "caption_plan": _file_ref("caption_plan", selected_caption)
            if selected_caption is not None
            else None,
            "findings": findings,
            "summary": counts,
            "overall_status": overall_status,
            "final_ready": required_failures == 0,
        }
        if selected_comparison is not None:
            report["inputs"].append(artifact_input("transcript_comparison", selected_comparison))
        if selected_caption is not None:
            report["inputs"].append(artifact_input("caption_plan", selected_caption))
        if selected_join is not None:
            report["inputs"].append(artifact_input("join_qa_report", selected_join))
        write_validated_artifact(
            package_root, "segment_qa_report", staging_root / report_path.name, report
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root / report_path.name, report_path)
        return report_path
