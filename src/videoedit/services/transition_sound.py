from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import ApprovalRequiredError, PlanningValidationError, VideoeditError
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.rendering import parse_clipped_samples, parse_loudness_measurement

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MOTION_TYPES = frozenset(
    {
        "dip_to_color",
        "swipe_left",
        "swipe_right",
        "push_left",
        "push_right",
        "blur_swipe",
        "chapter_transition",
    }
)


@dataclass(frozen=True, slots=True)
class TransitionSoundPolicy:
    allowed_licence_statuses: frozenset[str] = frozenset(
        {"owned", "licensed", "public_domain", "generated_with_recorded_terms"}
    )
    visual_peak_ratio: float = 0.4
    transient_alignment_tolerance_us: int = 30_000
    minimum_speech_clearance_us: int = 90_000
    default_effect_gain_db: float = -14.0
    maximum_effect_gain_db: float = -6.0
    default_fade_in_us: int = 20_000
    default_fade_out_us: int = 80_000
    default_minimum_reuse_interval_us: int = 45_000_000
    true_peak_limit_dbtp: float = -1.0
    speech_priority: bool = True
    styles: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 < self.visual_peak_ratio < 1:
            raise ValueError("visual peak ratio must be between zero and one")
        if self.transient_alignment_tolerance_us < 0:
            raise ValueError("transient alignment tolerance must be nonnegative")
        if self.minimum_speech_clearance_us < 0:
            raise ValueError("minimum speech clearance must be nonnegative")
        if self.default_effect_gain_db > self.maximum_effect_gain_db:
            raise ValueError("default effect gain must not exceed the maximum effect gain")
        if self.default_minimum_reuse_interval_us < 0:
            raise ValueError("minimum reuse interval must be nonnegative")
        if self.default_fade_in_us < 0 or self.default_fade_out_us < 0:
            raise ValueError("default fades must be nonnegative")

    @classmethod
    def from_yaml(cls, path: Path) -> TransitionSoundPolicy:
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PlanningValidationError(f"sound policy is unreadable: {path}") from exc
        if not isinstance(value, Mapping):
            raise PlanningValidationError("sound policy YAML must contain an object")
        mix = value.get("sound_mix", {})
        transition = value.get("transition_sound_metadata", {})
        if not isinstance(mix, Mapping):
            mix = {}
        if not isinstance(transition, Mapping):
            transition = {}
        statuses = value.get("catalog", {})
        statuses_value = (
            statuses.get("allowed_licence_status", []) if isinstance(statuses, Mapping) else []
        )
        allowed = frozenset(str(item) for item in statuses_value if str(item))
        return cls(
            allowed_licence_statuses=allowed
            or frozenset({"owned", "licensed", "public_domain", "generated_with_recorded_terms"}),
            visual_peak_ratio=float(transition.get("visual_peak_ratio", 0.4)),
            transient_alignment_tolerance_us=round(
                float(transition.get("transient_alignment_tolerance_ms", 30.0)) * 1000
            ),
            minimum_speech_clearance_us=round(
                float(mix.get("minimum_speech_clearance_ms", 90.0)) * 1000
            ),
            default_effect_gain_db=float(mix.get("default_effect_gain_db", -14.0)),
            maximum_effect_gain_db=float(mix.get("maximum_effect_gain_db", -6.0)),
            default_fade_in_us=round(float(mix.get("default_fade_in_ms", 20.0)) * 1000),
            default_fade_out_us=round(float(mix.get("default_fade_out_ms", 80.0)) * 1000),
            default_minimum_reuse_interval_us=round(
                float(transition.get("default_minimum_reuse_interval_seconds", 45.0)) * 1_000_000
            ),
            true_peak_limit_dbtp=float(mix.get("true_peak_limit_dbtp", -1.0)),
            speech_priority=bool(mix.get("speech_priority", True)),
        )


@dataclass(frozen=True, slots=True)
class TransitionSoundSelection:
    asset_id: str
    asset_sha256: str
    license_id: str
    file_path: str
    score: tuple[int, ...]
    rationale: tuple[str, ...]
    asset: Mapping[str, object]


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must contain an object")
    return value


def _int(value: object, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _transition_range(transition: Mapping[str, object]) -> tuple[int, int]:
    range_value = transition.get("range")
    if not isinstance(range_value, Mapping):
        raise PlanningValidationError("transition has no output range")
    start_us = _int(range_value.get("start_us"))
    end_us = _int(range_value.get("end_us"))
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError("transition range is invalid")
    return start_us, end_us


def _visual_peak_us(transition: Mapping[str, object], policy: TransitionSoundPolicy) -> int:
    start_us, end_us = _transition_range(transition)
    return start_us + round((end_us - start_us) * policy.visual_peak_ratio)


def _asset_candidates(catalog: Mapping[str, object]) -> list[Mapping[str, object]]:
    assets = catalog.get("assets", [])
    if not isinstance(assets, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    return [item for item in assets if isinstance(item, Mapping)]


def _reuse_allowed(
    asset_id: str,
    planned_start_us: int,
    asset_minimum_us: int,
    existing_cues: Sequence[Mapping[str, object]],
) -> bool:
    for cue in existing_cues:
        if str(cue.get("asset_id", "")) != asset_id:
            continue
        previous_start = _int(cue.get("start_us"))
        if previous_start >= 0 and abs(planned_start_us - previous_start) < asset_minimum_us:
            return False
    return True


def select_transition_sound(
    transition: Mapping[str, object],
    catalog: Mapping[str, object],
    *,
    existing_cues: Sequence[Mapping[str, object]] = (),
    brand_context: str | None = None,
    policy: TransitionSoundPolicy | None = None,
) -> TransitionSoundSelection:
    """Select a deterministic licensed, speech-safe local sound candidate."""

    selected_policy = policy or TransitionSoundPolicy()
    transition_type = str(transition.get("transition_type", ""))
    if transition_type not in _MOTION_TYPES:
        raise PlanningValidationError("routine clean cuts cannot receive transition sound")
    visual_peak_us = _visual_peak_us(transition, selected_policy)
    ranked: list[tuple[tuple[int, ...], str, TransitionSoundSelection]] = []
    for asset in _asset_candidates(catalog):
        if asset.get("asset_type") != "sound_effect":
            continue
        if str(asset.get("licence_status", "")) not in selected_policy.allowed_licence_statuses:
            continue
        asset_id = str(asset.get("asset_id", ""))
        file_value = asset.get("file")
        metadata = asset.get("audio_metadata")
        if (
            not _IDENTIFIER.fullmatch(asset_id)
            or not isinstance(file_value, Mapping)
            or not isinstance(metadata, Mapping)
        ):
            continue
        digest = str(file_value.get("sha256", ""))
        license_id = str(asset.get("licence_reference", ""))
        file_path = str(file_value.get("path", ""))
        if not _SHA256.fullmatch(digest) or not license_id or not file_path:
            continue
        if metadata.get("speech_safe") is not True:
            continue
        intended = {str(item) for item in metadata.get("intended_transition_types", [])}
        if (
            transition_type not in intended
            and "structural" not in intended
            and "all" not in intended
        ):
            continue
        offset_us = _int(metadata.get("transient_peak_offset_us"))
        duration_us = _int(file_value.get("duration_us"))
        if offset_us < 0 or duration_us <= 0 or offset_us > duration_us:
            continue
        planned_start_us = visual_peak_us - offset_us
        minimum_reuse_us = max(
            selected_policy.default_minimum_reuse_interval_us,
            _int(metadata.get("minimum_reuse_interval_us"), 0),
        )
        if planned_start_us < 0 or not _reuse_allowed(
            asset_id, planned_start_us, minimum_reuse_us, existing_cues
        ):
            continue
        brand_contexts = {str(item) for item in metadata.get("brand_contexts", [])}
        brand_score = int(brand_context is not None and brand_context in brand_contexts)
        exact_score = int(transition_type in intended)
        intensity_score = {"subtle": 2, "medium": 1, "strong": 0}.get(
            str(metadata.get("intensity", "medium")), 0
        )
        score = (
            exact_score,
            brand_score,
            intensity_score,
            int(metadata.get("speech_safe") is True),
        )
        rationale: tuple[str, ...] = (
            "licensed local sound effect",
            f"compatible with {transition_type}",
            "speech-safe metadata",
            "reuse spacing available",
        )
        if brand_score:
            rationale += (f"brand context {brand_context}",)
        selection = TransitionSoundSelection(
            asset_id=asset_id,
            asset_sha256=digest,
            license_id=license_id,
            file_path=file_path,
            score=score,
            rationale=rationale,
            asset=dict(asset),
        )
        ranked.append((score, asset_id, selection))
    if not ranked:
        raise PlanningValidationError(
            f"no licensed speech-safe transition sound is compatible with {transition_type}"
        )
    ranked.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], -item[0][3], item[1]))
    return ranked[0][2]


def _speech_protection(
    transition: Mapping[str, object],
    cue_end_us: int,
    policy: TransitionSoundPolicy,
) -> dict[str, object]:
    dialogue = transition.get("dialogue_protection")
    if not isinstance(dialogue, Mapping):
        return {
            "minimum_clearance_us": policy.minimum_speech_clearance_us,
            "first_important_word_us": None,
            "status": "warning",
        }
    first_word = dialogue.get("first_incoming_word_us")
    first_us = _int(first_word, -1) if first_word is not None else -1
    minimum = max(
        policy.minimum_speech_clearance_us,
        _int(dialogue.get("minimum_clearance_us"), 0),
    )
    if first_us < 0:
        status = "warning"
    else:
        status = "pass" if first_us - cue_end_us >= minimum else "fail"
    return {
        "minimum_clearance_us": minimum,
        "first_important_word_us": first_us if first_us >= 0 else None,
        "status": status,
    }


def align_transition_sound(
    transition: Mapping[str, object],
    selection: TransitionSoundSelection,
    *,
    existing_cues: Sequence[Mapping[str, object]] = (),
    policy: TransitionSoundPolicy | None = None,
    approval_state: str = "proposed",
) -> dict[str, object]:
    """Place a selected sound at the visual peak and persist all QA-relevant fields."""

    selected_policy = policy or TransitionSoundPolicy()
    if approval_state not in {"proposed", "approved", "rejected"}:
        raise PlanningValidationError("sound cue approval state is invalid")
    start_us, end_us = _transition_range(transition)
    visual_peak_us = _visual_peak_us(transition, selected_policy)
    asset = selection.asset
    file_value = asset.get("file")
    metadata = asset.get("audio_metadata")
    if not isinstance(file_value, Mapping) or not isinstance(metadata, Mapping):
        raise PlanningValidationError("selected transition sound lacks audio metadata")
    duration_us = _int(file_value.get("duration_us"))
    offset_us = _int(metadata.get("transient_peak_offset_us"))
    minimum_reuse_us = max(
        selected_policy.default_minimum_reuse_interval_us,
        _int(metadata.get("minimum_reuse_interval_us"), 0),
    )
    cue_start_us = visual_peak_us - offset_us
    cue_end_us = cue_start_us + duration_us
    alignment_delta_us = abs((cue_start_us + offset_us) - visual_peak_us)
    alignment_status = (
        "pass"
        if cue_start_us >= 0
        and alignment_delta_us <= selected_policy.transient_alignment_tolerance_us
        else "fail"
    )
    speech = _speech_protection(transition, cue_end_us, selected_policy)
    reuse_status = (
        "pass"
        if _reuse_allowed(selection.asset_id, cue_start_us, minimum_reuse_us, existing_cues)
        else "fail"
    )
    qa_status = (
        "fail"
        if alignment_status == "fail" or speech["status"] == "fail" or reuse_status == "fail"
        else "warning"
        if speech["status"] == "warning"
        else "planned"
    )
    fade_in_us = min(selected_policy.default_fade_in_us, max(0, duration_us // 3))
    fade_out_us = min(selected_policy.default_fade_out_us, max(0, duration_us // 3))
    if fade_in_us + fade_out_us >= duration_us:
        fade_in_us = 0
        fade_out_us = 0
    gain_db = min(selected_policy.default_effect_gain_db, selected_policy.maximum_effect_gain_db)
    transition_id = str(transition.get("transition_id", ""))
    cue_id = f"sfx_{transition_id}" if transition_id else f"sfx_{selection.asset_id}"
    return {
        "cue_id": cue_id,
        "start_us": max(0, cue_start_us),
        "end_us": max(1, cue_end_us),
        "asset_id": selection.asset_id,
        "asset_sha256": selection.asset_sha256,
        "license_id": selection.license_id,
        "purpose": str(transition.get("reason", "Support a structural transition")),
        "gain_db": gain_db,
        "fade_in_us": fade_in_us,
        "fade_out_us": fade_out_us,
        "duck_speech": selected_policy.speech_priority,
        "approval_state": approval_state,
        "linked_transition_id": transition_id or None,
        "sync_peak_us": visual_peak_us,
        "transient_peak_offset_us": offset_us,
        "speech_clearance_us": speech["minimum_clearance_us"],
        "mix_policy": "speech_priority" if selected_policy.speech_priority else "foreground_effect",
        "minimum_reuse_interval_us": minimum_reuse_us,
        "qa_status": qa_status,
        "transient_alignment_status": alignment_status,
        "transient_alignment_tolerance_us": selected_policy.transient_alignment_tolerance_us,
        "speech_protection": speech,
        "reuse_status": reuse_status,
        "selection_score": list(selection.score),
        "selection_rationale": list(selection.rationale),
        "visual_peak_us": visual_peak_us,
        "alignment_delta_us": alignment_delta_us,
        "source_range_start_us": start_us,
        "source_range_end_us": end_us,
    }


def write_transition_sound_plan(
    package_root: Path,
    layout: ProjectLayout,
    transition_plan_path: Path,
    catalog_path: Path,
    *,
    policy_path: Path | None = None,
    revision_id: str = "rev_001",
    brand_context: str | None = None,
    existing_cues: Sequence[Mapping[str, object]] = (),
) -> Path:
    """Persist proposed, licensed transition cues; approval remains external."""

    transition_file = transition_plan_path.expanduser().resolve()
    catalog_file = catalog_path.expanduser().resolve()
    transition_plan = _read_object(transition_file, "transition plan")
    catalog = _read_object(catalog_file, "asset catalog")
    validate_artifact(package_root, "transition_plan", transition_plan)
    validate_artifact(package_root, "asset_catalog", catalog)
    if (
        transition_plan.get("project_id") != layout.root.name
        or transition_plan.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("transition plan project or revision does not match")
    selected_policy_path = (
        (policy_path or package_root / "config" / "assets.example.yaml").expanduser().resolve()
    )
    policy = TransitionSoundPolicy.from_yaml(selected_policy_path)
    raw_transitions = transition_plan.get("transitions", [])
    if not isinstance(raw_transitions, list):
        raise PlanningValidationError("transition plan transitions must be an array")
    cues: list[dict[str, object]] = []
    warnings: list[str] = []
    prior = list(existing_cues)
    for transition_value in raw_transitions:
        if not isinstance(transition_value, Mapping):
            continue
        transition = dict(transition_value)
        transition_id = str(transition.get("transition_id", ""))
        if str(transition.get("transition_type", "")) not in _MOTION_TYPES:
            continue
        try:
            selection = select_transition_sound(
                transition,
                catalog,
                existing_cues=prior,
                brand_context=brand_context,
                policy=policy,
            )
            cue = align_transition_sound(
                transition,
                selection,
                existing_cues=prior,
                policy=policy,
            )
            cues.append(cue)
            prior.append(cue)
            if cue["qa_status"] != "planned":
                warnings.append(f"transition_sound_requires_qa:{transition_id}")
        except (PlanningValidationError, ValueError) as exc:
            warnings.append(f"transition_sound_fallback:{transition_id}:{exc}")
    if any(cue.get("approval_state") != "approved" for cue in cues):
        warnings.append("transition_sound_approval_required")
    payload: dict[str, object] = {
        "schema_name": "sound_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_sound_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("transition-sound-planning", "licensed-local-catalog", __version__),
        "inputs": [
            artifact_input(str(transition_plan["artifact_id"]), transition_file),
            artifact_input(str(catalog["catalog_id"]), catalog_file),
            artifact_input("art_transition_sound_policy", selected_policy_path),
        ],
        "config_sha256": config_sha256(layout),
        "catalog_id": str(catalog["catalog_id"]),
        "cues": cues,
        "warnings": warnings,
    }
    validate_artifact(package_root, "sound_plan", payload)
    output = layout.artifacts / "sound-plan.json"
    write_validated_artifact(package_root, "sound_plan", output, payload)
    return output


def _resolve_catalog_asset(
    catalog_path: Path, catalog: Mapping[str, object], asset_id: str
) -> Path:
    root_path = catalog.get("root_path")
    if not isinstance(root_path, str) or not root_path:
        raise PlanningValidationError("asset catalog root_path is missing")
    root = (catalog_path.parent / root_path).resolve()
    for asset in _asset_candidates(catalog):
        if str(asset.get("asset_id", "")) != asset_id:
            continue
        file_value = asset.get("file")
        if not isinstance(file_value, Mapping) or not isinstance(file_value.get("path"), str):
            break
        path = (root / str(file_value["path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PlanningValidationError("catalog asset path escapes catalog root") from exc
        if not path.is_file():
            raise PlanningValidationError(f"catalog sound asset is missing: {path}")
        if sha256_file(path) != str(file_value.get("sha256", "")):
            raise PlanningValidationError(f"catalog sound asset hash mismatch: {path}")
        return path
    raise PlanningValidationError(f"catalog sound asset is missing: {asset_id}")


def resolve_catalog_asset(catalog_path: Path, asset_id: str) -> Path:
    catalog_file = catalog_path.expanduser().resolve()
    catalog = _read_object(catalog_file, "asset catalog")
    return _resolve_catalog_asset(catalog_file, catalog, asset_id)


def qa_transition_sound_mix(
    package_root: Path,
    source: Path,
    sound: Path,
    cue: Mapping[str, object],
    output: Path,
    *,
    policy: TransitionSoundPolicy | None = None,
    adapter: FFmpegAdapter | None = None,
    allow_proposed: bool = False,
) -> dict[str, object]:
    """Render a bounded sound mix and retain diagnostics before promotion."""

    if cue.get("approval_state") != "approved" and not allow_proposed:
        raise ApprovalRequiredError("transition sound mix requires an approved cue")
    selected_policy = policy or TransitionSoundPolicy()
    selected_adapter = adapter or FFmpegAdapter()
    source = source.expanduser().resolve()
    sound = sound.expanduser().resolve()
    output = output.expanduser().resolve()
    expected_hash = str(cue.get("asset_sha256", ""))
    if not _SHA256.fullmatch(expected_hash) or sha256_file(sound) != expected_hash:
        raise PlanningValidationError("transition sound cue hash does not match the asset")
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.part{output.suffix}")
    failed_output: Path | None = None
    try:
        selected_adapter.mix_transition_sound(
            source,
            sound,
            temporary,
            start_us=_int(cue.get("start_us")),
            gain_db=_number(cue.get("gain_db"), selected_policy.default_effect_gain_db),
            fade_in_us=_int(cue.get("fade_in_us"), 0),
            fade_out_us=_int(cue.get("fade_out_us"), 0),
            duck_speech=bool(cue.get("duck_speech", selected_policy.speech_priority)),
        )
        decode = selected_adapter.full_decode_check(temporary)
        clipping = selected_adapter.measure_clipping(temporary)
        clipped_samples = parse_clipped_samples(clipping)
        loudness: dict[str, float] | None = None
        loudness_warning: str | None = None
        try:
            loudness = parse_loudness_measurement(selected_adapter.measure_loudness(temporary))
        except VideoeditError as exc:
            loudness_warning = str(exc)
        failures: list[str] = []
        warnings: list[str] = []
        if decode.exit_code != 0:
            failures.append("full_decode_failed")
        if clipped_samples > 0:
            failures.append("clipping_detected")
        if loudness_warning:
            warnings.append("loudness_measurement_unavailable")
        if (
            loudness is not None
            and loudness["true_peak_dbfs"] > selected_policy.true_peak_limit_dbtp
        ):
            failures.append("true_peak_exceeded")
        if failures:
            failed_output = output.with_name(
                f"{output.stem}.failed-{uuid.uuid4().hex}{output.suffix}"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, failed_output)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, output)
        qa_status = "fail" if failures else "warning" if warnings else "pass"
        return {
            "schema_name": "transition_sound_qa",
            "schema_version": "1.0.0",
            "cue_id": str(cue.get("cue_id", "")),
            "output": str(failed_output or output),
            "output_sha256": sha256_file(failed_output or output),
            "qa_status": qa_status,
            "failures": failures,
            "warnings": warnings,
            "full_decode_status": "pass" if decode.exit_code == 0 else "fail",
            "clipped_samples": clipped_samples,
            "loudness": loudness,
            "speech_priority": bool(cue.get("duck_speech", selected_policy.speech_priority)),
        }
    finally:
        temporary.unlink(missing_ok=True)


def write_transition_sound_qa(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    sound_plan_path: Path,
    catalog_path: Path,
    cue_id: str,
    output: Path,
    *,
    revision_id: str = "rev_001",
    policy_path: Path | None = None,
    allow_proposed: bool = False,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    sound_plan_file = sound_plan_path.expanduser().resolve()
    catalog_file = catalog_path.expanduser().resolve()
    sound_plan = _read_object(sound_plan_file, "sound plan")
    catalog = _read_object(catalog_file, "asset catalog")
    validate_artifact(package_root, "sound_plan", sound_plan)
    validate_artifact(package_root, "asset_catalog", catalog)
    if (
        sound_plan.get("project_id") != layout.root.name
        or sound_plan.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("sound plan project or revision does not match")
    cues = sound_plan.get("cues", [])
    if not isinstance(cues, list):
        raise PlanningValidationError("sound plan cues must be an array")
    cue = next(
        (item for item in cues if isinstance(item, Mapping) and item.get("cue_id") == cue_id),
        None,
    )
    if cue is None:
        raise PlanningValidationError(f"sound cue does not exist: {cue_id}")
    cue_mapping = dict(cue)
    asset_id = str(cue_mapping.get("asset_id", ""))
    sound_path = _resolve_catalog_asset(catalog_file, catalog, asset_id)
    selected_policy_path = (
        (policy_path or package_root / "config" / "assets.example.yaml").expanduser().resolve()
    )
    qa = qa_transition_sound_mix(
        package_root,
        source,
        sound_path,
        cue_mapping,
        output,
        policy=TransitionSoundPolicy.from_yaml(selected_policy_path),
        adapter=adapter,
        allow_proposed=allow_proposed,
    )
    raw_failures = qa.get("failures", [])
    raw_warnings = qa.get("warnings", [])
    failures = [str(item) for item in raw_failures] if isinstance(raw_failures, list) else []
    warnings = [str(item) for item in raw_warnings] if isinstance(raw_warnings, list) else []
    clipped_value = qa.get("clipped_samples", 0)
    clipped_samples = clipped_value if isinstance(clipped_value, int) else 0
    output_file = Path(str(qa["output"])).expanduser().resolve()
    payload: dict[str, object] = {
        "schema_name": "transition_sound_qa",
        "schema_version": "1.0.0",
        "artifact_id": "art_transition_sound_qa",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("transition-sound-qa", "ffmpeg", __version__),
        "inputs": [
            artifact_input(str(sound_plan["artifact_id"]), sound_plan_file),
            artifact_input(str(catalog["catalog_id"]), catalog_file),
            artifact_input("art_source", source.expanduser().resolve()),
        ],
        "config_sha256": config_sha256(layout),
        "cue_id": cue_id,
        "asset_sha256": str(cue_mapping["asset_sha256"]),
        "output": {
            "path": str(output_file),
            "sha256": str(qa["output_sha256"]),
            "size_bytes": output_file.stat().st_size,
        },
        "qa_status": str(qa["qa_status"]),
        "failures": failures,
        "warnings": warnings,
        "full_decode_status": str(qa["full_decode_status"]),
        "clipped_samples": clipped_samples,
        "loudness": qa["loudness"],
        "speech_priority": bool(qa["speech_priority"]),
    }
    validate_artifact(package_root, "transition_sound_qa", payload)
    report = layout.artifacts / "transition-sound-qa.json"
    write_validated_artifact(package_root, "transition_sound_qa", report, payload)
    return report


__all__ = [
    "TransitionSoundPolicy",
    "TransitionSoundSelection",
    "align_transition_sound",
    "qa_transition_sound_mix",
    "resolve_catalog_asset",
    "select_transition_sound",
    "write_transition_sound_plan",
    "write_transition_sound_qa",
]
