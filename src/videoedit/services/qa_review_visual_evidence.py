from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from videoedit.errors import PlanningValidationError, StateConflictError
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

IMPLEMENTATION_VERSION = "p11-qa-review-visual-evidence-1"
_BOUNDARY_OFFSETS_US = (-250_000, 0, 250_000)


class ReviewVisualEvidenceAdapter(Protocol):
    def probe_frame_count(self, path: Path) -> int | None: ...

    def make_contact_sheet(
        self,
        source: Path,
        output: Path,
        frame_indices: Sequence[int],
        *,
        scale_width: int = 320,
        tile_columns: int | None = None,
        filter_prefix: str | None = None,
        input_start_number: int | None = None,
    ) -> object: ...


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    selected = path.expanduser().resolve()
    try:
        selected.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return selected


def _candidate_path(layout: ProjectLayout, path: Path) -> Path:
    selected = path.expanduser().resolve()
    workspace = layout.root.parent.parent.resolve()
    allowed_roots = (layout.root.resolve(), (workspace / "outputs").resolve())
    if not any(selected == root or root in selected.parents for root in allowed_roots):
        raise PlanningValidationError(
            "QA visual evidence candidate must be inside the project or workspace outputs"
        )
    if not selected.is_file() or selected.stat().st_size <= 0:
        raise PlanningValidationError(f"QA visual evidence candidate is missing: {selected}")
    return selected


def _file_ref(
    layout: ProjectLayout, path: Path, artifact_id: str, description: str
) -> dict[str, Any]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file() or selected.stat().st_size <= 0:
        raise PlanningValidationError(f"{description} is missing or empty: {selected}")
    return {
        "artifact_id": artifact_id,
        "path": str(selected),
        "sha256": sha256_file(selected),
        "size_bytes": selected.stat().st_size,
    }


def _candidate_ref(layout: ProjectLayout, path: Path, artifact_id: str) -> dict[str, Any]:
    selected = _candidate_path(layout, path)
    return {
        "artifact_id": artifact_id,
        "path": str(selected),
        "sha256": sha256_file(selected),
        "size_bytes": selected.stat().st_size,
    }


def _validate_ref(
    layout: ProjectLayout,
    reference: Mapping[str, Any],
    description: str,
    *,
    candidate_allowed: bool = False,
) -> Path:
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise PlanningValidationError(f"{description} path is missing")
    selected = (
        _candidate_path(layout, Path(raw_path))
        if candidate_allowed
        else _owned_path(layout, Path(raw_path), description)
    )
    if reference.get("sha256") != sha256_file(selected):
        raise PlanningValidationError(f"{description} hash is stale: {selected}")
    if reference.get("size_bytes") != selected.stat().st_size:
        raise PlanningValidationError(f"{description} size is stale: {selected}")
    return selected


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value.casefold()).strip("_")
    return normalized or "join"


def _nearest_frame(relative_us: int, duration_us: int, frame_count: int) -> int:
    if duration_us <= 0 or frame_count <= 0:
        raise ValueError("duration_us and frame_count must be positive")
    if not 0 <= relative_us <= duration_us:
        raise ValueError("relative_us must be inside the preview duration")
    last_frame = frame_count - 1
    numerator = relative_us * last_frame
    # Explicit nearest-integer rounding, with half values rounded upward.
    return min(last_frame, (2 * numerator + duration_us) // (2 * duration_us))


def sample_join_frames(
    preview_start_us: int,
    preview_end_us: int,
    output_join_us: int,
    frame_count: int,
) -> list[dict[str, int | str]]:
    """Select deterministic visual samples around a join boundary."""

    if preview_start_us < 0 or preview_end_us <= preview_start_us:
        raise ValueError("preview range must be a positive nonnegative half-open range")
    if not preview_start_us <= output_join_us <= preview_end_us:
        raise ValueError("output join must be inside the preview range")
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    duration_us = preview_end_us - preview_start_us
    boundary_relative_us = output_join_us - preview_start_us
    points: list[tuple[str, int, int]] = [("preview_start", preview_start_us - output_join_us, 0)]
    if boundary_relative_us > 0:
        points.append(("before_join", -250_000, max(0, boundary_relative_us - 250_000)))
    points.append(("at_join", 0, boundary_relative_us))
    if boundary_relative_us < duration_us:
        points.append(("after_join", 250_000, min(duration_us, boundary_relative_us + 250_000)))
    points.append(("preview_end", preview_end_us - output_join_us, duration_us))
    samples: list[dict[str, int | str]] = []
    seen_frames: set[int] = set()
    for label, offset_us, relative_us in points:
        frame_index = _nearest_frame(relative_us, duration_us, frame_count)
        if frame_index in seen_frames:
            continue
        seen_frames.add(frame_index)
        samples.append(
            {
                "label": label,
                "offset_us": offset_us,
                "preview_time_us": relative_us,
                "frame_index": frame_index,
            }
        )
    return samples


def _preview_ref(
    layout: ProjectLayout, item: Mapping[str, Any], item_index: int
) -> tuple[dict[str, Any], Path]:
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        raise PlanningValidationError(f"QA join item has no evidence: {item.get('item_id')}")
    for reference in evidence:
        if not isinstance(reference, Mapping):
            continue
        raw_path = reference.get("path")
        if isinstance(raw_path, str) and raw_path.casefold().endswith(".mp4"):
            selected = _validate_ref(layout, reference, f"join preview {item_index:06d}")
            return dict(reference), selected
    raise PlanningValidationError(f"QA join item has no MP4 preview: {item.get('item_id')}")


def _verify_cached_payload(layout: ProjectLayout, payload: Mapping[str, Any]) -> None:
    packet = payload.get("packet")
    candidate = payload.get("candidate")
    if not isinstance(packet, Mapping) or not isinstance(candidate, Mapping):
        raise StateConflictError("cached QA visual evidence is missing packet or candidate")
    _validate_ref(layout, packet, "cached QA review packet")
    _validate_ref(layout, candidate, "cached QA candidate", candidate_allowed=True)
    items = payload.get("items")
    if not isinstance(items, list):
        raise StateConflictError("cached QA visual evidence has no items")
    for item in items:
        if not isinstance(item, Mapping):
            raise StateConflictError("cached QA visual evidence item is invalid")
        for key in ("preview", "contact_sheet"):
            reference = item.get(key)
            if not isinstance(reference, Mapping):
                raise StateConflictError(f"cached QA visual evidence is missing {key}")
            _validate_ref(layout, reference, f"cached QA visual evidence {key}")


def _promote_stage(stage: Path, final: Path) -> None:
    if not stage.is_file() or stage.stat().st_size <= 0:
        raise PlanningValidationError(f"staged contact sheet is missing or empty: {stage}")
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.is_file():
        if sha256_file(final) == sha256_file(stage):
            stage.unlink()
            return
        raise StateConflictError(f"contact sheet exists with different bytes: {final}")
    os.replace(stage, final)


def _render_contact_sheet(
    adapter: ReviewVisualEvidenceAdapter,
    preview: Path,
    output: Path,
    samples: Sequence[Mapping[str, Any]],
) -> None:
    stage = output.with_name(f".{output.stem}.{uuid.uuid4().hex}{output.suffix}")
    try:
        adapter.make_contact_sheet(
            preview,
            stage,
            [int(item["frame_index"]) for item in samples],
            scale_width=360,
            tile_columns=len(samples),
        )
        _promote_stage(stage, output)
    except BaseException:
        if stage.is_file():
            failed = stage.with_name(f"{stage.name}.failed-{uuid.uuid4().hex}")
            os.replace(stage, failed)
        raise


def qa_review_visual_evidence_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# QA visual review evidence",
        "",
        f"- Project: `{payload['project_id']}`",
        f"- Revision: `{payload['revision_id']}`",
        f"- Packet: `{payload['packet']['sha256']}`",
        f"- Candidate: `{payload['candidate']['sha256']}`",
        f"- Contact sheets: {summary['contact_sheet_count']}/{summary['item_count']}",
        "",
        "Each sheet shows preview start, 250 ms before the join, the join boundary, "
        "250 ms after the join, and preview end from left to right when those distinct "
        "in-range frames exist. The sheet is visual "
        "evidence only; it does not classify or approve any finding.",
        "",
    ]
    for item in payload["items"]:
        samples = ", ".join(
            f"{sample['label']}=frame {sample['frame_index']}" for sample in item["samples"]
        )
        lines.extend(
            [
                f"## {item['item_id']} | {item['join_id']}",
                "",
                f"- Preview: `{item['preview']['path']}`",
                f"- Contact sheet: `{item['contact_sheet']['path']}`",
                f"- Samples: {samples}",
                "",
            ]
        )
    return "\n".join(lines)


def write_qa_review_visual_evidence(
    package_root: Path,
    layout: ProjectLayout,
    packet_path: Path,
    *,
    revision_id: str | None = None,
    adapter: ReviewVisualEvidenceAdapter | None = None,
    output_path: Path | None = None,
) -> Path:
    """Render deterministic, hash-bound contact sheets for current join warnings."""

    from videoedit.adapters.ffmpeg import FFmpegAdapter

    selected_packet_path = _owned_path(layout, packet_path, "QA review packet")
    if not selected_packet_path.is_file():
        raise PlanningValidationError(f"QA review packet does not exist: {selected_packet_path}")
    packet = _read_object(selected_packet_path, "QA review packet")
    validate_artifact(package_root, "qa_review_packet", packet)
    if packet.get("project_id") != layout.root.name:
        raise PlanningValidationError("QA review packet belongs to another project")
    packet_revision = str(packet["revision_id"])
    if revision_id is not None and packet_revision != revision_id:
        raise PlanningValidationError("QA review packet belongs to another revision")

    packet_ref = _file_ref(
        layout, selected_packet_path, str(packet["artifact_id"]), "QA review packet"
    )
    candidate_value = packet.get("candidate")
    if not isinstance(candidate_value, Mapping):
        raise PlanningValidationError("QA review packet has no candidate")
    candidate_path = _validate_ref(
        layout, candidate_value, "QA review packet candidate", candidate_allowed=True
    )
    candidate_ref = _candidate_ref(layout, candidate_path, str(candidate_value["artifact_id"]))

    raw_items = packet.get("items")
    if not isinstance(raw_items, list):
        raise PlanningValidationError("QA review packet has no items")
    join_items = [
        item for item in raw_items if isinstance(item, Mapping) and item.get("scope") == "join"
    ]
    if not join_items:
        raise PlanningValidationError("QA review packet has no join items for visual evidence")

    selected_adapter = adapter or FFmpegAdapter()
    prepared: list[dict[str, Any]] = []
    input_paths: list[tuple[str, Path]] = [
        (str(packet["artifact_id"]), selected_packet_path),
        (str(candidate_value["artifact_id"]), candidate_path),
    ]
    seen_preview_hashes: set[str] = set()
    for item_index, item in enumerate(join_items, start=1):
        preview_ref, preview_path = _preview_ref(layout, item, item_index)
        preview_hash = str(preview_ref["sha256"])
        if preview_hash not in seen_preview_hashes:
            input_paths.append((f"art_qa_join_preview_{item_index:06d}", preview_path))
            seen_preview_hashes.add(preview_hash)
        preview_range = item.get("time_range")
        if not isinstance(preview_range, Mapping):
            raise PlanningValidationError(f"join item has no preview range: {item['item_id']}")
        try:
            start_us = int(str(preview_range["start_us"]))
            end_us = int(str(preview_range["end_us"]))
            output_join_us = int(str(item["details"]["output_join_us"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningValidationError(
                f"join item has invalid visual evidence timing: {item['item_id']}"
            ) from exc
        frame_count = selected_adapter.probe_frame_count(preview_path)
        if frame_count is None or frame_count <= 0:
            raise PlanningValidationError(
                f"join preview has no decoded frame count: {preview_path}"
            )
        samples = sample_join_frames(start_us, end_us, output_join_us, frame_count)
        prepared.append(
            {
                "item_id": str(item["item_id"]),
                "join_id": str(item["details"]["join_id"]),
                "preview": preview_ref,
                "preview_path": preview_path,
                "preview_range": {"start_us": start_us, "end_us": end_us},
                "output_join_us": output_join_us,
                "frame_count": frame_count,
                "samples": samples,
            }
        )

    stage_key = make_stage_key(
        "qa-review-visual-evidence",
        IMPLEMENTATION_VERSION,
        [sha256_file(path) for _artifact_id, path in input_paths],
        {
            "project_id": layout.root.name,
            "revision_id": packet_revision,
            "packet_sha256": packet_ref["sha256"],
            "candidate_sha256": candidate_ref["sha256"],
            "sample_offsets_us": list(_BOUNDARY_OFFSETS_US),
        },
    )
    selected_output = (
        _owned_path(layout, output_path, "QA visual evidence output")
        if output_path is not None
        else layout.review / f"qa-review-visual-evidence-{stage_key[:16]}.json"
    )
    if selected_output in {
        selected_packet_path,
        candidate_path,
        *(item["preview_path"] for item in prepared),
    }:
        raise PlanningValidationError("QA visual evidence output cannot overwrite an input")

    with ProjectLock(
        layout, stage="qa_review_visual_evidence", revision_id=packet_revision
    ) as lock:
        if selected_output.is_file():
            current = _read_object(selected_output, "QA visual evidence")
            validate_artifact(package_root, "qa_review_visual_evidence", current)
            if (
                current.get("project_id") != layout.root.name
                or current.get("revision_id") != packet_revision
            ):
                raise StateConflictError(
                    "cached QA visual evidence belongs to another project or revision"
                )
            _verify_cached_payload(layout, current)
            return selected_output

        evidence_dir = layout.review / "qa-visual-evidence" / stage_key[:16]
        output_items: list[dict[str, Any]] = []
        for item_index, item in enumerate(prepared, start=1):
            output_sheet = evidence_dir / f"{item_index:03d}-{_safe_name(str(item['join_id']))}.png"
            _render_contact_sheet(
                selected_adapter,
                item["preview_path"],
                output_sheet,
                item["samples"],
            )
            output_items.append(
                {
                    "item_id": item["item_id"],
                    "join_id": item["join_id"],
                    "preview": item["preview"],
                    "contact_sheet": _file_ref(
                        layout,
                        output_sheet,
                        f"art_qa_join_contact_sheet_{item_index:06d}",
                        "QA join contact sheet",
                    ),
                    "preview_range": item["preview_range"],
                    "output_join_us": item["output_join_us"],
                    "frame_count": item["frame_count"],
                    "samples": item["samples"],
                }
            )
            lock.heartbeat()

        payload: dict[str, Any] = {
            "schema_name": "qa_review_visual_evidence",
            "schema_version": "1.0.0",
            "artifact_id": f"art_qa_review_visual_evidence_{stage_key[:16]}",
            "project_id": layout.root.name,
            "revision_id": packet_revision,
            "created_at": now_iso(),
            "producer": producer(
                "qa-review-visual-evidence", "ffmpeg-contact-sheet", IMPLEMENTATION_VERSION
            ),
            "inputs": [artifact_input(artifact_id, path) for artifact_id, path in input_paths],
            "packet": packet_ref,
            "candidate": candidate_ref,
            "status": "complete",
            "summary": {
                "item_count": len(output_items),
                "generated_count": len(output_items),
                "contact_sheet_count": len(output_items),
                "failed_count": 0,
            },
            "items": output_items,
        }
        write_validated_artifact(
            package_root, "qa_review_visual_evidence", selected_output, payload
        )
        write_text_atomically(
            selected_output.with_suffix(".md"), qa_review_visual_evidence_markdown(payload)
        )
    return selected_output


__all__ = [
    "IMPLEMENTATION_VERSION",
    "ReviewVisualEvidenceAdapter",
    "qa_review_visual_evidence_markdown",
    "sample_join_frames",
    "write_qa_review_visual_evidence",
]
