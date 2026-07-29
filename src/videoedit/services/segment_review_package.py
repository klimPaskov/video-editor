from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import PlanningValidationError, RenderOutputError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p10-02c"
KNOWN_REVIEW_ARTIFACT_NAMES = (
    "effect-plan.json",
    "focus-pacing-plan.json",
    "visual-timeline.json",
    "asset-manifest.json",
)
DIAGNOSTIC_SCHEMA_NAMES = {
    "matting_quality_review",
    "matting_result",
    "object_replacement_manifest",
    "object_track_review",
    "occluder_manifest",
    "segmentation_validation",
}


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


def _file_ref(artifact_id: str, path: Path) -> dict[str, str]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
    }


def _safe_id(value: str, prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_")
    return f"{prefix}_{normalized or 'artifact'}"


def _timecode(time_us: int) -> str:
    total_ms = max(0, round(time_us / 1000))
    milliseconds = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _overlap(start_us: int, end_us: int, range_start_us: int, range_end_us: int) -> int:
    return max(0, min(end_us, range_end_us) - max(start_us, range_start_us))


def _range(value: object, description: str) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        start_us = int(str(value["start_us"]))
        end_us = int(str(value["end_us"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{description} has invalid time bounds") from exc
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError(f"{description} must be a positive half-open range")
    return start_us, end_us


def _preview_plan(
    package_root: Path,
    path: Path,
    layout: ProjectLayout,
    revision_id: str,
) -> dict[str, Any]:
    payload = _read_object(path, "segment preview plan")
    validate_artifact(package_root, "segment_preview", payload)
    if payload["project_id"] != layout.root.name or payload["revision_id"] != revision_id:
        raise PlanningValidationError(
            "segment preview plan is bound to a different project or revision"
        )
    source_media = payload["source_media"]
    source_path = _owned_path(layout, Path(str(source_media["path"])), "segment preview source")
    if not source_path.is_file() or sha256_file(source_path) != source_media["sha256"]:
        raise PlanningValidationError("segment preview source hash is stale")
    transcript_ref = payload.get("transcript")
    if isinstance(transcript_ref, Mapping):
        transcript_path = _owned_path(
            layout, Path(str(transcript_ref["path"])), "segment preview transcript"
        )
        if (
            not transcript_path.is_file()
            or sha256_file(transcript_path) != transcript_ref["sha256"]
        ):
            raise PlanningValidationError("segment preview transcript hash is stale")
    for item in payload["segments"]:
        preview_path = _owned_path(layout, Path(str(item["preview_path"])), "segment preview clip")
        if not preview_path.is_file() or sha256_file(preview_path) != item["preview_sha256"]:
            raise PlanningValidationError(f"segment preview clip hash is stale: {preview_path}")
    return payload


def _review_artifacts(
    layout: ProjectLayout,
    paths: Sequence[Path] | None,
    *,
    revision_id: str,
) -> list[Path]:
    selected: list[Path] = []
    if paths:
        selected.extend(_owned_path(layout, path, "review artifact") for path in paths)
    for root in (layout.artifacts, layout.work):
        for name in KNOWN_REVIEW_ARTIFACT_NAMES:
            candidate = (root / name).resolve()
            if not candidate.is_file() or candidate in selected:
                continue
            try:
                payload = _read_object(candidate, "review artifact")
            except PlanningValidationError:
                # Preserve the existing validation failure for malformed
                # defaults; only a known stale revision is safely ignored.
                selected.append(candidate)
                continue
            if payload.get("project_id") not in (None, layout.root.name):
                continue
            if payload.get("revision_id") not in (None, revision_id):
                continue
            selected.append(candidate)
    return selected


def _load_review_artifact(
    package_root: Path, path: Path, project_id: str, revision_id: str
) -> dict[str, Any]:
    payload = _read_object(path, "review artifact")
    schema_name = payload.get("schema_name")
    if (
        isinstance(schema_name, str)
        and (package_root / "schemas" / f"{schema_name}.schema.json").is_file()
    ):
        validate_artifact(package_root, schema_name, payload)
    if payload.get("project_id") not in (None, project_id):
        raise PlanningValidationError(f"review artifact belongs to another project: {path}")
    artifact_revision = payload.get("revision_id")
    if artifact_revision not in (None, revision_id):
        raise PlanningValidationError(f"review artifact belongs to another revision: {path}")
    return payload


def _artifact_range(item: Mapping[str, Any]) -> tuple[int, int] | None:
    for key in ("source_range", "range_us", "range"):
        selected = _range(item.get(key), f"effect {key}")
        if selected is not None:
            return selected
    start = item.get("start_us")
    end = item.get("end_us")
    if start is None or end is None:
        return None
    return _range({"start_us": start, "end_us": end}, "effect range")


def _asset_refs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        asset_id = str(item.get("asset_id", ""))
        asset_sha = str(item.get("sha256") or item.get("asset_sha256") or "")
        if not asset_id or not re.fullmatch(r"[a-f0-9]{64}", asset_sha):
            continue
        ref: dict[str, Any] = {"asset_id": asset_id, "sha256": asset_sha}
        if item.get("path") is not None:
            ref["path"] = str(item["path"])
        if item.get("licence_reference") is not None:
            ref["licence_reference"] = str(item["licence_reference"])
        refs.append(ref)
    return refs


def _effect_summary(
    package_root: Path,
    layout: ProjectLayout,
    segment: Mapping[str, Any],
    planning_key: str,
    artifact_paths: Sequence[Path],
    revision_id: str,
) -> dict[str, Any]:
    source_range = _range(segment.get("source_range"), "segment source range")
    if source_range is None:
        raise PlanningValidationError("segment is missing source range")
    range_start_us, range_end_us = source_range
    source_artifacts: list[dict[str, str]] = []
    effects: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    saw_effect_source = False
    saw_asset_source = False

    for path in artifact_paths:
        payload = _load_review_artifact(package_root, path, layout.root.name, revision_id)
        schema_name = str(payload.get("schema_name") or path.stem)
        source_artifacts.append(_file_ref(_safe_id(schema_name, "review"), path))
        if schema_name == "asset_manifest":
            saw_asset_source = True
            for item in payload.get("assets", []):
                if not isinstance(item, Mapping):
                    continue
                asset_id = str(item.get("asset_id", ""))
                asset_sha = str(item.get("asset_sha256", ""))
                if not asset_id or not re.fullmatch(r"[a-f0-9]{64}", asset_sha):
                    continue
                assets.append(
                    {
                        "asset_id": asset_id,
                        "sha256": asset_sha,
                        "licence_reference": str(item.get("licence_reference", "")) or None,
                    }
                )
            continue
        if schema_name == "effect_plan":
            saw_effect_source = True
            raw_effects = payload.get("effects", [])
        elif schema_name == "focus_pacing_plan":
            saw_effect_source = True
            raw_effects = [
                {
                    **dict(item),
                    "id": item.get("zoom_id"),
                    "kind": "purposeful_zoom",
                    "renderer": "remotion",
                }
                for item in payload.get("zooms", [])
                if isinstance(item, Mapping)
            ] + [
                {
                    **dict(item),
                    "id": item.get("speedup_id"),
                    "kind": "prompt_speedup",
                    "renderer": "ffmpeg",
                }
                for item in payload.get("speedups", [])
                if isinstance(item, Mapping)
            ]
        else:
            continue
        if not isinstance(raw_effects, list):
            continue
        for raw in raw_effects:
            if not isinstance(raw, Mapping):
                continue
            effect_range = _artifact_range(raw)
            if effect_range is None:
                continue
            effect_start_us, effect_end_us = effect_range
            overlap_us = _overlap(effect_start_us, effect_end_us, range_start_us, range_end_us)
            if overlap_us <= 0:
                continue
            effect_id = str(
                raw.get("id") or raw.get("effect_id") or raw.get("zoom_id") or raw.get("speedup_id")
            )
            kind = str(raw.get("kind") or raw.get("action_type") or "effect")
            renderer = str(raw.get("renderer") or "remotion")
            clipped_start_us = max(effect_start_us, range_start_us)
            clipped_end_us = min(effect_end_us, range_end_us)
            effects.append(
                {
                    "effect_id": effect_id or f"effect_{len(effects) + 1:06d}",
                    "kind": kind,
                    "renderer": renderer,
                    "source_range": {
                        "start_us": clipped_start_us,
                        "end_us": clipped_end_us,
                    },
                    "overlap_us": overlap_us,
                    "asset_refs": _asset_refs(raw.get("asset_refs")),
                    "status": "current",
                }
            )

    if not saw_effect_source:
        warnings.append("effect_plan_not_present")
    elif not effects:
        warnings.append("no_effects_overlap_segment")
    if not saw_asset_source:
        warnings.append("asset_manifest_not_present")
    unique_assets = {str(item["asset_id"]): item for item in assets}
    return {
        "schema_name": "segment_effect_summary",
        "schema_version": "1.0.0",
        "artifact_id": _safe_id(
            f"{layout.root.name}_{segment['segment_id']}", "art_effect_summary"
        ),
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "segment_id": str(segment["segment_id"]),
        "created_at": now_iso(),
        "producer": producer("segment-review", "local-artifacts", __version__),
        "planning_key": planning_key,
        "source_range": {"start_us": range_start_us, "end_us": range_end_us},
        "source_artifacts": source_artifacts,
        "effects": effects,
        "assets": list(unique_assets.values()),
        "warnings": list(dict.fromkeys(warnings)),
        "status": "warning" if warnings else "complete",
    }


def _diagnostic_summary(
    package_root: Path,
    layout: ProjectLayout,
    segment: Mapping[str, Any],
    planning_key: str,
    revision_id: str,
    preview_sha256: str,
) -> dict[str, Any]:
    candidates: list[Path] = []
    for root in (layout.artifacts, layout.work):
        if not root.is_dir():
            continue
        for path in root.rglob("*.json"):
            try:
                payload = _read_object(path, "diagnostic artifact")
            except PlanningValidationError:
                continue
            if str(payload.get("schema_name")) in DIAGNOSTIC_SCHEMA_NAMES:
                candidates.append(path.resolve())
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in sorted(set(candidates)):
        payload = _load_review_artifact(package_root, path, layout.root.name, revision_id)
        raw_status = str(
            payload.get("status")
            or payload.get("validation_status")
            or payload.get("qa_status")
            or "warning"
        )
        if raw_status in {"pass", "complete", "valid"}:
            status = "pass"
        elif raw_status in {"fail", "failed"}:
            status = "fail"
        else:
            status = "warning"
        item_warnings = [str(value) for value in payload.get("warnings", [])]
        if status != "pass":
            item_warnings.append(f"artifact_status:{raw_status}")
        records.append(
            {
                "artifact_id": _safe_id(str(payload.get("artifact_id") or path.stem), "diagnostic"),
                "kind": str(payload.get("schema_name") or path.stem),
                "path": str(path),
                "sha256": sha256_file(path),
                "status": status,
                "warnings": list(dict.fromkeys(item_warnings)),
            }
        )
        if item_warnings:
            warnings.extend(f"{path.name}:{value}" for value in item_warnings)
    if not records:
        warnings.append("no_mask_or_matte_diagnostics")
    return {
        "schema_name": "segment_diagnostics",
        "schema_version": "1.0.0",
        "artifact_id": _safe_id(f"{layout.root.name}_{segment['segment_id']}", "art_diagnostics"),
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "segment_id": str(segment["segment_id"]),
        "created_at": now_iso(),
        "producer": producer("segment-review", "local-artifacts", __version__),
        "planning_key": planning_key,
        "preview_sha256": preview_sha256,
        "mask_matte": records,
        "warnings": list(dict.fromkeys(warnings)),
        "status": "warning" if warnings else "complete",
    }


def _transcript_excerpt(
    package_root: Path,
    layout: ProjectLayout,
    transcript_path: Path | None,
    segment: Mapping[str, Any],
    preview_sha256: str,
    revision_id: str,
) -> tuple[dict[str, Any], str]:
    source_range = _range(segment.get("source_range"), "segment source range")
    if source_range is None:
        raise PlanningValidationError("segment is missing source range")
    start_us, end_us = source_range
    warnings: list[str] = []
    inputs: list[dict[str, str]] = []
    transcript_value: dict[str, Any] | None = None
    if transcript_path is not None:
        transcript_value = _read_object(transcript_path, "segment transcript")
        validate_artifact(package_root, "transcript", transcript_value)
        if (
            transcript_value["project_id"] != layout.root.name
            or transcript_value["revision_id"] != revision_id
        ):
            raise PlanningValidationError(
                "segment transcript belongs to a different project or revision"
            )
        inputs.append(artifact_input("segment_preview_transcript", transcript_path))
    else:
        warnings.append("transcript_not_provided")
    words: list[dict[str, Any]] = []
    transcript_segment_ids: list[str] = []
    segment_texts: list[str] = []
    if transcript_value is not None:
        for value in transcript_value.get("segments", []):
            if not isinstance(value, Mapping):
                continue
            segment_range = _range(value, "transcript segment")
            if segment_range is None:
                continue
            if _overlap(segment_range[0], segment_range[1], start_us, end_us) > 0:
                segment_id = str(value["segment_id"])
                transcript_segment_ids.append(segment_id)
                text = " ".join(str(value.get("text", "")).split())
                if text:
                    segment_texts.append(text)
        for value in transcript_value.get("words", []):
            if not isinstance(value, Mapping):
                continue
            word_range = _range(value, "transcript word")
            if word_range is None:
                continue
            if _overlap(word_range[0], word_range[1], start_us, end_us) <= 0:
                continue
            words.append(
                {
                    "word_id": str(value["word_id"]),
                    "segment_id": str(value["segment_id"]),
                    "text": str(value["text"]),
                    "start_us": word_range[0],
                    "end_us": word_range[1],
                    "probability": value.get("probability"),
                    "timing_status": str(value.get("timing_status", "original")),
                }
            )
        if not words:
            warnings.append("no_words_in_segment_range")
    text = " ".join(word["text"] for word in words) or " ".join(segment_texts)
    if not text:
        warnings.append("no_transcript_text_in_segment_range")
    transcript_segment_ids = list(dict.fromkeys(transcript_segment_ids))
    excerpt = {
        "schema_name": "segment_transcript_excerpt",
        "schema_version": "1.0.0",
        "artifact_id": _safe_id(
            f"{layout.root.name}_{segment['segment_id']}", "art_transcript_excerpt"
        ),
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "segment_id": str(segment["segment_id"]),
        "created_at": now_iso(),
        "producer": producer("segment-review", "canonical-transcript", __version__),
        "inputs": inputs,
        "preview_sha256": preview_sha256,
        "source_range": {"start_us": start_us, "end_us": end_us},
        "transcript_segment_ids": transcript_segment_ids,
        "text": text,
        "words": words,
        "warnings": list(dict.fromkeys(warnings)),
        "status": "warning" if warnings else "complete",
    }
    markdown_lines = [
        f"# {segment['segment_id']}",
        "",
        f"Source range: {_timecode(start_us)} - {_timecode(end_us)}",
        "",
        "## Intended speech",
        "",
        text or "_No transcript text in this range._",
        "",
        "## Word timings",
        "",
        "| Time | Word | Word ID | Probability |",
        "| --- | --- | --- | --- |",
    ]
    if words:
        markdown_lines.extend(
            f"| {_timecode(int(word['start_us']))} - {_timecode(int(word['end_us']))} | "
            f"{word['text']} | {word['word_id']} | {word['probability']} |"
            for word in words
        )
    else:
        markdown_lines.append("| _none_ | _none_ | _none_ | _none_ |")
    if warnings:
        markdown_lines.extend(["", "## Warnings", ""])
        markdown_lines.extend(f"- {warning}" for warning in dict.fromkeys(warnings))
    return excerpt, "\n".join(markdown_lines) + "\n"


def _promote_stage(stage: Path, final: Path) -> None:
    if not stage.is_file():
        raise RenderOutputError(f"staged review output is missing: {stage}")
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        if sha256_file(final) == sha256_file(stage):
            stage.unlink()
            return
        raise StateConflictError(f"review output already exists with different bytes: {final}")
    os.replace(stage, final)


def _verify_package_file_refs(
    layout: ProjectLayout, package: Mapping[str, Any], package_path: Path
) -> None:
    for key in (
        "preview",
        "contact_sheet",
        "transcript_excerpt",
        "transcript_markdown",
        "effect_summary",
        "diagnostics",
        "fixes_template",
    ):
        value = package.get(key)
        if not isinstance(value, Mapping):
            raise PlanningValidationError(f"review package is missing file reference: {key}")
        path = _owned_path(layout, Path(str(value.get("path", ""))), f"review package {key}")
        if not path.is_file() or sha256_file(path) != value.get("sha256"):
            raise PlanningValidationError(
                f"review package file reference is stale: {package_path} ({key})"
            )


def _contact_sheet(
    adapter: FFmpegAdapter,
    preview_path: Path,
    output: Path,
    frame_count: int,
) -> None:
    probe = adapter.probe(preview_path)
    actual_count = frame_count
    streams = probe.get("streams", [])
    if isinstance(streams, list):
        for item in streams:
            if isinstance(item, Mapping) and item.get("codec_type") == "video":
                try:
                    actual_count = max(1, int(str(item.get("nb_frames", frame_count))))
                except (TypeError, ValueError):
                    pass
                break
    indices = sorted(set([0, max(0, actual_count // 2), max(0, actual_count - 1)]))
    stage = output.with_name(f".{output.stem}.{uuid.uuid4().hex}{output.suffix}")
    try:
        adapter.make_contact_sheet(
            preview_path,
            stage,
            indices,
            scale_width=480,
            tile_columns=len(indices),
        )
        if not stage.is_file() or stage.stat().st_size <= 0:
            raise RenderOutputError(f"contact sheet output is empty: {stage}")
        _promote_stage(stage, output)
    except BaseException:
        if stage.is_file():
            failed = stage.with_name(f"{stage.name}.failed-{uuid.uuid4().hex}")
            os.replace(stage, failed)
        raise


def build_segment_review_packages(
    package_root: Path,
    layout: ProjectLayout,
    preview_plan_path: Path,
    *,
    transcript_path: Path | None = None,
    review_artifact_paths: Sequence[Path] | None = None,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> list[Path]:
    preview_plan_path = _owned_path(layout, preview_plan_path, "segment preview plan")
    if not preview_plan_path.is_file():
        raise PlanningValidationError(f"segment preview plan does not exist: {preview_plan_path}")
    selected_transcript = (
        _owned_path(layout, transcript_path, "segment transcript") if transcript_path else None
    )
    if selected_transcript is not None and not selected_transcript.is_file():
        raise PlanningValidationError(f"segment transcript does not exist: {selected_transcript}")
    selected_adapter = adapter or FFmpegAdapter()

    with ProjectLock(layout, stage="segment_review_package", revision_id=revision_id):
        plan = _preview_plan(package_root, preview_plan_path, layout, revision_id)
        plan_transcript = plan.get("transcript")
        if selected_transcript is None and isinstance(plan_transcript, Mapping):
            selected_transcript = _owned_path(
                layout, Path(str(plan_transcript["path"])), "segment transcript"
            )
        artifact_paths = _review_artifacts(
            layout,
            review_artifact_paths,
            revision_id=revision_id,
        )
        dependency_hashes = [sha256_file(path) for path in artifact_paths]
        outputs: list[Path] = []
        for segment in plan["segments"]:
            preview_path = _owned_path(
                layout, Path(str(segment["preview_path"])), "segment preview"
            )
            package_key = make_stage_key(
                "segment_review_package",
                f"{__version__}:{IMPLEMENTATION_VERSION}",
                [str(plan["planning_key"]), str(segment["preview_sha256"]), *dependency_hashes],
                {
                    "project_id": layout.root.name,
                    "revision_id": revision_id,
                    "segment_id": segment["segment_id"],
                    "source_range": segment["source_range"],
                },
            )
            package_dir = (
                layout.review
                / "segments"
                / str(segment["segment_id"])
                / str(plan["planning_key"])[:16]
                / f"package-{package_key[:16]}"
            ).resolve()
            package_path = package_dir / "review-package.json"
            if package_path.is_file():
                current = _read_object(package_path, "segment review package")
                validate_artifact(package_root, "segment_review_package", current)
                if current.get("package_key") != package_key:
                    raise StateConflictError(f"review package key mismatch: {package_path}")
                _verify_package_file_refs(layout, current, package_path)
                outputs.append(package_path)
                continue
            package_dir.mkdir(parents=True, exist_ok=True)
            contact_sheet_path = package_dir / "contact-sheet.jpg"
            _contact_sheet(
                selected_adapter,
                preview_path,
                contact_sheet_path,
                int(segment["frame_count"]),
            )
            transcript_excerpt, transcript_markdown = _transcript_excerpt(
                package_root,
                layout,
                selected_transcript,
                segment,
                str(segment["preview_sha256"]),
                revision_id,
            )
            effect_summary = _effect_summary(
                package_root,
                layout,
                segment,
                str(plan["planning_key"]),
                artifact_paths,
                revision_id,
            )
            diagnostics = _diagnostic_summary(
                package_root,
                layout,
                segment,
                str(plan["planning_key"]),
                revision_id,
                str(segment["preview_sha256"]),
            )
            transcript_path_out = package_dir / "transcript-excerpt.json"
            effect_path_out = package_dir / "effect-summary.json"
            diagnostics_path_out = package_dir / "diagnostics.json"
            for schema_name, path, payload in (
                ("segment_transcript_excerpt", transcript_path_out, transcript_excerpt),
                ("segment_effect_summary", effect_path_out, effect_summary),
                ("segment_diagnostics", diagnostics_path_out, diagnostics),
            ):
                stage = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                write_validated_artifact(package_root, schema_name, stage, payload)
                _promote_stage(stage, path)
            markdown_path = package_dir / "transcript-excerpt.md"
            markdown_stage = markdown_path.with_name(
                f".{markdown_path.name}.{uuid.uuid4().hex}.tmp"
            )
            write_text_atomically(markdown_stage, transcript_markdown)
            _promote_stage(markdown_stage, markdown_path)
            fixes_path = package_dir / "fixes.template.md"
            fixes_stage = fixes_path.with_name(f".{fixes_path.name}.{uuid.uuid4().hex}.tmp")
            source_start_us = int(segment["source_range"]["start_us"])
            source_end_us = int(segment["source_range"]["end_us"])
            fixes_template = (
                f"# Review markers for {segment['segment_id']}\\n\\n"
                "Add one marker per line. Ranges are half-open canonical microseconds or "
                "HH:MM:SS.mmm timecodes.\\n\\n"
                f"[KEEP {_timecode(source_start_us)}-{_timecode(source_end_us)}] "
                "Keep this reviewed range.\\n"
                f"[FIX {_timecode(source_start_us)}-{_timecode(source_end_us)}] "
                "Describe a concrete correction.\\n"
                f"[ZOOM {_timecode(source_start_us)}-{_timecode(source_end_us)}] "
                "Describe the visible target and purposeful reason.\\n"
                f"[SPEED {_timecode(source_start_us)}-{_timecode(source_end_us)}] "
                "Describe the allowed visible prompt action and requested rate.\\n"
            )
            write_text_atomically(fixes_stage, fixes_template)
            _promote_stage(fixes_stage, fixes_path)
            warnings = list(
                dict.fromkeys(
                    [
                        *[str(value) for value in plan.get("warnings", [])],
                        *[str(value) for value in transcript_excerpt["warnings"]],
                        *[str(value) for value in effect_summary["warnings"]],
                        *[str(value) for value in diagnostics["warnings"]],
                    ]
                )
            )
            package_payload: dict[str, Any] = {
                "schema_name": "segment_review_package",
                "schema_version": "1.0.0",
                "artifact_id": _safe_id(
                    f"{layout.root.name}_{segment['segment_id']}_{package_key[:12]}",
                    "art_review_package",
                ),
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "segment_id": str(segment["segment_id"]),
                "created_at": now_iso(),
                "producer": producer("segment-review", "ffmpeg-and-local-artifacts", __version__),
                "planning_key": str(plan["planning_key"]),
                "package_key": package_key,
                "source_range": dict(segment["source_range"]),
                "preview": _file_ref(
                    _safe_id(str(segment["segment_id"]), "segment_preview"), preview_path
                ),
                "contact_sheet": _file_ref("segment_contact_sheet", contact_sheet_path),
                "transcript_excerpt": _file_ref("segment_transcript_excerpt", transcript_path_out),
                "transcript_markdown": _file_ref("segment_transcript_markdown", markdown_path),
                "effect_summary": _file_ref("segment_effect_summary", effect_path_out),
                "diagnostics": _file_ref("segment_diagnostics", diagnostics_path_out),
                "fixes_template": _file_ref("segment_fixes_template", fixes_path),
                "warnings": warnings,
                "status": "warning" if warnings else "complete",
            }
            stage_package = package_path.with_name(f".{package_path.name}.{uuid.uuid4().hex}.tmp")
            write_validated_artifact(
                package_root, "segment_review_package", stage_package, package_payload
            )
            _promote_stage(stage_package, package_path)
            outputs.append(package_path)
    return outputs
