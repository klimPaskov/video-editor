from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import gcd
from pathlib import Path
from typing import Any

import yaml

from videoedit import __version__
from videoedit.errors import ApprovalRequiredError, PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.transition_sound import write_transition_sound_plan
from videoedit.services.transitions import MOTION_TRANSITION_TYPES

CUE_PLANNING_IMPLEMENTATION_VERSION = f"{__version__}:cue-planning-v1"
_ALLOWED_BROLL_TYPES = frozenset({"broll", "image", "background"})
_MOTION_CUE_TYPES = frozenset(
    {
        "key_point",
        "counter",
        "comparison",
        "definition",
        "chapter",
        "quote",
        "highlight",
    }
)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object: {path}")
    return value


def _owned(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} must be inside the project: {resolved}") from exc
    return resolved


def _int(value: object, label: str, default: int | None = None) -> int:
    if isinstance(value, bool):
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be an integer") from exc


def _number(value: object, label: str, default: float | None = None) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be finite")
    return result


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanningValidationError(f"{label} must be an object")
    return value


def _range(value: Mapping[str, Any], label: str) -> tuple[int, int]:
    start_us = _int(value.get("start_us"), f"{label} start")
    end_us = _int(value.get("end_us"), f"{label} end")
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError(f"{label} must be a positive half-open range")
    return start_us, end_us


def _read_yaml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    return _mapping(value, f"{label} YAML")


def _policy_value(root: Mapping[str, Any], section: str, key: str, default: object) -> object:
    value = root.get(section, {})
    if not isinstance(value, Mapping):
        return default
    return value.get(key, default)


class CuePlacementPolicy:
    def __init__(
        self,
        *,
        maximum_broll_coverage_percent: float = 20.0,
        minimum_broll_spacing_us: int = 15_000_000,
        maximum_same_asset_uses_per_project: int = 1,
        maximum_motion_cues_per_minute: int = 1,
        minimum_motion_spacing_us: int = 12_000_000,
        maximum_sound_cues_per_minute: int = 3,
        currency: str = "USD",
        reserve_percent: int = 20,
    ) -> None:
        self.maximum_broll_coverage_percent = maximum_broll_coverage_percent
        self.minimum_broll_spacing_us = minimum_broll_spacing_us
        self.maximum_same_asset_uses_per_project = maximum_same_asset_uses_per_project
        self.maximum_motion_cues_per_minute = maximum_motion_cues_per_minute
        self.minimum_motion_spacing_us = minimum_motion_spacing_us
        self.maximum_sound_cues_per_minute = maximum_sound_cues_per_minute
        self.currency = currency
        self.reserve_percent = reserve_percent
        if not 0 <= maximum_broll_coverage_percent <= 100:
            raise ValueError("maximum B-roll coverage must be between zero and one hundred")
        if minimum_broll_spacing_us < 0 or minimum_motion_spacing_us < 0:
            raise ValueError("cue spacing must be nonnegative")
        if maximum_same_asset_uses_per_project < 1:
            raise ValueError("maximum same-asset uses must be positive")
        if maximum_motion_cues_per_minute < 0 or maximum_sound_cues_per_minute < 0:
            raise ValueError("cue frequency limits must be nonnegative")
        if not len(currency) == 3 or not currency.isupper() or not currency.isalpha():
            raise ValueError("currency must be an uppercase ISO code")
        if not 0 <= reserve_percent <= 1000:
            raise ValueError("reserve percent must be between zero and one thousand")

    @classmethod
    def from_yaml(cls, assets_path: Path, transitions_path: Path) -> CuePlacementPolicy:
        assets = _read_yaml(assets_path, "asset selection policy")
        transitions = _read_yaml(transitions_path, "transition policy")
        selection = _mapping(assets.get("selection", {}), "asset selection policy selection")
        transition_policy = _mapping(
            transitions.get("transition_policy", {}), "transition policy transition_policy"
        )
        budget = assets.get("budget", {})
        budget_mapping = budget if isinstance(budget, Mapping) else {}
        sound = assets.get("sound", {})
        sound_mapping = sound if isinstance(sound, Mapping) else {}
        maximum_sound = selection.get(
            "maximum_sound_cues_per_minute",
            sound_mapping.get("maximum_cues_per_minute", 3),
        )
        return cls(
            maximum_broll_coverage_percent=_number(
                selection.get("maximum_broll_coverage_percent", 20),
                "maximum B-roll coverage",
            ),
            minimum_broll_spacing_us=round(
                _number(selection.get("minimum_broll_spacing_seconds", 15), "B-roll spacing")
                * 1_000_000
            ),
            maximum_same_asset_uses_per_project=_int(
                selection.get("maximum_same_asset_uses_per_project", 1),
                "maximum same-asset uses",
            ),
            maximum_motion_cues_per_minute=_int(
                transition_policy.get("maximum_motion_transitions_per_minute", 1),
                "maximum motion cues",
            ),
            minimum_motion_spacing_us=round(
                _number(
                    transition_policy.get("minimum_motion_transition_spacing_seconds", 12),
                    "motion spacing",
                )
                * 1_000_000
            ),
            maximum_sound_cues_per_minute=_int(maximum_sound, "maximum sound cues"),
            currency=str(budget_mapping.get("currency", "USD")),
            reserve_percent=_int(budget_mapping.get("reserve_percent", 20), "reserve percent"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_broll_coverage_percent": self.maximum_broll_coverage_percent,
            "minimum_broll_spacing_us": self.minimum_broll_spacing_us,
            "maximum_same_asset_uses_per_project": self.maximum_same_asset_uses_per_project,
            "maximum_motion_cues_per_minute": self.maximum_motion_cues_per_minute,
            "minimum_motion_spacing_us": self.minimum_motion_spacing_us,
            "maximum_sound_cues_per_minute": self.maximum_sound_cues_per_minute,
        }


def _project_video_settings(layout: ProjectLayout) -> tuple[int, int, dict[str, int]]:
    path = layout.config / "project.yaml"
    value = _read_yaml(path, "project configuration") if path.is_file() else {}
    width = _int(value.get("width", 1920), "project width")
    height = _int(value.get("height", 1080), "project height")
    fps = value.get("fps", 30)
    if isinstance(fps, Mapping):
        numerator = _int(fps.get("numerator"), "project frame rate numerator")
        denominator = _int(fps.get("denominator"), "project frame rate denominator")
    elif isinstance(fps, str) and "/" in fps:
        numerator_text, denominator_text = fps.split("/", 1)
        numerator = _int(numerator_text, "project frame rate numerator")
        denominator = _int(denominator_text, "project frame rate denominator")
    else:
        numerator = _int(fps, "project frame rate")
        denominator = 1
    if width < 1 or height < 1 or numerator < 1 or denominator < 1:
        raise PlanningValidationError("project video dimensions and frame rate must be positive")
    return width, height, {"numerator": numerator, "denominator": denominator}


def _aspect_ratio(width: int, height: int) -> str:
    factor = gcd(width, height)
    return f"{width // factor}:{height // factor}"


def _catalog_root(catalog_path: Path, catalog: Mapping[str, Any]) -> Path:
    root_value = Path(str(catalog.get("root_path", ""))).expanduser()
    if not str(root_value):
        raise PlanningValidationError("asset catalog root_path is missing")
    return (
        root_value.resolve()
        if root_value.is_absolute()
        else (catalog_path.parent / root_value).resolve()
    )


def _read_catalog(
    package_root: Path, layout: ProjectLayout, catalog_path: Path
) -> tuple[dict[str, Any], Path, str, dict[str, Mapping[str, Any]]]:
    catalog = _read_object(catalog_path, "asset catalog")
    validate_artifact(package_root, "asset_catalog", catalog)
    root = _catalog_root(catalog_path, catalog)
    try:
        root.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"asset catalog root escapes the project: {root}") from exc
    if not root.is_dir():
        raise PlanningValidationError(f"asset catalog root does not exist: {root}")
    raw_assets = catalog.get("assets")
    if not isinstance(raw_assets, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    assets: dict[str, Mapping[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, Mapping):
            raise PlanningValidationError("asset catalog entry must be an object")
        asset_id = str(raw_asset.get("asset_id", ""))
        if not asset_id or asset_id in assets:
            raise PlanningValidationError(
                f"asset catalog has invalid or duplicate asset: {asset_id}"
            )
        assets[asset_id] = raw_asset
    return catalog, root, sha256_file(catalog_path), assets


def _asset_file(
    root: Path, asset: Mapping[str, Any], *, asset_id: str
) -> tuple[Path, Mapping[str, Any]]:
    file_value = asset.get("file")
    if not isinstance(file_value, Mapping):
        raise PlanningValidationError(f"asset file reference is missing: {asset_id}")
    candidate = Path(str(file_value.get("path", ""))).expanduser()
    path = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PlanningValidationError(f"asset path escapes catalog root: {asset_id}") from exc
    if not path.is_file():
        raise PlanningValidationError(f"asset file is missing: {path}")
    if str(file_value.get("sha256", "")) != sha256_file(path):
        raise PlanningValidationError(f"asset file hash is stale: {asset_id}")
    if _int(file_value.get("size_bytes"), "asset size", -1) != path.stat().st_size:
        raise PlanningValidationError(f"asset file size is stale: {asset_id}")
    license_id = str(asset.get("licence_reference", "")).strip()
    if not license_id:
        raise PlanningValidationError(f"asset licence reference is missing: {asset_id}")
    if not isinstance(asset.get("permitted_uses"), list) or not asset["permitted_uses"]:
        raise PlanningValidationError(f"asset permitted_uses is missing: {asset_id}")
    return path, file_value


def _read_transition_plan(
    package_root: Path, layout: ProjectLayout, transition_path: Path, revision_id: str
) -> dict[str, Any]:
    transition_plan = _read_object(transition_path, "transition plan")
    validate_artifact(package_root, "transition_plan", transition_plan)
    if (
        transition_plan.get("project_id") != layout.root.name
        or transition_plan.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("transition plan project or revision does not match")
    if not isinstance(transition_plan.get("transitions"), list):
        raise PlanningValidationError("transition plan transitions must be an array")
    return transition_plan


def _read_search_result(
    package_root: Path,
    layout: ProjectLayout,
    search_path: Path,
    revision_id: str,
    catalog_hash: str,
) -> dict[str, Any]:
    result = _read_object(search_path, "asset search result")
    validate_artifact(package_root, "asset_search_result", result)
    if result.get("project_id") != layout.root.name or result.get("revision_id") != revision_id:
        raise PlanningValidationError("asset search result project or revision does not match")
    if result.get("catalog_sha256") != catalog_hash:
        raise StaleApprovalError("asset search result is stale for the current asset catalog")
    if not isinstance(result.get("results"), list):
        raise PlanningValidationError("asset search result results must be an array")
    return result


def _motion_cue_type(purpose: str) -> str:
    return {
        "new_point": "key_point",
        "new_chapter": "chapter",
        "mode_change": "definition",
        "comparison": "comparison",
        "before_after": "comparison",
        "location_change": "chapter",
        "return_from_visual_explanation": "highlight",
    }.get(purpose, "highlight")


def _build_motion_plan(
    package_root: Path,
    layout: ProjectLayout,
    transition_plan_path: Path,
    transition_plan: Mapping[str, Any],
    transitions_policy_path: Path,
    *,
    revision_id: str,
    policy: CuePlacementPolicy,
) -> tuple[dict[str, Any], list[str]]:
    width, height, frame_rate = _project_video_settings(layout)
    raw_transitions = transition_plan.get("transitions", [])
    if not isinstance(raw_transitions, list):
        raise PlanningValidationError("transition plan transitions must be an array")
    cues: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw_transition in sorted(
        (item for item in raw_transitions if isinstance(item, Mapping)),
        key=lambda item: (
            _int(item.get("range", {}).get("start_us", 0), "transition start", 0),
            str(item.get("transition_id", "")),
        ),
    ):
        transition_type = str(raw_transition.get("transition_type", ""))
        transition_id = str(raw_transition.get("transition_id", ""))
        if transition_type not in MOTION_TRANSITION_TYPES:
            continue
        range_value = raw_transition.get("range")
        if not isinstance(range_value, Mapping):
            warnings.append(f"motion_invalid_range:{transition_id}")
            continue
        try:
            start_us, end_us = _range(range_value, f"motion transition {transition_id}")
        except PlanningValidationError:
            warnings.append(f"motion_invalid_range:{transition_id}")
            continue
        if any(
            start_us < _int(cue["end_us"], "motion cue end")
            and end_us > _int(cue["start_us"], "motion cue start")
            for cue in cues
        ):
            warnings.append(f"motion_collision:{transition_id}")
            continue
        if any(
            start_us - _int(cue["start_us"], "motion cue start") < policy.minimum_motion_spacing_us
            for cue in cues
        ):
            warnings.append(f"motion_spacing_below_minimum:{transition_id}")
            continue
        recent = [
            cue for cue in cues if start_us - _int(cue["start_us"], "motion cue start") < 60_000_000
        ]
        if len(recent) >= policy.maximum_motion_cues_per_minute:
            warnings.append(f"motion_frequency_limit:{transition_id}")
            continue
        purpose = str(raw_transition.get("purpose", ""))
        template_suffix = transition_type.replace("-", "_")
        cues.append(
            {
                "cue_id": f"mot_{transition_id}",
                "cue_type": _motion_cue_type(purpose),
                "start_us": start_us,
                "end_us": end_us,
                "template_id": f"tpl_transition_{template_suffix}",
                "payload": {
                    "transition_id": transition_id,
                    "purpose": purpose,
                    "direction": str(raw_transition.get("direction", "none")),
                    "easing": str(raw_transition.get("easing", "smooth_ease_in_out")),
                    "confidence": raw_transition.get("confidence"),
                },
                "region": "full",
                "z_index": 10,
                "rationale": str(
                    raw_transition.get("reason", "Reinforce the verified structural change")
                ),
                "approval_state": "proposed",
                "fallback_template_id": "tpl_clean_cut",
            }
        )
    if cues:
        warnings.append("motion_cue_approval_required")
    payload: dict[str, Any] = {
        "schema_name": "motion_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_motion_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "cue-planning", "licensed-local-catalog", CUE_PLANNING_IMPLEMENTATION_VERSION
        ),
        "inputs": [
            artifact_input(str(transition_plan["artifact_id"]), transition_plan_path),
            artifact_input("art_transitions_policy", transitions_policy_path),
        ],
        "config_sha256": config_sha256(layout),
        "renderer": "local",
        "target_width": width,
        "target_height": height,
        "frame_rate": frame_rate,
        "cues": cues,
        "warnings": warnings,
    }
    validate_artifact(package_root, "motion_plan", payload)
    return payload, warnings


def _search_candidates(
    search_result: Mapping[str, Any] | None,
    catalog_assets: Mapping[str, Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    if search_result is None:
        return []
    raw_results = search_result.get("results", [])
    if not isinstance(raw_results, list):
        return []
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            continue
        asset_id = str(raw_result.get("asset_id", ""))
        asset = catalog_assets.get(asset_id)
        if asset is None or asset.get("asset_type") not in _ALLOWED_BROLL_TYPES:
            continue
        if raw_result.get("asset_sha256") != asset.get("file", {}).get("sha256"):
            continue
        if raw_result.get("licence_reference") != asset.get("licence_reference"):
            continue
        candidates.append((raw_result, asset))
    return candidates


def _project_usage_count(asset: Mapping[str, Any], project_id: str) -> int:
    history = asset.get("usage_history", [])
    if not isinstance(history, list):
        return 0
    return sum(
        1 for item in history if isinstance(item, Mapping) and item.get("project_id") == project_id
    )


def _build_broll_plan(
    package_root: Path,
    layout: ProjectLayout,
    catalog_path: Path,
    catalog: Mapping[str, Any],
    catalog_root: Path,
    search_path: Path | None,
    search_result: Mapping[str, Any] | None,
    assets_policy_path: Path,
    *,
    revision_id: str,
    timeline_duration_us: int,
    broll_windows: Sequence[Mapping[str, object]],
    motion_cues: Sequence[Mapping[str, object]],
    policy: CuePlacementPolicy,
) -> tuple[dict[str, Any], list[str]]:
    width, height, _frame_rate = _project_video_settings(layout)
    _ = (width, height)
    warnings: list[str] = []
    raw_assets = catalog.get("assets", [])
    catalog_assets = {
        str(item.get("asset_id")): item
        for item in raw_assets
        if isinstance(item, Mapping) and item.get("asset_id")
    }
    candidates = _search_candidates(search_result, catalog_assets)
    requests: list[dict[str, Any]] = []
    accepted_starts: list[int] = []
    coverage_us = 0
    selected_asset_counts: dict[str, int] = {}
    if search_result is None:
        warnings.append("broll_search_result_missing")
    if not broll_windows and candidates:
        warnings.append("broll_window_missing")
    if broll_windows and not candidates:
        warnings.append("broll_no_eligible_local_asset")
    for index, window in enumerate(
        sorted(
            broll_windows,
            key=lambda item: (
                _int(item.get("start_us"), "B-roll start"),
                str(item.get("request_id", "")),
            ),
        ),
        start=1,
    ):
        if not candidates:
            break
        try:
            start_us, end_us = _range(window, f"B-roll window {index}")
        except PlanningValidationError:
            warnings.append(f"broll_invalid_range:{index}")
            continue
        if end_us > timeline_duration_us:
            warnings.append(f"broll_out_of_bounds:{index}")
            continue
        if any(
            start_us < _int(cue.get("end_us"), "motion cue end")
            and end_us > _int(cue.get("start_us"), "motion cue start")
            for cue in motion_cues
        ):
            warnings.append(f"broll_collision_with_motion:{index}")
            continue
        if any(
            start_us - previous < policy.minimum_broll_spacing_us for previous in accepted_starts
        ):
            warnings.append(f"broll_spacing_below_minimum:{index}")
            continue
        duration_us = end_us - start_us
        proposed_coverage_percent = (coverage_us + duration_us) * 100 / timeline_duration_us
        if proposed_coverage_percent > policy.maximum_broll_coverage_percent:
            warnings.append(f"broll_coverage_limit:{index}")
            continue
        selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
        for candidate in candidates:
            asset_id = str(candidate[1].get("asset_id", ""))
            if (
                _project_usage_count(candidate[1], layout.root.name)
                + selected_asset_counts.get(asset_id, 0)
                < policy.maximum_same_asset_uses_per_project
            ):
                selected = candidate
                break
        if selected is None:
            warnings.append(f"broll_asset_reuse_limit:{index}")
            continue
        result, asset = selected
        asset_id = str(asset["asset_id"])
        _asset_file(catalog_root, asset, asset_id=asset_id)
        description = str(asset.get("description", result.get("description", "local asset")))
        transcript_context = (
            str(window.get("transcript_context", "")).strip()
            or str(search_result.get("query", "") if search_result else "").strip()
        )
        if not transcript_context:
            transcript_context = description
        rationale = str(window.get("rationale", "")).strip() or (
            f"Use the reviewed local asset because it matches the transcript context: "
            f"{transcript_context}"
        )
        request_id = str(window.get("request_id", f"brr_{index:03d}"))
        request = {
            "request_id": request_id,
            "start_us": start_us,
            "end_us": end_us,
            "transcript_context": transcript_context,
            "rationale": rationale,
            "mode": "licensed_footage",
            "prompt": description,
            "negative_constraints": [
                "No unlicensed media",
                "No unverifiable people, logos, or factual claims",
                "No unreadable text",
            ],
            "duration_us": duration_us,
            "aspect_ratio": _aspect_ratio(width, height),
            "factual_sensitivity": "medium" if asset.get("sensitive_content") else "low",
            "fallback": "base_video",
            "asset_id": asset_id,
            "asset_sha256": asset["file"]["sha256"],
            "license_id": asset["licence_reference"],
            "collision_group": "picture_full_frame",
            "minimum_spacing_us": policy.minimum_broll_spacing_us,
            "provider": None,
            "model": None,
            "estimated_cost": None,
            "approval_state": "proposed",
        }
        requests.append(request)
        accepted_starts.append(start_us)
        coverage_us += duration_us
        selected_asset_counts[asset_id] = selected_asset_counts.get(asset_id, 0) + 1
    if requests:
        warnings.append("broll_cue_approval_required")
    payload: dict[str, Any] = {
        "schema_name": "broll_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_broll_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "cue-planning", "licensed-local-catalog", CUE_PLANNING_IMPLEMENTATION_VERSION
        ),
        "inputs": [
            artifact_input(str(catalog["catalog_id"]), catalog_path),
            artifact_input("art_assets_policy", assets_policy_path),
        ]
        + (
            [artifact_input(str(search_result["artifact_id"]), search_path)]
            if search_result and search_path
            else []
        ),
        "config_sha256": config_sha256(layout),
        "currency": policy.currency,
        "estimated_total": "0",
        "reserve_percent": policy.reserve_percent,
        "requests": requests,
        "warnings": warnings,
    }
    validate_artifact(package_root, "broll_plan", payload)
    return payload, warnings


def _sound_density_filter(
    package_root: Path, sound_plan: dict[str, Any], policy: CuePlacementPolicy
) -> tuple[dict[str, Any], list[str]]:
    raw_cues = sound_plan.get("cues", [])
    if not isinstance(raw_cues, list):
        raise PlanningValidationError("sound plan cues must be an array")
    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw_cue in sorted(
        (item for item in raw_cues if isinstance(item, Mapping)),
        key=lambda item: (
            _int(item.get("start_us"), "sound cue start", 0),
            str(item.get("cue_id", "")),
        ),
    ):
        cue = dict(raw_cue)
        cue_id = str(cue.get("cue_id", ""))
        start_us = _int(cue.get("start_us"), "sound cue start")
        end_us = _int(cue.get("end_us"), "sound cue end")
        if any(
            start_us < _int(previous.get("end_us"), "sound cue end")
            and end_us > _int(previous.get("start_us"), "sound cue start")
            for previous in kept
        ):
            warnings.append(f"sound_collision:{cue_id}")
            continue
        recent = [
            previous
            for previous in kept
            if start_us - _int(previous.get("start_us"), "sound cue start") < 60_000_000
        ]
        if len(recent) >= policy.maximum_sound_cues_per_minute:
            warnings.append(f"sound_frequency_limit:{cue_id}")
            continue
        kept.append(cue)
    if warnings:
        sound_plan["warnings"] = list(
            dict.fromkeys([str(item) for item in sound_plan.get("warnings", [])] + warnings)
        )
    sound_plan["cues"] = kept
    validate_artifact(package_root, "sound_plan", sound_plan)
    return sound_plan, warnings


def _path_ref(artifact_id: str, path: Path) -> dict[str, str]:
    return {"artifact_id": artifact_id, "path": str(path), "sha256": sha256_file(path)}


def _bundle_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    dependencies = payload.get("dependencies")
    plans = payload.get("plans")
    if not isinstance(dependencies, list) or not isinstance(plans, Mapping):
        raise PlanningValidationError("cue plan bundle dependencies and plans are missing")
    return {
        "schema_name": "cue_plan_bundle",
        "schema_version": "1.0.0",
        "project_id": payload.get("project_id"),
        "revision_id": payload.get("revision_id"),
        "planning_key": payload.get("planning_key"),
        "timeline_duration_us": payload.get("timeline_duration_us"),
        "dependencies": dependencies,
        "plans": plans,
        "density_policy": payload.get("density_policy"),
        "metrics": payload.get("metrics"),
        "config_sha256": payload.get("config_sha256"),
    }


def _verify_path_refs(
    refs: Sequence[Mapping[str, Any]], layout: ProjectLayout, package_root: Path
) -> None:
    for ref in refs:
        path = Path(str(ref.get("path", ""))).expanduser().resolve()
        try:
            path.relative_to(layout.root.resolve())
        except ValueError:
            try:
                path.relative_to(package_root.resolve())
            except ValueError as exc:
                raise PlanningValidationError(
                    f"cue plan dependency escapes allowed roots: {path}"
                ) from exc
        if not path.is_file():
            raise StaleApprovalError(f"cue plan dependency is missing: {path}")
        if sha256_file(path) != ref.get("sha256"):
            raise StaleApprovalError(f"cue plan dependency is stale: {path}")


def _verify_plan_refs(
    package_root: Path, layout: ProjectLayout, refs: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    schema_names = {"broll": "broll_plan", "motion": "motion_plan", "sound": "sound_plan"}
    verified: dict[str, dict[str, Any]] = {}
    for name, schema_name in schema_names.items():
        raw_ref = refs.get(name)
        if not isinstance(raw_ref, Mapping):
            raise StaleApprovalError(f"cue plan bundle is missing the {name} plan")
        path = Path(str(raw_ref.get("path", ""))).expanduser().resolve()
        try:
            path.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise PlanningValidationError(f"cue plan {name} escapes the project: {path}") from exc
        if not path.is_file() or sha256_file(path) != raw_ref.get("sha256"):
            raise StaleApprovalError(f"cue plan {name} is stale or missing: {path}")
        payload = _read_object(path, f"{name} plan")
        validate_artifact(package_root, schema_name, payload)
        if payload.get("artifact_id") != raw_ref.get("artifact_id"):
            raise StaleApprovalError(f"cue plan {name} artifact identity changed")
        verified[name] = payload
    return verified


def write_cue_plan_bundle(
    package_root: Path,
    layout: ProjectLayout,
    transition_plan_path: Path,
    catalog_path: Path,
    *,
    search_result_path: Path | None = None,
    assets_policy_path: Path | None = None,
    transitions_policy_path: Path | None = None,
    timeline_duration_us: int = 60_000_000,
    broll_windows: Sequence[Mapping[str, object]] = (),
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Build proposed local B-roll, motion, and sound plans under one approval hash."""

    if timeline_duration_us <= 0:
        raise PlanningValidationError("cue plan timeline duration must be positive")
    transition_file = _owned(layout, transition_plan_path, "transition plan")
    catalog_file = _owned(layout, catalog_path, "asset catalog")
    selected_assets_policy = (
        (assets_policy_path or package_root / "config" / "assets.example.yaml")
        .expanduser()
        .resolve()
    )
    selected_transitions_policy = (
        (transitions_policy_path or package_root / "config" / "transitions.example.yaml")
        .expanduser()
        .resolve()
    )
    if not selected_assets_policy.is_file() or not selected_transitions_policy.is_file():
        raise PlanningValidationError("cue placement policy files are missing")
    transition_plan = _read_transition_plan(package_root, layout, transition_file, revision_id)
    catalog, catalog_root, catalog_hash, _catalog_assets = _read_catalog(
        package_root, layout, catalog_file
    )
    search_result: dict[str, Any] | None = None
    search_file: Path | None = None
    if search_result_path is not None:
        search_file = _owned(layout, search_result_path, "asset search result")
        search_result = _read_search_result(
            package_root, layout, search_file, revision_id, catalog_hash
        )
    policy = CuePlacementPolicy.from_yaml(selected_assets_policy, selected_transitions_policy)
    width, height, frame_rate = _project_video_settings(layout)
    normalized_windows = [dict(window) for window in broll_windows]
    dependency_refs = [
        _path_ref(str(transition_plan["artifact_id"]), transition_file),
        _path_ref(str(catalog["catalog_id"]), catalog_file),
        _path_ref("art_assets_policy", selected_assets_policy),
        _path_ref("art_transitions_policy", selected_transitions_policy),
    ]
    if search_result is not None and search_file is not None:
        dependency_refs.append(_path_ref(str(search_result["artifact_id"]), search_file))
    planning_binding = {
        "implementation": CUE_PLANNING_IMPLEMENTATION_VERSION,
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "transition_plan_sha256": sha256_file(transition_file),
        "catalog_sha256": catalog_hash,
        "search_result_sha256": sha256_file(search_file) if search_file else None,
        "assets_policy_sha256": sha256_file(selected_assets_policy),
        "transitions_policy_sha256": sha256_file(selected_transitions_policy),
        "config_sha256": config_sha256(layout),
        "timeline_duration_us": timeline_duration_us,
        "broll_windows": normalized_windows,
        "target": {"width": width, "height": height, "frame_rate": frame_rate},
    }
    planning_key = canonical_sha256(planning_binding)
    destination = _owned(
        layout,
        output or layout.artifacts / "cue-plan-bundle.json",
        "cue plan bundle output",
    )
    if destination.is_file():
        existing = _read_object(destination, "existing cue plan bundle")
        validate_artifact(package_root, "cue_plan_bundle", existing)
        if existing.get("planning_key") == planning_key:
            _verify_path_refs(existing["dependencies"], layout, package_root)
            _verify_plan_refs(package_root, layout, existing["plans"])
            if existing.get("bundle_sha256") == canonical_sha256(_bundle_binding(existing)):
                return destination
    motion_payload, motion_warnings = _build_motion_plan(
        package_root,
        layout,
        transition_file,
        transition_plan,
        selected_transitions_policy,
        revision_id=revision_id,
        policy=policy,
    )
    broll_payload, broll_warnings = _build_broll_plan(
        package_root,
        layout,
        catalog_file,
        catalog,
        catalog_root,
        search_file,
        search_result,
        selected_assets_policy,
        revision_id=revision_id,
        timeline_duration_us=timeline_duration_us,
        broll_windows=normalized_windows,
        motion_cues=motion_payload["cues"],
        policy=policy,
    )
    broll_path = layout.artifacts / "broll-plan.json"
    motion_path = layout.artifacts / "motion-plan.json"
    write_validated_artifact(package_root, "broll_plan", broll_path, broll_payload)
    write_validated_artifact(package_root, "motion_plan", motion_path, motion_payload)
    sound_path = write_transition_sound_plan(
        package_root,
        layout,
        transition_file,
        catalog_file,
        policy_path=selected_assets_policy,
        revision_id=revision_id,
    )
    sound_payload = _read_object(sound_path, "sound plan")
    validate_artifact(package_root, "sound_plan", sound_payload)
    sound_payload, sound_warnings = _sound_density_filter(package_root, sound_payload, policy)
    write_validated_artifact(package_root, "sound_plan", sound_path, sound_payload)
    plan_refs = {
        "broll": _path_ref(str(broll_payload["artifact_id"]), broll_path),
        "motion": _path_ref(str(motion_payload["artifact_id"]), motion_path),
        "sound": _path_ref(str(sound_payload["artifact_id"]), sound_path),
    }
    broll_requests = broll_payload["requests"]
    motion_cues = motion_payload["cues"]
    sound_cues = sound_payload["cues"]
    broll_coverage_us = sum(_int(item["duration_us"], "B-roll duration") for item in broll_requests)
    collision_warnings = sorted(
        {
            warning
            for warning in [*motion_warnings, *broll_warnings, *sound_warnings]
            if "collision" in warning or "spacing" in warning or "frequency" in warning
        }
    )
    metrics = {
        "broll_count": len(broll_requests),
        "broll_coverage_us": broll_coverage_us,
        "broll_coverage_percent": round(broll_coverage_us * 100 / timeline_duration_us, 4),
        "motion_count": len(motion_cues),
        "sound_count": len(sound_cues),
        "collision_status": "warning" if collision_warnings else "pass",
        "collision_warnings": collision_warnings,
    }
    bundle: dict[str, Any] = {
        "schema_name": "cue_plan_bundle",
        "schema_version": "1.0.0",
        "artifact_id": "art_cue_plan_bundle",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "cue-planning", "licensed-local-catalog", CUE_PLANNING_IMPLEMENTATION_VERSION
        ),
        "inputs": [
            {"artifact_id": ref["artifact_id"], "sha256": ref["sha256"]} for ref in dependency_refs
        ],
        "config_sha256": config_sha256(layout),
        "planning_key": planning_key,
        "timeline_duration_us": timeline_duration_us,
        "dependencies": dependency_refs,
        "plans": plan_refs,
        "density_policy": policy.as_dict(),
        "metrics": metrics,
        "approval_required": True,
        "approval_state": "proposed",
        "bundle_sha256": "0" * 64,
        "warnings": sorted(
            set(
                [
                    "cue_plan_approval_required",
                    *[str(item) for item in broll_payload["warnings"]],
                    *[str(item) for item in motion_payload["warnings"]],
                    *[str(item) for item in sound_payload["warnings"]],
                    *collision_warnings,
                ]
            )
        ),
    }
    bundle["bundle_sha256"] = canonical_sha256(_bundle_binding(bundle))
    validate_artifact(package_root, "cue_plan_bundle", bundle)
    write_validated_artifact(package_root, "cue_plan_bundle", destination, bundle)
    return destination


def _read_bundle(
    package_root: Path, layout: ProjectLayout, bundle_path: Path
) -> tuple[Path, dict[str, Any]]:
    resolved = _owned(layout, bundle_path, "cue plan bundle")
    bundle = _read_object(resolved, "cue plan bundle")
    validate_artifact(package_root, "cue_plan_bundle", bundle)
    if bundle.get("project_id") != layout.root.name:
        raise PlanningValidationError("cue plan bundle project does not match")
    _verify_path_refs(bundle["dependencies"], layout, package_root)
    _verify_plan_refs(package_root, layout, bundle["plans"])
    expected = canonical_sha256(_bundle_binding(bundle))
    if bundle.get("bundle_sha256") != expected:
        raise StaleApprovalError("cue plan bundle hash binding is invalid")
    return resolved, bundle


def approve_cue_plan_bundle(
    package_root: Path,
    layout: ProjectLayout,
    bundle_path: Path,
    *,
    actor: str,
    role: str = "editor",
    reason: str = "Cue plan approved after human review",
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Write a human approval bound to the current cue-plan bundle hash."""

    if not actor.strip():
        raise PlanningValidationError("cue plan approval actor must not be empty")
    bundle_file, bundle = _read_bundle(package_root, layout, bundle_path)
    if bundle.get("revision_id") != revision_id:
        raise PlanningValidationError("cue plan bundle revision does not match")
    if bundle.get("approval_state") != "proposed":
        raise ApprovalRequiredError("cue plan bundle is not awaiting approval")
    bundle_hash = str(bundle["bundle_sha256"])
    approval_path = _owned(
        layout,
        output or layout.artifacts / "cue-plan-approval.json",
        "cue plan approval output",
    )
    if approval_path.is_file():
        existing = _read_object(approval_path, "existing cue plan approval")
        validate_artifact(package_root, "approval_record", existing)
        if existing.get("approved_item_sha256") == bundle_hash:
            return approval_path
        raise PlanningValidationError(
            "approval output already binds a different cue plan; choose a new output path"
        )
    plan_refs = bundle["plans"]
    approval_payload: dict[str, Any] = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": f"art_cue_approval_{bundle_hash[:12]}",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("cue-plan-approval", "human-review"),
        "inputs": [
            artifact_input("art_cue_plan_bundle", bundle_file),
            artifact_input(
                str(plan_refs["broll"]["artifact_id"]), Path(str(plan_refs["broll"]["path"]))
            ),
            artifact_input(
                str(plan_refs["motion"]["artifact_id"]), Path(str(plan_refs["motion"]["path"]))
            ),
            artifact_input(
                str(plan_refs["sound"]["artifact_id"]), Path(str(plan_refs["sound"]["path"]))
            ),
        ],
        "config_sha256": config_sha256(layout),
        "approval_id": f"apr_cue_{bundle_hash[:16]}",
        "approval_type": "cue_batch",
        "actor": actor,
        "role": role,
        "decision": "approved",
        "reason": reason,
        "approved_item_type": "cue_plan_bundle",
        "approved_item_sha256": bundle_hash,
        "expires_at": None,
        "budget": None,
    }
    validate_artifact(package_root, "approval_record", approval_payload)
    return write_validated_artifact(
        package_root, "approval_record", approval_path, approval_payload
    )


def authorize_cue_plan_bundle(
    package_root: Path,
    layout: ProjectLayout,
    bundle_path: Path,
    approval_path: Path,
    *,
    revision_id: str = "rev_001",
) -> dict[str, str]:
    """Verify the current human cue approval before a renderer consumes plans."""

    _bundle_file, bundle = _read_bundle(package_root, layout, bundle_path)
    if bundle.get("revision_id") != revision_id:
        raise StaleApprovalError("cue plan bundle approval revision is stale")
    approval_file = _owned(layout, approval_path, "cue plan approval")
    approval = _read_object(approval_file, "cue plan approval")
    validate_artifact(package_root, "approval_record", approval)
    if approval.get("project_id") != layout.root.name or approval.get("revision_id") != revision_id:
        raise StaleApprovalError("cue plan approval project or revision is stale")
    if approval.get("approval_type") != "cue_batch" or approval.get("decision") != "approved":
        raise ApprovalRequiredError("cue plan approval is not an approved cue batch")
    if approval.get("approved_item_sha256") != bundle.get("bundle_sha256"):
        raise StaleApprovalError("cue plan approval is stale for the current bundle")
    expires_at = approval.get("expires_at")
    if isinstance(expires_at, str):
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise StaleApprovalError("cue plan approval has expired")
        except ValueError as exc:
            raise StaleApprovalError("cue plan approval has an invalid expiry") from exc
    return {"approval_id": str(approval["approval_id"]), "sha256": sha256_file(approval_file)}


__all__ = [
    "CuePlacementPolicy",
    "approve_cue_plan_bundle",
    "authorize_cue_plan_bundle",
    "write_cue_plan_bundle",
]
