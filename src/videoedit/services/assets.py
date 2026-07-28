from __future__ import annotations

import json
import mimetypes
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import now_iso, validate_artifact, write_validated_artifact
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, sha256_file

ASSET_INDEX_IMPLEMENTATION_VERSION = f"{__version__}:asset-index-v1"
PROBED_ASSET_TYPES = frozenset(
    {
        "image",
        "background",
        "replacement_object",
        "broll",
        "sound_effect",
        "music",
        "logo",
        "intro",
        "outro",
    }
)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object")
    return value


def _owned(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} escapes the project: {resolved}") from exc
    return resolved


def _first_stream(probe: Mapping[str, Any], stream_type: str) -> Mapping[str, Any] | None:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if isinstance(stream, Mapping) and stream.get("codec_type") == stream_type:
            return stream
    return None


def _media_properties(
    path: Path,
    asset_type: str,
    existing: Mapping[str, Any],
    adapter: FFmpegAdapter,
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(path.name)[0] or existing.get("mime_type")
    values: dict[str, Any] = {
        "mime_type": mime_type,
        "width": existing.get("width"),
        "height": existing.get("height"),
        "duration_us": existing.get("duration_us"),
    }
    if asset_type == "font":
        return values
    try:
        probe = adapter.probe(path)
    except Exception as exc:
        raise PlanningValidationError(f"could not probe local asset {path}: {exc}") from exc
    video = _first_stream(probe, "video")
    audio = _first_stream(probe, "audio")
    selected = video or audio
    if selected is None:
        raise PlanningValidationError(f"local asset has no media stream: {path}")
    if video is not None:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if width <= 0 or height <= 0:
            raise PlanningValidationError(f"local visual asset has invalid dimensions: {path}")
        values["width"] = width
        values["height"] = height
    format_payload = probe.get("format")
    duration_value = selected.get("duration")
    if duration_value in (None, "N/A", "") and isinstance(format_payload, Mapping):
        duration_value = format_payload.get("duration")
    duration_us = seconds_to_us(duration_value)
    if duration_us is not None:
        values["duration_us"] = duration_us
    elif asset_type in {"broll", "sound_effect", "music", "intro", "outro"}:
        raise PlanningValidationError(f"local timed asset has no duration: {path}")
    if video is not None:
        rate = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
        if rate is None and asset_type in {"broll", "background", "intro", "outro"}:
            raise PlanningValidationError(f"local video asset has no rational frame rate: {path}")
    return values


def index_local_asset_catalog(
    package_root: Path,
    layout: ProjectLayout,
    asset_root: Path,
    metadata_path: Path,
    output_path: Path | None = None,
    *,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Hash and probe a licensed local asset catalog without inferring licence rights."""

    root = _owned(layout, asset_root, "asset root")
    if not root.is_dir():
        raise PlanningValidationError(f"asset root does not exist: {root}")
    metadata = _owned(layout, metadata_path, "asset catalog metadata")
    catalog = _read_object(metadata, "asset catalog metadata")
    validate_artifact(package_root, "asset_catalog", catalog)
    catalog_root = Path(str(catalog["root_path"])).expanduser()
    if not catalog_root.is_absolute():
        catalog_root = (metadata.parent / catalog_root).resolve()
    else:
        catalog_root = catalog_root.resolve()
    if catalog_root != root:
        raise PlanningValidationError("asset catalog root_path does not match the indexed root")
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    selected_adapter = adapter or FFmpegAdapter()
    indexed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            raise PlanningValidationError("asset catalog entry must be an object")
        asset_id = str(raw_asset.get("asset_id", ""))
        asset_type = str(raw_asset.get("asset_type", ""))
        if not asset_id or asset_id in seen_ids:
            raise PlanningValidationError(f"asset catalog contains duplicate asset id: {asset_id}")
        if asset_type not in {
            "font",
            "image",
            "background",
            "replacement_object",
            "broll",
            "sound_effect",
            "music",
            "logo",
            "intro",
            "outro",
        }:
            raise PlanningValidationError(f"asset has unsupported type: {asset_type}")
        file_value = raw_asset.get("file")
        if not isinstance(file_value, Mapping) or not isinstance(file_value.get("path"), str):
            raise PlanningValidationError(f"asset file reference is missing: {asset_id}")
        candidate = Path(str(file_value["path"])).expanduser()
        file_path = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        try:
            file_path.relative_to(root)
        except ValueError as exc:
            raise PlanningValidationError(f"asset escapes the indexed root: {file_path}") from exc
        if not file_path.is_file():
            raise PlanningValidationError(f"asset file does not exist: {file_path}")
        if file_path in seen_paths:
            raise PlanningValidationError(f"asset catalog contains duplicate file: {file_path}")
        seen_ids.add(asset_id)
        seen_paths.add(file_path)
        properties = _media_properties(file_path, asset_type, file_value, selected_adapter)
        indexed_file = {
            "path": file_path.relative_to(root).as_posix(),
            "sha256": sha256_file(file_path),
            "size_bytes": file_path.stat().st_size,
            **properties,
        }
        indexed.append({**dict(raw_asset), "file": indexed_file})
    indexed.sort(key=lambda item: str(item["asset_id"]))
    output = _owned(
        layout,
        output_path or layout.work / "asset-catalog-indexed.json",
        "asset catalog output",
    )
    payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": catalog["catalog_id"],
        "created_at": catalog["created_at"],
        "updated_at": now_iso(),
        "root_path": str(root),
        "assets": indexed,
    }
    if output.is_file():
        existing = _read_object(output, "existing asset catalog")
        validate_artifact(package_root, "asset_catalog", existing)
        comparable = dict(payload)
        comparable["updated_at"] = existing["updated_at"]
        if existing == comparable:
            return output
    validate_artifact(package_root, "asset_catalog", payload)
    return write_validated_artifact(package_root, "asset_catalog", output, payload)
