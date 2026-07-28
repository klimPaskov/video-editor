from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import (
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_review_package import _range, _verify_package_file_refs

SUPPORTED_MARKERS = {
    "FIX",
    "KEEP",
    "REMOVE",
    "RETIME",
    "MASK",
    "TEXT",
    "AUDIO",
    "ZOOM",
    "SPEED",
}
MARKER_LINE = re.compile(
    r"^\s*\[(?P<kind>[A-Z]+)(?:\s+(?P<range>[^\]]+))?\]\s*(?P<instruction>.*?)\s*$"
)
RANGE_VALUE = re.compile(r"^\s*(?P<start>\S+)\s*-\s*(?P<end>\S+)\s*$")
TIME_VALUE = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<unit>us|ms|s)?$")


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _time_value(value: str) -> int:
    token = value.strip()
    if ":" in token:
        parts = token.split(":")
        if len(parts) not in {2, 3}:
            raise PlanningValidationError(f"invalid timecode: {value}")
        try:
            decimals = [Decimal(part) for part in parts]
        except InvalidOperation as exc:
            raise PlanningValidationError(f"invalid timecode: {value}") from exc
        if any(item < 0 for item in decimals):
            raise PlanningValidationError(f"timecode cannot be negative: {value}")
        if len(decimals) == 2:
            minutes, seconds = decimals
            hours = Decimal(0)
        else:
            hours, minutes, seconds = decimals
        if minutes >= 60 or seconds >= 60:
            raise PlanningValidationError(f"timecode has invalid minute or second field: {value}")
        total_seconds = hours * Decimal(3600) + minutes * Decimal(60) + seconds
        return int((total_seconds * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
    match = TIME_VALUE.fullmatch(token.lower())
    if match is None:
        raise PlanningValidationError(f"invalid time value: {value}")
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation as exc:
        raise PlanningValidationError(f"invalid time value: {value}") from exc
    multiplier = {"us": Decimal(1), "ms": Decimal(1_000), "s": Decimal(1_000_000)}
    return int(
        (number * multiplier.get(match.group("unit") or "us", Decimal(1))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def _parse_range(value: str, segment_range: tuple[int, int]) -> dict[str, int]:
    match = RANGE_VALUE.fullmatch(value)
    if match is None:
        raise PlanningValidationError(
            "marker range must be written as START-END using microseconds, units, or timecodes"
        )
    start_us = _time_value(match.group("start"))
    end_us = _time_value(match.group("end"))
    if end_us <= start_us:
        raise PlanningValidationError("marker range must be a positive half-open range")
    if start_us < segment_range[0] or end_us > segment_range[1]:
        raise PlanningValidationError(
            f"marker range {start_us}:{end_us} is outside segment range "
            f"{segment_range[0]}:{segment_range[1]}"
        )
    return {"start_us": start_us, "end_us": end_us}


def parse_review_markdown(
    markdown: str,
    *,
    segment_range: tuple[int, int],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if "[" not in line:
            continue
        match = MARKER_LINE.fullmatch(line)
        if match is None:
            if re.search(r"\[[A-Z]+(?:\s|\])", line):
                raise PlanningValidationError(f"unparseable review marker on line {line_number}")
            continue
        kind = match.group("kind")
        if kind not in SUPPORTED_MARKERS:
            raise PlanningValidationError(
                f"unsupported review marker [{kind}] on line {line_number}"
            )
        instruction = " ".join(match.group("instruction").split())
        if not instruction:
            raise PlanningValidationError(f"review marker on line {line_number} has no instruction")
        range_text = match.group("range")
        if not range_text:
            raise PlanningValidationError(
                f"review marker [{kind}] on line {line_number} must include a range"
            )
        markers.append(
            {
                "marker_id": f"marker_{len(markers) + 1:06d}",
                "kind": kind,
                "instruction": instruction,
                "range_us": _parse_range(range_text, segment_range),
                "line_number": line_number,
                "raw": line,
            }
        )
    return markers


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def import_review_markers(
    package_root: Path,
    layout: ProjectLayout,
    markdown_path: Path,
    *,
    package_path: Path | None = None,
    output: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    markdown_file = _owned_path(layout, markdown_path, "review Markdown")
    if not markdown_file.is_file():
        raise PlanningValidationError(f"review Markdown does not exist: {markdown_file}")
    selected_package = (
        _owned_path(layout, package_path, "segment review package")
        if package_path
        else markdown_file.parent / "review-package.json"
    )
    if not selected_package.is_file():
        raise PlanningValidationError(f"segment review package does not exist: {selected_package}")
    with ProjectLock(layout, stage="review_marker_import", revision_id=revision_id):
        package = _read_object(selected_package, "segment review package")
        validate_artifact(package_root, "segment_review_package", package)
        if package["project_id"] != layout.root.name or package["revision_id"] != revision_id:
            raise PlanningValidationError("review package project or revision does not match")
        _verify_package_file_refs(layout, package, selected_package)
        source_range = _range(package.get("source_range"), "review package source range")
        if source_range is None:
            raise PlanningValidationError("review package has no source range")
        markdown_text = markdown_file.read_text(encoding="utf-8")
        markers = parse_review_markdown(markdown_text, segment_range=source_range)
        marker_key = canonical_sha256(
            {
                "package_sha256": sha256_file(selected_package),
                "markdown_sha256": sha256_file(markdown_file),
                "markers": markers,
            }
        )
        selected_output = _owned_path(
            layout,
            output or layout.artifacts / f"review-markers-{marker_key[:16]}.json",
            "review marker output",
        )
        if selected_output.is_file():
            previous = _read_object(selected_output, "review marker artifact")
            validate_artifact(package_root, "review_markers", previous)
            if previous.get("marker_key") != marker_key:
                raise StateConflictError(
                    f"review marker output already binds different Markdown: {selected_output}"
                )
            return selected_output
        payload: dict[str, Any] = {
            "schema_name": "review_markers",
            "schema_version": "1.0.0",
            "artifact_id": f"art_review_markers_{marker_key[:12]}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "segment_id": str(package["segment_id"]),
            "created_at": now_iso(),
            "producer": producer("review-marker-import", "markdown-parser", __version__),
            "marker_key": marker_key,
            "source_markdown": {
                "artifact_id": "review_markdown",
                "path": str(markdown_file),
                "sha256": sha256_file(markdown_file),
            },
            "source_package": {
                "artifact_id": str(package["artifact_id"]),
                "path": str(selected_package),
                "sha256": sha256_file(selected_package),
            },
            "bound_hashes": {
                "preview_sha256": package["preview"]["sha256"],
                "contact_sheet_sha256": package["contact_sheet"]["sha256"],
                "transcript_excerpt_sha256": package["transcript_excerpt"]["sha256"],
                "effect_summary_sha256": package["effect_summary"]["sha256"],
                "diagnostics_sha256": package["diagnostics"]["sha256"],
                "fixes_template_sha256": package["fixes_template"]["sha256"],
            },
            "markers": markers,
            "warnings": ["no_markers_found"] if not markers else [],
            "status": "warning" if not markers else "complete",
        }
        validate_artifact(package_root, "review_markers", payload)
        return write_validated_artifact(package_root, "review_markers", selected_output, payload)


__all__ = ["import_review_markers", "parse_review_markdown"]
