from __future__ import annotations

import json
import math
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from videoedit import __version__
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file


@dataclass(frozen=True, slots=True)
class CaptionPlanResult:
    plan_path: Path
    ass_path: Path
    webvtt_path: Path
    text_path: Path
    event_count: int
    warnings: tuple[str, ...]


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _read_yaml_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a mapping")
    return {str(key): item for key, item in value.items()}


def _nested_mapping(value: object, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        return {}
    return {str(item_key): item for item_key, item in nested.items()}


def _positive_int(value: object, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _bounded_percent(value: object, default: float, label: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(str(value)) / 100.0
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a percentage") from exc
    if not math.isfinite(parsed) or not 0 <= parsed < 1:
        raise ValueError(f"{label} must be between 0 and 100")
    return parsed


def _resolve_brand_path(
    package_root: Path,
    layout: ProjectLayout,
    project_config: Mapping[str, Any],
    explicit: Path | None,
) -> Path | None:
    if explicit is not None:
        candidate = explicit.expanduser()
        if not candidate.is_absolute():
            candidate = (package_root / candidate).resolve()
        return candidate
    captions = _nested_mapping(project_config, "captions")
    configured = captions.get("brand_profile")
    if not isinstance(configured, str) or not configured:
        return None
    candidates = [
        (layout.root / configured).resolve(),
        (package_root / configured).resolve(),
        (layout.config / Path(configured).name).resolve(),
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def _load_caption_settings(
    package_root: Path,
    layout: ProjectLayout,
    project_config: Mapping[str, Any],
    brand_path: Path | None,
) -> tuple[dict[str, Any], Path | None, list[str]]:
    warnings: list[str] = []
    project_captions = _nested_mapping(project_config, "captions")
    brand: dict[str, Any] = {}
    if brand_path is not None:
        if not brand_path.is_file():
            warnings.append(f"brand_profile_missing:{brand_path}")
        else:
            brand = _read_yaml_object(brand_path, "brand profile")
    elif not project_captions:
        warnings.append("caption_config_defaulted")
    brand_identity = _nested_mapping(brand, "brand")
    brand_caption = _nested_mapping(brand, "captions")
    brand_fonts = _nested_mapping(brand, "fonts")
    primary_font = _nested_mapping(brand_fonts, "caption_primary")
    family = str(primary_font.get("family") or "Arial")
    font_file = primary_font.get("file")
    if font_file:
        font_candidate = Path(str(font_file)).expanduser()
        if not font_candidate.is_absolute() and brand_path is not None:
            font_candidate = (brand_path.parent / font_candidate).resolve()
        if not font_candidate.is_file():
            warnings.append(f"caption_font_missing:{font_candidate}")
    else:
        warnings.append("caption_font_unconfigured:caption_primary")
    settings = {
        "brand_id": str(brand_identity.get("id") or "brand_default"),
        "brand_version": _positive_int(brand_identity.get("version"), 1, "brand version"),
        "font_family": family,
        "max_words": _positive_int(
            project_captions.get("maximum_words_per_phrase"), 5, "maximum_words_per_phrase"
        ),
        "max_lines": min(
            3,
            _positive_int(project_captions.get("maximum_lines"), 2, "maximum_lines"),
        ),
        "minimum_duration_us": _positive_int(
            project_captions.get("minimum_duration_ms"), 650, "minimum_duration_ms"
        )
        * 1000,
        "maximum_duration_us": _positive_int(
            project_captions.get("maximum_duration_ms"), 2800, "maximum_duration_ms"
        )
        * 1000,
        "maximum_width": _bounded_percent(
            brand_caption.get("maximum_width_percent"), 84.0, "maximum_width_percent"
        ),
        "safe_margin_x": _bounded_percent(
            brand_caption.get("safe_margin_x_percent"), 8.0, "safe_margin_x_percent"
        ),
        "safe_margin_y": _bounded_percent(
            brand_caption.get("safe_margin_y_percent"), 10.0, "safe_margin_y_percent"
        ),
        "emphasized_terms": [
            str(term).strip().casefold()
            for term in brand_caption.get("emphasized_terms", [])
            if str(term).strip()
        ]
        if isinstance(brand_caption.get("emphasized_terms", []), list)
        else [],
        "case": str(brand_caption.get("case") or "sentence"),
    }
    if int(str(settings["maximum_duration_us"])) < int(str(settings["minimum_duration_us"])):
        raise ValueError("maximum caption duration must not be shorter than minimum duration")
    if int(str(settings["max_lines"])) > 3:
        raise ValueError("caption maximum_lines must be at most 3")
    return settings, brand_path, warnings


def _word_values(
    transcript: Mapping[str, Any], duration_us: int, *, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
    raw_words = transcript.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("transcript words must be an array")
    words: list[dict[str, Any]] = []
    previous_start = -1
    previous_end = -1
    for value in raw_words:
        if not isinstance(value, Mapping):
            raise ValueError("transcript word must be an object")
        word = {str(key): item for key, item in value.items()}
        word_id = str(word.get("word_id", ""))
        text = str(word.get("text", "")).strip()
        start_us = word.get("start_us")
        end_us = word.get("end_us")
        if not word_id or not text or not isinstance(start_us, int) or not isinstance(end_us, int):
            raise ValueError("transcript words require word_id, text, and integer timing")
        if not 0 <= start_us < end_us <= duration_us:
            raise ValueError(f"transcript word is outside output duration: {word_id}")
        if start_us < previous_start:
            raise ValueError(f"transcript words are not non-overlapping: {word_id}")
        if start_us < previous_end:
            overlap_us = previous_end - start_us
            if overlap_us > 1_000 or previous_end >= end_us:
                raise ValueError(f"transcript words are not non-overlapping: {word_id}")
            start_us = previous_end
            if warnings is not None:
                warnings.append(f"caption_word_start_clamped:{word_id}:{overlap_us}us")
        words.append(
            {
                "word_id": word_id,
                "text": text,
                "start_us": start_us,
                "end_us": end_us,
            }
        )
        previous_start = start_us
        previous_end = end_us
    return words


def _group_words(
    words: Sequence[Mapping[str, Any]],
    *,
    max_words: int,
    maximum_duration_us: int,
) -> list[list[Mapping[str, Any]]]:
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    previous_end = 0
    for word in words:
        start_us = int(word["start_us"])
        end_us = int(word["end_us"])
        should_split = bool(current) and (
            len(current) >= max_words
            or end_us - int(current[0]["start_us"]) > maximum_duration_us
            or start_us - previous_end > 900_000
        )
        if should_split:
            groups.append(current)
            current = []
        current.append(word)
        previous_end = end_us
    if current:
        groups.append(current)
    return groups


def _balanced_lines(text: str, max_lines: int, width: int) -> list[str]:
    wrapped = textwrap.wrap(
        text,
        width=max(1, width),
        break_long_words=False,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    )
    if len(wrapped) <= max_lines:
        return wrapped or [text]
    words = text.split()
    lines: list[str] = []
    cursor = 0
    for line_index in range(max_lines):
        remaining_lines = max_lines - line_index
        remaining_words = len(words) - cursor
        take = max(1, math.ceil(remaining_words / remaining_lines))
        lines.append(" ".join(words[cursor : cursor + take]))
        cursor += take
    if cursor < len(words):
        lines[-1] = f"{lines[-1]} {' '.join(words[cursor:])}".strip()
    return lines


def _caption_lines(text: str, target_width: int, maximum_width: float, max_lines: int) -> list[str]:
    approximate_font_size = max(24, round(target_width * 0.04))
    character_width = max(1.0, approximate_font_size * 0.55)
    width = max(12, math.floor(target_width * maximum_width / character_width))
    return _balanced_lines(text, max_lines, width)


def _emphasis_spans(text: str, terms: Sequence[str]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for term in sorted(
        {item.casefold() for item in terms if item}, key=lambda item: (len(item), item)
    ):
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        spans.extend(
            {
                "start_char": match.start(),
                "end_char": match.end(),
                "style_token": "emphasis",
            }
            for match in pattern.finditer(text)
        )
    return sorted(spans, key=lambda span: (int(span["start_char"]), int(span["end_char"])))


def _event_end_us(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    index: int,
    duration_us: int,
    minimum_duration_us: int,
) -> int:
    group = groups[index]
    start_us = int(group[0]["start_us"])
    end_us = max(int(word["end_us"]) for word in group)
    end_us = min(duration_us, max(end_us, start_us + minimum_duration_us))
    if index + 1 < len(groups):
        next_start = int(groups[index + 1][0]["start_us"])
        if next_start > start_us:
            end_us = min(end_us, next_start)
    return max(start_us + 1, end_us)


def _format_ass_time(value_us: int) -> str:
    centiseconds = max(0, value_us // 10_000)
    seconds, fraction = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def _format_vtt_time(value_us: int) -> str:
    milliseconds = max(0, value_us // 1_000)
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"


def _escape_ass(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _file_ref(layout: ProjectLayout, path: Path) -> dict[str, Any]:
    relative = path.resolve().relative_to(layout.root.resolve()).as_posix()
    return {"path": relative, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def build_caption_plan(
    package_root: Path,
    layout: ProjectLayout,
    transcript_path: Path,
    render_manifest_path: Path,
    *,
    brand_path: Path | None = None,
    revision_id: str = "rev_001",
) -> CaptionPlanResult:
    transcript_path = transcript_path.expanduser().resolve()
    render_manifest_path = render_manifest_path.expanduser().resolve()
    transcript = _read_json_object(transcript_path, "transcript")
    manifest = _read_json_object(render_manifest_path, "render manifest")
    validate_artifact(package_root, "transcript", transcript)
    validate_artifact(package_root, "render_manifest", manifest)
    if (
        transcript.get("project_id") != layout.root.name
        or manifest.get("project_id") != layout.root.name
    ):
        raise ValueError("caption inputs must belong to the project")
    if transcript.get("revision_id") != revision_id or manifest.get("revision_id") != revision_id:
        raise ValueError("caption inputs must belong to the requested revision")
    duration_value = transcript.get("output_duration_us", transcript.get("source_duration_us"))
    if not isinstance(duration_value, int) or duration_value <= 0:
        raise ValueError("transcript has no positive output duration")
    video = manifest.get("video")
    if not isinstance(video, Mapping):
        raise ValueError("render manifest video metadata is missing")
    width = video.get("width")
    height = video.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("render manifest dimensions are invalid")
    project_config_path = layout.config / "project.yaml"
    project_config = (
        _read_yaml_object(project_config_path, "project configuration")
        if project_config_path.is_file()
        else {}
    )
    selected_brand_path = _resolve_brand_path(package_root, layout, project_config, brand_path)
    settings, selected_brand_path, warnings = _load_caption_settings(
        package_root, layout, project_config, selected_brand_path
    )
    words = _word_values(transcript, duration_value, warnings=warnings)
    groups = _group_words(
        words,
        max_words=int(settings["max_words"]),
        maximum_duration_us=int(settings["maximum_duration_us"]),
    )
    events: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        text = " ".join(str(word["text"]) for word in group).strip()
        if settings["case"] == "sentence" and text:
            text = text[0].upper() + text[1:]
        start_us = int(group[0]["start_us"])
        end_us = _event_end_us(
            groups, index - 1, duration_value, int(settings["minimum_duration_us"])
        )
        events.append(
            {
                "caption_id": f"cap_{index:06d}",
                "start_us": start_us,
                "end_us": end_us,
                "text": text,
                "lines": _caption_lines(
                    text,
                    int(width),
                    float(settings["maximum_width"]),
                    int(settings["max_lines"]),
                ),
                "word_ids": [str(word["word_id"]) for word in group],
                "style_id": "sty_caption_default",
                "region": "bottom",
                "emphasis": _emphasis_spans(text, settings["emphasized_terms"]),
            }
        )
    input_values = [
        artifact_input("art_transcript", transcript_path),
        artifact_input("art_render_manifest", render_manifest_path),
    ]
    if selected_brand_path is not None and selected_brand_path.is_file():
        input_values.append(artifact_input("art_brand_config", selected_brand_path))
    key = canonical_sha256(
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "inputs": input_values,
            "config_sha256": config_sha256(layout),
            "settings": settings,
            "events": events,
            "implementation": __version__,
        }
    )
    output_root = layout.artifacts / "captions" / key
    ass_path = output_root / "captions.ass"
    webvtt_path = output_root / "captions.vtt"
    text_path = output_root / "captions.txt"
    ass_events = "\n".join(
        "Dialogue: 0,{start},{end},Default,,0,0,0,,{text}".format(
            start=_format_ass_time(int(event["start_us"])),
            end=_format_ass_time(int(event["end_us"])),
            text=_escape_ass("\\N".join(str(line) for line in event["lines"])),
        )
        for event in events
    )
    ass_text = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        "Style: Default,{font},42,&H00FFFFFF,&H000000FF,"
        "&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,60,1"
        "\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n{events}\n"
    ).format(
        width=width,
        height=height,
        font=str(settings["font_family"]),
        events=ass_events,
    )
    vtt_cues = "\n\n".join(
        f"{event['caption_id']}\n{_format_vtt_time(int(event['start_us']))} --> "
        f"{_format_vtt_time(int(event['end_us']))}\n"
        + "\n".join(str(line) for line in event["lines"])
        for event in events
    )
    webvtt_text = f"WEBVTT\n\n{vtt_cues}\n" if vtt_cues else "WEBVTT\n"
    text_output = "\n".join(
        f"{_format_vtt_time(int(event['start_us']))} {event['text']}" for event in events
    )
    write_text_atomically(ass_path, ass_text)
    write_text_atomically(webvtt_path, webvtt_text)
    write_text_atomically(text_path, text_output + ("\n" if text_output else ""))
    payload: dict[str, Any] = {
        "schema_name": "caption_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_caption_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("caption-plan", "local-word-caption-planner", __version__),
        "inputs": input_values,
        "config_sha256": config_sha256(layout),
        "target_width": width,
        "target_height": height,
        "brand_id": str(settings["brand_id"]),
        "brand_version": int(settings["brand_version"]),
        "safe_area": {
            "left": float(settings["safe_margin_x"]),
            "right": float(settings["safe_margin_x"]),
            "top": float(settings["safe_margin_y"]),
            "bottom": float(settings["safe_margin_y"]),
        },
        "events": events,
        "outputs": {
            "ass": _file_ref(layout, ass_path),
            "webvtt": _file_ref(layout, webvtt_path),
            "text": _file_ref(layout, text_path),
        },
        "warnings": list(dict.fromkeys(warnings)),
    }
    plan_path = layout.artifacts / f"caption-plan-{key}.json"
    write_validated_artifact(package_root, "caption_plan", plan_path, payload)
    write_validated_artifact(
        package_root, "caption_plan", layout.artifacts / "caption-plan.json", payload
    )
    return CaptionPlanResult(
        plan_path, ass_path, webvtt_path, text_path, len(events), tuple(warnings)
    )
