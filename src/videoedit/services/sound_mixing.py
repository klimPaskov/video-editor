from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import PlanningValidationError, StaleApprovalError, VideoeditError
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.cue_planning import authorize_cue_plan_bundle
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.rendering import parse_clipped_samples, parse_loudness_measurement
from videoedit.services.transition_sound import TransitionSoundPolicy, resolve_catalog_asset

SOUND_MIX_IMPLEMENTATION_VERSION = f"{__version__}:sound-mix-v1"
_ALLOWED_LICENCE_STATUSES = frozenset(
    {"owned", "licensed", "public_domain", "generated_with_recorded_terms"}
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


def _int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise PlanningValidationError(f"{label} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{label} must be an integer") from exc


def _number(value: object, label: str) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise PlanningValidationError(f"{label} must be finite")
    return result


def _catalog_root(layout: ProjectLayout, catalog_file: Path, catalog: Mapping[str, Any]) -> Path:
    root_value = Path(str(catalog.get("root_path", ""))).expanduser()
    root = (
        root_value.resolve()
        if root_value.is_absolute()
        else (catalog_file.parent / root_value).resolve()
    )
    try:
        root.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(
            f"sound asset catalog root escapes the project: {root}"
        ) from exc
    if not root.is_dir():
        raise PlanningValidationError(f"sound asset catalog root does not exist: {root}")
    return root


def _catalog_assets(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
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
    return assets


def _dependency_matches(
    bundle: Mapping[str, Any], artifact_id: str, path: Path, digest: str
) -> bool:
    dependencies = bundle.get("dependencies", [])
    if not isinstance(dependencies, list):
        return False
    resolved = path.resolve()
    return any(
        isinstance(item, Mapping)
        and item.get("artifact_id") == artifact_id
        and Path(str(item.get("path", ""))).expanduser().resolve() == resolved
        and item.get("sha256") == digest
        for item in dependencies
    )


def _bundle_and_sound_plan(
    package_root: Path,
    layout: ProjectLayout,
    bundle_path: Path,
    approval_path: Path,
    sound_plan_path: Path | None,
    revision_id: str,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any], dict[str, str]]:
    bundle_file = _owned(layout, bundle_path, "cue plan bundle")
    bundle = _read_object(bundle_file, "cue plan bundle")
    validate_artifact(package_root, "cue_plan_bundle", bundle)
    approval = authorize_cue_plan_bundle(
        package_root, layout, bundle_file, approval_path, revision_id=revision_id
    )
    plans = bundle.get("plans")
    if not isinstance(plans, Mapping) or not isinstance(plans.get("sound"), Mapping):
        raise StaleApprovalError("cue plan bundle has no sound plan reference")
    sound_ref = plans["sound"]
    referenced_sound = Path(str(sound_ref.get("path", ""))).expanduser().resolve()
    selected_sound = (
        _owned(layout, sound_plan_path, "sound plan")
        if sound_plan_path is not None
        else _owned(layout, referenced_sound, "sound plan")
    )
    if selected_sound != referenced_sound or sha256_file(selected_sound) != sound_ref.get("sha256"):
        raise StaleApprovalError("sound plan does not match the approved cue bundle")
    sound_plan = _read_object(selected_sound, "sound plan")
    validate_artifact(package_root, "sound_plan", sound_plan)
    if (
        sound_plan.get("project_id") != layout.root.name
        or sound_plan.get("revision_id") != revision_id
        or sound_plan.get("artifact_id") != sound_ref.get("artifact_id")
    ):
        raise StaleApprovalError("sound plan project, revision, or artifact is stale")
    return bundle_file, bundle, selected_sound, sound_plan, approval


def _validate_cue(
    cue: Mapping[str, Any],
    asset: Mapping[str, Any],
    *,
    policy: TransitionSoundPolicy,
) -> tuple[int, int, float, int, int]:
    cue_id = str(cue.get("cue_id", ""))
    start_us = _int(cue.get("start_us"), f"sound cue {cue_id} start")
    end_us = _int(cue.get("end_us"), f"sound cue {cue_id} end")
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError(f"sound cue {cue_id} range is not a positive half-open range")
    gain_db = _number(cue.get("gain_db"), f"sound cue {cue_id} gain")
    if gain_db < -60 or gain_db > policy.maximum_effect_gain_db:
        raise PlanningValidationError(
            f"sound cue {cue_id} gain exceeds the configured speech-safe range"
        )
    fade_in_us = _int(cue.get("fade_in_us"), f"sound cue {cue_id} fade in")
    fade_out_us = _int(cue.get("fade_out_us"), f"sound cue {cue_id} fade out")
    if fade_in_us < 0 or fade_out_us < 0:
        raise PlanningValidationError(f"sound cue {cue_id} fades must be nonnegative")
    if policy.speech_priority and cue.get("duck_speech") is not True:
        raise PlanningValidationError(
            f"sound cue {cue_id} must duck speech under the active policy"
        )
    if policy.speech_priority and cue.get("mix_policy") != "speech_priority":
        raise PlanningValidationError(f"sound cue {cue_id} must use speech_priority mixing")
    metadata = asset.get("audio_metadata")
    if not isinstance(metadata, Mapping) or metadata.get("speech_safe") is not True:
        raise PlanningValidationError(f"sound cue {cue_id} asset is not marked speech-safe")
    return start_us, end_us, gain_db, fade_in_us, fade_out_us


def _asset_for_cue(
    catalog_file: Path,
    assets: Mapping[str, Mapping[str, Any]],
    cue: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Path]:
    cue_id = str(cue.get("cue_id", ""))
    asset_id = str(cue.get("asset_id", ""))
    asset = assets.get(asset_id)
    if asset is None:
        raise PlanningValidationError(f"sound cue {cue_id} references a missing asset: {asset_id}")
    if asset.get("asset_type") != "sound_effect":
        raise PlanningValidationError(f"sound cue {cue_id} references a non-sound asset")
    if asset.get("licence_status") not in _ALLOWED_LICENCE_STATUSES:
        raise PlanningValidationError(f"sound cue {cue_id} asset licence status is not allowed")
    license_id = str(asset.get("licence_reference", "")).strip()
    if not license_id or cue.get("license_id") != license_id:
        raise PlanningValidationError(f"sound cue {cue_id} licence reference is stale")
    file_value = asset.get("file")
    if not isinstance(file_value, Mapping) or cue.get("asset_sha256") != file_value.get("sha256"):
        raise PlanningValidationError(f"sound cue {cue_id} asset hash is stale")
    path = resolve_catalog_asset(catalog_file, asset_id)
    if sha256_file(path) != str(cue.get("asset_sha256")):
        raise PlanningValidationError(f"sound cue {cue_id} asset file hash is stale")
    return asset, path


def mix_approved_sound_plan(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    catalog_path: Path,
    bundle_path: Path,
    approval_path: Path,
    *,
    sound_plan_path: Path | None = None,
    output: Path | None = None,
    report: Path | None = None,
    policy_path: Path | None = None,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Mix every cue from a currently approved local sound plan and QA the result."""

    source_file = _owned(layout, source, "sound mix source")
    catalog_file = _owned(layout, catalog_path, "sound asset catalog")
    approval_file = _owned(layout, approval_path, "cue plan approval")
    bundle_file, bundle, sound_file, sound_plan, approval_ref = _bundle_and_sound_plan(
        package_root,
        layout,
        bundle_path,
        approval_file,
        sound_plan_path,
        revision_id,
    )
    catalog = _read_object(catalog_file, "asset catalog")
    validate_artifact(package_root, "asset_catalog", catalog)
    _catalog_root(layout, catalog_file, catalog)
    catalog_hash = sha256_file(catalog_file)
    if not _dependency_matches(bundle, str(catalog["catalog_id"]), catalog_file, catalog_hash):
        raise StaleApprovalError(
            "sound asset catalog is not the catalog approved in the cue bundle"
        )
    selected_policy = (
        (policy_path or package_root / "config" / "assets.example.yaml").expanduser().resolve()
    )
    if not selected_policy.is_file():
        raise PlanningValidationError(f"sound policy does not exist: {selected_policy}")
    if not _dependency_matches(
        bundle, "art_assets_policy", selected_policy, sha256_file(selected_policy)
    ):
        raise StaleApprovalError("sound policy is not the policy approved in the cue bundle")
    policy = TransitionSoundPolicy.from_yaml(selected_policy)
    assets = _catalog_assets(catalog)
    raw_cues = sound_plan.get("cues")
    if not isinstance(raw_cues, list):
        raise PlanningValidationError("sound plan cues must be an array")
    cue_inputs: list[tuple[dict[str, Any], Mapping[str, Any], Path]] = []
    for raw_cue in raw_cues:
        if not isinstance(raw_cue, Mapping):
            raise PlanningValidationError("sound plan cue must be an object")
        asset, asset_path = _asset_for_cue(catalog_file, assets, raw_cue)
        _validate_cue(raw_cue, asset, policy=policy)
        cue_inputs.append((dict(raw_cue), asset, asset_path))
    selected_output = _owned(
        layout,
        output or layout.review / "sound-mix" / "approved-sound-mix.mp4",
        "sound mix output",
    )
    report_path = _owned(
        layout,
        report or layout.artifacts / "sound-mix-qa.json",
        "sound mix QA report",
    )
    source_hash = sha256_file(source_file)
    sound_hash = sha256_file(sound_file)
    bundle_hash = str(bundle["bundle_sha256"])
    if report_path.is_file() and selected_output.is_file():
        existing = _read_object(report_path, "existing sound mix QA report")
        validate_artifact(package_root, "sound_mix_qa", existing)
        if (
            existing.get("source_sha256") == source_hash
            and existing.get("asset_catalog_sha256") == catalog_hash
            and existing.get("sound_plan_sha256") == sound_hash
            and existing.get("cue_plan_bundle_sha256") == bundle_hash
            and existing.get("output", {}).get("sha256") == sha256_file(selected_output)
        ):
            return report_path
    selected_adapter = adapter or FFmpegAdapter()
    selected_output.parent.mkdir(parents=True, exist_ok=True)
    layout.staging.mkdir(parents=True, exist_ok=True)
    suffix = selected_output.suffix or source_file.suffix or ".mp4"
    failures: list[str] = []
    warnings: list[str] = []
    current_output: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="sound-mix-", dir=str(layout.staging.resolve())
        ) as staging_name:
            staging = Path(staging_name)
            if not cue_inputs:
                current_output = staging / f"source-copy{suffix}"
                shutil.copy2(source_file, current_output)
            else:
                current_input = source_file
                for index, (cue, _asset, asset_path) in enumerate(cue_inputs, start=1):
                    next_output = staging / f"mix-{index:03d}{suffix}"
                    start_us, _end_us, gain_db, fade_in_us, fade_out_us = _validate_cue(
                        cue, _asset, policy=policy
                    )
                    selected_adapter.mix_transition_sound(
                        current_input,
                        asset_path,
                        next_output,
                        start_us=start_us,
                        gain_db=gain_db,
                        fade_in_us=fade_in_us,
                        fade_out_us=fade_out_us,
                        duck_speech=bool(cue["duck_speech"]),
                    )
                    current_input = next_output
                current_output = current_input
            decode = selected_adapter.full_decode_check(current_output)
            if decode.exit_code != 0:
                failures.append("full_decode_failed")
            clipping = selected_adapter.measure_clipping(current_output)
            clipped_samples = parse_clipped_samples(clipping)
            if clipped_samples > 0:
                failures.append("clipping_detected")
            loudness: dict[str, float] | None = None
            try:
                loudness = parse_loudness_measurement(
                    selected_adapter.measure_loudness(current_output)
                )
            except VideoeditError:
                warnings.append("loudness_measurement_unavailable")
            if loudness is not None and loudness["true_peak_dbfs"] > policy.true_peak_limit_dbtp:
                failures.append("true_peak_exceeded")
            status = "fail" if failures else "warning" if warnings else "pass"
            failed_output: Path | None = None
            if failures:
                failed_output = selected_output.with_name(
                    f"{selected_output.stem}.failed-{uuid.uuid4().hex}{selected_output.suffix}"
                )
                failed_output.parent.mkdir(parents=True, exist_ok=True)
                os.replace(current_output, failed_output)
            else:
                os.replace(current_output, selected_output)
            final_output = failed_output or selected_output
            cue_results = [
                {
                    "cue_id": str(cue["cue_id"]),
                    "asset_id": str(cue["asset_id"]),
                    "asset_sha256": str(cue["asset_sha256"]),
                    "start_us": _int(cue["start_us"], "sound cue start"),
                    "end_us": _int(cue["end_us"], "sound cue end"),
                    "gain_db": _number(cue["gain_db"], "sound cue gain"),
                    "fade_in_us": _int(cue["fade_in_us"], "sound cue fade in"),
                    "fade_out_us": _int(cue["fade_out_us"], "sound cue fade out"),
                    "duck_speech": bool(cue["duck_speech"]),
                    "mix_policy": str(cue.get("mix_policy", "speech_priority")),
                    "status": status,
                }
            ]
            payload: dict[str, Any] = {
                "schema_name": "sound_mix_qa",
                "schema_version": "1.0.0",
                "artifact_id": "art_sound_mix_qa",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("sound-mix-qa", "ffmpeg", SOUND_MIX_IMPLEMENTATION_VERSION),
                "inputs": [
                    artifact_input("art_source", source_file),
                    artifact_input(str(catalog["catalog_id"]), catalog_file),
                    artifact_input(str(sound_plan["artifact_id"]), sound_file),
                    artifact_input("art_cue_plan_bundle", bundle_file),
                    artifact_input("art_cue_approval", approval_file),
                ],
                "config_sha256": config_sha256(layout),
                "source_sha256": source_hash,
                "asset_catalog_sha256": catalog_hash,
                "sound_plan_sha256": sound_hash,
                "cue_plan_bundle_sha256": bundle_hash,
                "cue_approval_id": approval_ref["approval_id"],
                "output": {
                    "path": str(final_output.resolve()),
                    "sha256": sha256_file(final_output),
                    "size_bytes": final_output.stat().st_size,
                },
                "cue_results": cue_results,
                "qa_status": status,
                "failures": failures,
                "warnings": warnings,
                "full_decode_status": "pass" if decode.exit_code == 0 else "fail",
                "clipped_samples": clipped_samples,
                "clipping_status": "fail" if clipped_samples > 0 else "pass",
                "loudness": loudness,
                "speech_priority": policy.speech_priority,
            }
            validate_artifact(package_root, "sound_mix_qa", payload)
            write_validated_artifact(package_root, "sound_mix_qa", report_path, payload)
            return report_path
    finally:
        if current_output is not None and current_output.is_file():
            current_output.unlink(missing_ok=True)


__all__ = ["mix_approved_sound_plan"]
