from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.inpainting import CommandInpaintingAdapter
from videoedit.adapters.transcription import WhisperAdapter
from videoedit.adapters.worker import WorkerAdapter
from videoedit.domain.models import TimelineSpec
from videoedit.errors import ApprovalRequiredError, VideoeditError
from videoedit.logging import configure_logging
from videoedit.services.approval import approve_final_render
from videoedit.services.asset_manifest import write_project_asset_manifest
from videoedit.services.asset_search import search_local_assets
from videoedit.services.assets import index_local_asset_catalog
from videoedit.services.backup import verify_backup_targets
from videoedit.services.captions import build_caption_plan
from videoedit.services.cleanup import approve_cleanup, execute_cleanup, plan_cleanup
from videoedit.services.composition import build_visual_composition
from videoedit.services.cue_planning import approve_cue_plan_bundle, write_cue_plan_bundle
from videoedit.services.demo import build_demo
from videoedit.services.doctor import run_doctor
from videoedit.services.editing import (
    augment_edit_proposals,
    compile_edl,
    create_gate1_approval,
    create_smart_dense_policy_approval,
    import_edit_decisions,
    materialize_operator_edit_decisions,
    plan_review_package,
    plan_silence_edits,
    write_edit_metrics_qa,
    write_smart_dense_review_batch,
)
from videoedit.services.effects import encode_segmentation_masks, prepare_matting_overlay
from videoedit.services.final_assembly import assemble_approved_segments
from videoedit.services.final_qa import qa_final_candidate
from videoedit.services.focus_pacing import (
    build_focus_pacing_plan,
    read_focus_pacing_plan,
    review_batch,
    write_focus_pacing_plan,
)
from videoedit.services.focus_qa import evaluate_focus_pacing_qa, write_focus_pacing_qa
from videoedit.services.foreground import render_chroma_key_foreground
from videoedit.services.gate2 import approve_segment_gate2
from videoedit.services.gate3 import approve_gate3
from videoedit.services.inpainting import plan_inpainting_request, submit_inpainting_request
from videoedit.services.join_qa import qa_rendered_joins
from videoedit.services.join_repair import (
    write_join_plan,
    write_retimed_join_plan,
    write_revision_join_plan,
)
from videoedit.services.marker_focus_pacing import build_marker_focus_pacing_plan
from videoedit.services.masking import recolor_local_mask, validate_local_mask
from videoedit.services.matting import (
    build_matting_quality_review,
    render_matting_contrast_previews,
    verify_matting_result,
)
from videoedit.services.occluder import append_occluder_video_layer, render_tracked_occluder
from videoedit.services.operations import (
    cancel_stage,
    recover_crashed_stage,
    request_stage_retry,
    write_project_status,
)
from videoedit.services.project import ProjectLayout, ingest_source, initialize_project, sha256_file
from videoedit.services.provider_jobs import plan_provider_job, submit_provider_job
from videoedit.services.publishing import (
    publish_delivery,
    write_publishing_metadata,
)
from videoedit.services.qa import basic_media_qa, qa_render
from videoedit.services.qa_override import create_qa_override, evaluate_qa_override
from videoedit.services.qa_review_decision import write_qa_review_decision
from videoedit.services.qa_review_packet import write_qa_review_packet
from videoedit.services.qa_review_visual_evidence import write_qa_review_visual_evidence
from videoedit.services.recut import recut_revision
from videoedit.services.remotion import RemotionService
from videoedit.services.render_manifest_bridge import (
    write_revision_render_manifest,
    write_revision_retimed_render_manifest,
)
from videoedit.services.rendering import render_base_timeline
from videoedit.services.replacement import write_object_replacement_manifest
from videoedit.services.retiming import (
    compile_retimed_timeline,
    read_retimed_timeline,
    render_retimed_timeline,
    write_retimed_timeline,
)
from videoedit.services.review_markers import import_review_markers
from videoedit.services.revisions import apply_review_markers
from videoedit.services.segment_lock import lock_segment_revision
from videoedit.services.segment_preview import write_segment_preview_plan
from videoedit.services.segment_qa import qa_segment_revision
from videoedit.services.segment_review_package import build_segment_review_packages
from videoedit.services.segment_transcription import (
    retranscribe_revision,
    write_segment_transcript_comparisons,
)
from videoedit.services.segment_visual_qa import qa_visual_segment
from videoedit.services.segmentation import (
    validate_segmentation_result,
    write_segmentation_contact_sheets,
    write_segmentation_validation,
)
from videoedit.services.silence import detect_project_silence
from videoedit.services.sound_mixing import mix_approved_sound_plan
from videoedit.services.source_candidate_qa import qa_source_candidate
from videoedit.services.tracking import (
    append_tracked_image_layer,
    write_object_track_keyframes,
    write_object_track_review,
)
from videoedit.services.transcription import transcribe_project
from videoedit.services.transition_sound import (
    write_transition_sound_plan,
    write_transition_sound_qa,
)
from videoedit.services.transitions import write_structural_boundaries, write_transition_plan
from videoedit.services.watchthrough import record_watchthrough
from videoedit.services.worker_runtime import approve_worker_runtime
from videoedit.settings import Settings

app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="Codex-orchestrated video editing workflow",
)


def _package_root() -> Path:
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "schemas").is_dir() and (candidate / "remotion").is_dir():
            return candidate
    raise RuntimeError("Could not locate the repository root containing schemas/")


def _configured_ffmpeg_adapter() -> FFmpegAdapter:
    settings = Settings()
    return FFmpegAdapter(
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
        video_codec=settings.video_codec,
        video_bitrate_bps=settings.video_bitrate_bps,
    )


def _configured_remotion_npm_path() -> str:
    """Return the npm executable paired with the configured Node.js runtime."""

    return Settings().npm_path


def _configured_remotion_service(remotion_directory: Path) -> RemotionService:
    """Build a Remotion service with the process-level Node.js configuration."""

    settings = Settings()
    return RemotionService(
        remotion_directory.resolve(),
        npm_path=settings.npm_path,
        package_root=_package_root(),
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
    )


def _configured_whisper_adapter(model_path: Path | None = None) -> WhisperAdapter:
    """Build the local transcription adapter from explicit or process settings."""

    selected_model_path = model_path
    if selected_model_path is None:
        selected_model_path = Settings().whisper_model_path
    return WhisperAdapter(model_path=selected_model_path)


def _configured_whisper_model(model_name: str | None = None) -> str:
    """Select an explicit model name or the configured local model name."""

    if model_name is not None and model_name.strip():
        return model_name
    return Settings().whisper_model


def _workspace_path(value: Path | None) -> Path:
    return (value or Path.cwd()).resolve()


def _read_json_path(path: Path, description: str) -> Any:
    try:
        return json.loads(path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"{description} is unreadable: {path}") from exc


def _parse_plan_specs(specs: list[str]) -> dict[str, Path]:
    plans: dict[str, Path] = {}
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter("plan specs must use SCHEMA_NAME=PATH")
        schema_name, raw_path = spec.split("=", 1)
        if not schema_name or not raw_path or schema_name in plans:
            raise typer.BadParameter("plan specs must have unique non-empty schema names")
        plans[schema_name] = Path(raw_path).resolve()
    return plans


def _parse_qa_override_finding_specs(specs: list[str]) -> dict[str, list[Path]]:
    findings: dict[str, list[Path]] = {}
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter("finding specs must use FINDING_ID=EVIDENCE_PATH")
        finding_id, raw_path = spec.split("=", 1)
        if not finding_id or not raw_path:
            raise typer.BadParameter("finding specs must have a finding ID and evidence path")
        findings.setdefault(finding_id, []).append(Path(raw_path).resolve())
    return findings


def _parse_qa_review_decision_specs(specs: list[str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter("decision specs must use ITEM_ID=DECISION")
        item_id, value = spec.split("=", 1)
        if not item_id or not value or item_id in decisions:
            raise typer.BadParameter("decision specs require unique item IDs and decisions")
        decisions[item_id] = value
    return decisions


def _parse_qa_review_evidence_specs(specs: list[str]) -> dict[str, list[Path]]:
    evidence: dict[str, list[Path]] = {}
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter("evidence specs must use ITEM_ID=EVIDENCE_PATH")
        item_id, raw_path = spec.split("=", 1)
        if not item_id or not raw_path:
            raise typer.BadParameter("evidence specs require an item ID and evidence path")
        evidence.setdefault(item_id, []).append(Path(raw_path).resolve())
    return evidence


def _parse_derivative_specs(specs: list[str]) -> dict[str, tuple[int, int]]:
    derivatives: dict[str, tuple[int, int]] = {}
    for spec in specs:
        if "=" not in spec or "x" not in spec.lower():
            raise typer.BadParameter("derivative specs must use NAME=WIDTHxHEIGHT")
        name, dimensions = spec.split("=", 1)
        width_text, height_text = dimensions.lower().split("x", 1)
        try:
            width, height = int(width_text), int(height_text)
        except ValueError as exc:
            raise typer.BadParameter("derivative dimensions must be integers") from exc
        if not name or width <= 0 or height <= 0 or name in derivatives:
            raise typer.BadParameter("derivative names and dimensions must be positive and unique")
        derivatives[name] = (width, height)
    return derivatives


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the application version and exit"),
    ] = False,
    log_level: Annotated[str, typer.Option(help="Log level for structured stderr logs")] = "INFO",
) -> None:
    configure_logging(log_level)
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    settings = Settings()
    report = run_doctor(settings, package_root=_package_root())
    payload = report.as_payload()
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        for check in report.checks:
            typer.echo(f"{check.status:>7}  {check.name}: {check.message}")
    if report.failed:
        raise typer.Exit(code=3)


@app.command("init")
def init_project(
    project_id: str,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = initialize_project(_workspace_path(workspace), project_id)
    typer.echo(str(layout.root))


@app.command()
def ingest(
    project_id: str,
    source: Path,
    copy_source: Annotated[
        bool,
        typer.Option(
            "--copy/--reference", help="Copy source bytes or register a managed reference"
        ),
    ] = True,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = initialize_project(_workspace_path(workspace), project_id)
    manifest = ingest_source(
        layout,
        source,
        package_root=_package_root(),
        adapter=_configured_ffmpeg_adapter(),
        copy_source=copy_source,
    )
    typer.echo(json.dumps(manifest, indent=2))


@app.command()
def probe(
    project_id: str,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    probe_path = layout.artifacts / "media-probe.json"
    if not probe_path.is_file():
        raise typer.BadParameter("ingest the source before probing")
    typer.echo(probe_path.read_text(encoding="utf-8"))


@app.command()
def transcribe(
    project_id: str,
    model: Annotated[str | None, typer.Option(help="Local Whisper model name")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = transcribe_project(
        _package_root(),
        layout,
        _configured_whisper_model(model),
        adapter=_configured_whisper_adapter(),
    )
    typer.echo(str(output))


@app.command("detect-silence")
def detect_silence(
    project_id: str,
    threshold_db: Annotated[float, typer.Option(help="Silence threshold in dB")] = -38.0,
    minimum_ms: Annotated[int, typer.Option(help="Minimum silence in milliseconds")] = 650,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = detect_project_silence(
        _package_root(),
        layout,
        threshold_db=threshold_db,
        minimum_duration_us=minimum_ms * 1000,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(output))


@app.command("plan-edits")
def plan_edits(
    project_id: str,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    proposals, decisions = plan_silence_edits(_package_root(), layout)
    effect_plan = layout.artifacts / "effect-plan.json"
    typer.echo(
        json.dumps(
            {
                "proposals": str(proposals),
                "decisions": str(decisions),
                "effect_plan": str(effect_plan),
            },
            indent=2,
        )
    )


@app.command("plan-review")
def plan_review(
    project_id: str,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    outputs = plan_review_package(_package_root(), layout)
    typer.echo(
        json.dumps(
            {
                "proposals": str(outputs.proposals_path),
                "effect_plan": str(outputs.effect_plan_path),
                "decisions": str(outputs.decision_template_path),
                "review_markdown": str(outputs.markdown_path),
            },
            indent=2,
        )
    )


@app.command("approve-smart-dense-policy")
def approve_smart_dense_policy(
    project_id: str,
    actor: Annotated[str, typer.Option(help="Approving person or account")],
    proposals: Annotated[Path | None, typer.Option(help="Edit proposal artifact JSON")] = None,
    role: Annotated[str, typer.Option(help="Approver role")] = "editor",
    reason: Annotated[
        str, typer.Option(help="Policy approval reason")
    ] = "Approved high-confidence mechanical edits under smart_dense policy",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = proposals or layout.artifacts / "edit-proposals.json"
    output = create_smart_dense_policy_approval(
        _package_root(),
        layout,
        selected.resolve(),
        actor=actor,
        role=role,
        reason=reason,
    )
    typer.echo(str(output))


@app.command("plan-smart-dense-review")
def plan_smart_dense_review(
    project_id: str,
    proposals: Annotated[Path | None, typer.Option(help="Edit proposal artifact JSON")] = None,
    policy_approval: Annotated[
        Path | None, typer.Option(help="Explicit human smart_dense policy approval JSON")
    ] = None,
    policy: Annotated[Path | None, typer.Option(help="Editing policy YAML")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = proposals or layout.artifacts / "edit-proposals.json"
    output = write_smart_dense_review_batch(
        _package_root(),
        layout,
        selected.resolve(),
        policy_approval_path=policy_approval.resolve() if policy_approval else None,
        policy_path=policy.resolve() if policy else None,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    typer.echo(
        json.dumps(
            {
                "path": str(output),
                "markdown": str(output.with_suffix(".md")),
                "policy_approval": payload["policy_approval"],
                "summary": payload["summary"],
            },
            indent=2,
        )
    )


@app.command("qa-edit-metrics")
def qa_edit_metrics(
    project_id: str,
    edl: Annotated[Path | None, typer.Option(help="Canonical edit decision list JSON")] = None,
    proposals: Annotated[Path | None, typer.Option(help="Edit proposal artifact JSON")] = None,
    transcript: Annotated[Path | None, typer.Option(help="Source transcript JSON")] = None,
    transition_plan: Annotated[
        Path | None, typer.Option(help="Optional structural transition plan JSON")
    ] = None,
    output_transcript: Annotated[
        Path | None, typer.Option(help="Optional rendered output transcript JSON")
    ] = None,
    join_qa: Annotated[
        Path | None, typer.Option(help="Optional rendered join QA report JSON")
    ] = None,
    policy: Annotated[Path | None, typer.Option(help="Editing policy YAML")] = None,
    transition_policy: Annotated[Path | None, typer.Option(help="Transition policy YAML")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_edit_metrics_qa(
        _package_root(),
        layout,
        (edl or layout.artifacts / "edit-decision-list.json").resolve(),
        (proposals or layout.artifacts / "edit-proposals.json").resolve(),
        (transcript or layout.artifacts / "transcript.json").resolve(),
        transition_plan_path=transition_plan.resolve() if transition_plan else None,
        output_transcript_path=output_transcript.resolve() if output_transcript else None,
        join_qa_path=join_qa.resolve() if join_qa else None,
        editing_policy_path=policy.resolve() if policy else None,
        transition_policy_path=transition_policy.resolve() if transition_policy else None,
    )
    typer.echo(str(output))


@app.command("plan-focus-pacing")
def plan_focus_pacing(
    project_id: str,
    candidates: Annotated[
        Path,
        typer.Option(
            help="JSON candidate package with inputs, operator_request, zooms, speedups, and skips"
        ),
    ],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    value = json.loads(candidates.resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise typer.BadParameter("focus pacing candidates must be a JSON object")
    payload = build_focus_pacing_plan(
        package_root=_package_root(),
        project_id=layout.root.name,
        revision_id=str(value.get("revision_id", "rev_001")),
        inputs=[item for item in value.get("inputs", []) if isinstance(item, dict)],
        zoom_candidates=[item for item in value.get("zooms", []) if isinstance(item, dict)],
        speedup_candidates=[item for item in value.get("speedups", []) if isinstance(item, dict)],
        operator_request=value.get("operator_request"),
        skipped_zoom_candidates=[
            item for item in value.get("skipped_zoom_candidates", []) if isinstance(item, dict)
        ],
        config_hash=value.get("config_sha256"),
        policy_values=value.get("policy"),
        warnings=[str(item) for item in value.get("warnings", [])],
    )
    output = write_focus_pacing_plan(_package_root(), layout, payload)
    typer.echo(
        json.dumps(
            {
                "path": str(output),
                "review_batch": review_batch(read_focus_pacing_plan(_package_root(), output)),
            },
            indent=2,
        )
    )


@app.command("validate-focus-plan")
def validate_focus_plan(path: Path) -> None:
    plan = read_focus_pacing_plan(_package_root(), path.resolve())
    typer.echo(
        json.dumps(
            {
                "status": "pass",
                "project_id": plan.project_id,
                "zoom_count": len(plan.zooms),
                "speedup_count": len(plan.speedups),
                "review_batch": review_batch(plan),
            },
            indent=2,
        )
    )


@app.command("qa-focus-pacing")
def qa_focus_pacing(
    project_id: str,
    focus_pacing_plan: Annotated[Path | None, typer.Option(help="Focus/pacing plan JSON")] = None,
    retimed_timeline: Annotated[
        Path | None, typer.Option(help="Optional retimed timeline JSON")
    ] = None,
    transcript: Annotated[Path | None, typer.Option(help="Optional transcript JSON")] = None,
    width: Annotated[
        int, typer.Option(help="Rendered composition width for geometry checks")
    ] = 1920,
    height: Annotated[
        int, typer.Option(help="Rendered composition height for geometry checks")
    ] = 1080,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = (focus_pacing_plan or layout.artifacts / "focus-pacing-plan.json").resolve()
    plan = read_focus_pacing_plan(_package_root(), selected_plan)
    selected_retimed = (
        (retimed_timeline or layout.artifacts / "retimed-timeline.json").resolve()
        if retimed_timeline is not None or (layout.artifacts / "retimed-timeline.json").is_file()
        else None
    )
    retimed = (
        read_retimed_timeline(_package_root(), selected_retimed)
        if selected_retimed is not None
        else None
    )
    transcript_value = (
        json.loads(transcript.resolve().read_text(encoding="utf-8")) if transcript else None
    )
    report = evaluate_focus_pacing_qa(
        plan,
        retimed_timeline=retimed,
        transcript=transcript_value,
        width=width,
        height=height,
    )
    report_path = write_focus_pacing_qa(
        _package_root(),
        layout,
        selected_plan,
        report,
        retimed_timeline_path=selected_retimed,
        revision_id=plan.revision_id,
    )
    typer.echo(report_path.read_text(encoding="utf-8"))
    if not report["final_ready"]:
        raise typer.Exit(code=10)


@app.command("import-edit-decisions")
def import_decisions(
    project_id: str,
    decisions: Annotated[Path, typer.Option(help="Reviewer decision JSON")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = import_edit_decisions(_package_root(), layout, decisions.resolve())
    typer.echo(str(output))


@app.command("augment-edit-proposals")
def augment_edit_proposals_command(
    project_id: str,
    base: Annotated[Path, typer.Option(help="Immutable base edit proposal artifact JSON")],
    instructions: Annotated[Path, typer.Option(help="Hash-bound operator edit instructions JSON")],
    output: Annotated[Path | None, typer.Option(help="Optional project-local output JSON")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = augment_edit_proposals(
        _package_root(),
        layout,
        base.resolve(),
        instructions.resolve(),
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("materialize-edit-decisions")
def materialize_edit_decisions_command(
    project_id: str,
    proposals: Annotated[Path, typer.Option(help="Hash-bound production edit proposals JSON")],
    smart_dense_batch: Annotated[Path, typer.Option(help="Approved smart-dense review batch JSON")],
    instructions: Annotated[Path, typer.Option(help="Hash-bound operator edit instructions JSON")],
    output: Annotated[Path | None, typer.Option(help="Optional project-local output JSON")] = None,
    safe_fallback_only: Annotated[
        bool,
        typer.Option(
            help=(
                "After a rejected render, reject all automatic policy cuts and retain only "
                "explicit operator edits"
            )
        ),
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = materialize_operator_edit_decisions(
        _package_root(),
        layout,
        proposals.resolve(),
        smart_dense_batch.resolve(),
        instructions.resolve(),
        output=output.resolve() if output else None,
        safe_fallback_only=safe_fallback_only,
    )
    typer.echo(str(created))


@app.command("approve-gate1")
def approve_gate1(
    project_id: str,
    decisions: Annotated[Path, typer.Option(help="Complete reviewer decision JSON")],
    actor: Annotated[str, typer.Option(help="Approving person or account")],
    effect_plan: Annotated[Path | None, typer.Option(help="Effect plan bound to Gate 1")] = None,
    focus_pacing_plan: Annotated[
        Path | None, typer.Option(help="Optional focus/pacing plan bound to Gate 1")
    ] = None,
    reason: Annotated[str, typer.Option(help="Approval reason")] = "Gate 1 approved after review",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_effect_plan = effect_plan or layout.artifacts / "effect-plan.json"
    selected_focus_plan = focus_pacing_plan
    if selected_focus_plan is None:
        candidate_focus_plan = layout.artifacts / "focus-pacing-plan.json"
        if candidate_focus_plan.is_file():
            selected_focus_plan = candidate_focus_plan
    output = create_gate1_approval(
        _package_root(),
        layout,
        decisions.resolve(),
        selected_effect_plan.resolve(),
        actor=actor,
        reason=reason,
        focus_pacing_plan_path=selected_focus_plan.resolve() if selected_focus_plan else None,
    )
    typer.echo(str(output))


@app.command("compile-edl")
def compile_edit_decisions(
    project_id: str,
    decisions: Annotated[Path | None, typer.Option(help="Reviewed edit decision JSON")] = None,
    gate1_approval: Annotated[
        Path | None, typer.Option(help="Current Gate 1 approval for approved cuts")
    ] = None,
    focus_pacing_plan: Annotated[
        Path | None, typer.Option(help="Optional focus/pacing plan bound to Gate 1")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = decisions or layout.review / "edit-decisions.json"
    selected_focus_plan = focus_pacing_plan
    if selected_focus_plan is None:
        candidate_focus_plan = layout.artifacts / "focus-pacing-plan.json"
        if candidate_focus_plan.is_file():
            selected_focus_plan = candidate_focus_plan
    output = compile_edl(
        _package_root(),
        layout,
        selected,
        gate1_approval_path=gate1_approval.resolve() if gate1_approval else None,
        focus_pacing_plan_path=selected_focus_plan.resolve() if selected_focus_plan else None,
    )
    typer.echo(str(output))


@app.command("plan-joins")
def plan_joins(
    project_id: str,
    proposals: Annotated[Path | None, typer.Option(help="Edit proposal artifact JSON")] = None,
    decisions: Annotated[Path | None, typer.Option(help="Reviewed edit decision JSON")] = None,
    edl: Annotated[Path | None, typer.Option(help="Compiled edit decision list JSON")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_proposals = proposals or layout.artifacts / "edit-proposals.json"
    selected_decisions = decisions or layout.review / "edit-decisions.json"
    selected_edl = edl or layout.artifacts / "edit-decision-list.json"
    output = write_join_plan(
        _package_root(),
        layout,
        selected_proposals.resolve(),
        selected_decisions.resolve(),
        selected_edl.resolve(),
    )
    typer.echo(str(output))


@app.command("rebase-join-plan")
def rebase_join_plan(
    project_id: str,
    join_plan: Annotated[
        Path | None, typer.Option(help="Pre-retime applied join plan JSON")
    ] = None,
    retimed_timeline: Annotated[
        Path | None,
        typer.Option(help="Authoritative retimed timeline JSON"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = join_plan or layout.artifacts / "join-plan.json"
    selected_timeline = retimed_timeline or layout.artifacts / "retimed-timeline.json"
    output = write_retimed_join_plan(
        _package_root(),
        layout,
        selected_plan.resolve(),
        selected_timeline.resolve(),
    )
    typer.echo(str(output))


@app.command("rebase-join-plan-revision")
def rebase_join_plan_revision(
    project_id: str,
    join_plan: Annotated[Path, typer.Option(help="Parent-output-clock join plan JSON")],
    revision_media: Annotated[Path, typer.Option(help="Current revision media manifest JSON")],
    revision_id: Annotated[str, typer.Option(help="Target project revision ID")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_revision_join_plan(
        _package_root(),
        layout,
        join_plan.resolve(),
        revision_media.resolve(),
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("qa-joins")
def qa_joins(
    project_id: str,
    render_manifest: Annotated[
        Path | None,
        typer.Option(help="Rendered base timeline manifest JSON"),
    ] = None,
    join_plan: Annotated[
        Path | None,
        typer.Option(help="Applied join plan JSON"),
    ] = None,
    transcript: Annotated[
        Path | None,
        typer.Option(help="Approved output transcript JSON"),
    ] = None,
    model_path: Annotated[
        Path | None,
        typer.Option(help="Operator-supplied local Whisper model path"),
    ] = None,
    model_name: Annotated[
        str | None,
        typer.Option(help="Whisper model name recorded in evidence"),
    ] = None,
    transcript_clock: Annotated[
        Literal["output", "source"],
        typer.Option(
            help=(
                "Clock used by the approved transcript: output for a post-render transcript "
                "(default), or source for source-clock ranges"
            )
        ),
    ] = "output",
    revision_id: Annotated[
        str, typer.Option(help="Revision bound to the rendered join-QA report")
    ] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    if render_manifest is not None:
        selected_manifest = render_manifest
    elif (layout.artifacts / "render-final.json").is_file():
        selected_manifest = layout.artifacts / "render-final.json"
    else:
        selected_manifest = layout.artifacts / "render-rough.json"
    selected_plan = join_plan or (
        layout.artifacts / "join-plan-retimed.json"
        if (layout.artifacts / "join-plan-retimed.json").is_file()
        else layout.artifacts / "join-plan.json"
    )
    selected_transcript = transcript or (
        layout.artifacts / "transcript-output.json"
        if (layout.artifacts / "transcript-output.json").is_file()
        else layout.artifacts / "transcript.json"
    )
    selected_transcriber = _configured_whisper_adapter(model_path)
    output = qa_rendered_joins(
        _package_root(),
        layout,
        selected_manifest.resolve(),
        selected_plan.resolve(),
        selected_transcript.resolve(),
        transcriber=selected_transcriber,
        model_name=_configured_whisper_model(model_name),
        adapter=_configured_ffmpeg_adapter(),
        revision_id=revision_id,
        transcript_clock=transcript_clock,
    )
    typer.echo(str(output))


@app.command("detect-boundaries")
def detect_boundaries_command(
    project_id: str,
    transcript: Annotated[Path | None, typer.Option(help="Output transcript JSON")] = None,
    explicit_boundaries: Annotated[
        Path | None,
        typer.Option(help="Optional operator boundary evidence JSON"),
    ] = None,
    policy: Annotated[Path | None, typer.Option(help="Transition policy YAML")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_transcript = transcript or (
        layout.artifacts / "transcript-output.json"
        if (layout.artifacts / "transcript-output.json").is_file()
        else layout.artifacts / "transcript.json"
    )
    output = write_structural_boundaries(
        _package_root(),
        layout,
        selected_transcript.resolve(),
        explicit_boundaries_path=explicit_boundaries.resolve() if explicit_boundaries else None,
        policy_path=policy.resolve() if policy else None,
    )
    typer.echo(str(output))


@app.command("plan-transitions")
def plan_transitions_command(
    project_id: str,
    transcript: Annotated[Path | None, typer.Option(help="Output transcript JSON")] = None,
    boundaries: Annotated[Path | None, typer.Option(help="Structural boundaries JSON")] = None,
    sound_plan: Annotated[
        Path | None, typer.Option(help="Approved or proposed sound plan JSON")
    ] = None,
    policy: Annotated[Path | None, typer.Option(help="Transition policy YAML")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_transcript = transcript or (
        layout.artifacts / "transcript-output.json"
        if (layout.artifacts / "transcript-output.json").is_file()
        else layout.artifacts / "transcript.json"
    )
    selected_boundaries = boundaries
    if selected_boundaries is None and (layout.artifacts / "structural-boundaries.json").is_file():
        selected_boundaries = layout.artifacts / "structural-boundaries.json"
    selected_sound_plan = sound_plan
    if selected_sound_plan is None and (layout.artifacts / "sound-plan.json").is_file():
        selected_sound_plan = layout.artifacts / "sound-plan.json"
    output = write_transition_plan(
        _package_root(),
        layout,
        selected_transcript.resolve(),
        boundaries_path=selected_boundaries.resolve() if selected_boundaries else None,
        sound_plan_path=selected_sound_plan.resolve() if selected_sound_plan else None,
        policy_path=policy.resolve() if policy else None,
    )
    typer.echo(str(output))


@app.command("plan-transition-sounds")
def plan_transition_sounds_command(
    project_id: str,
    transition_plan: Annotated[
        Path | None, typer.Option(help="Structural transition plan JSON")
    ] = None,
    catalog: Annotated[Path | None, typer.Option(help="Licensed local asset catalog JSON")] = None,
    policy: Annotated[Path | None, typer.Option(help="Local assets policy YAML")] = None,
    brand_context: Annotated[str | None, typer.Option(help="Optional brand context tag")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_transition = transition_plan or layout.artifacts / "transition-plan.json"
    selected_catalog = catalog or layout.artifacts / "asset-catalog.json"
    if not selected_transition.is_file():
        raise typer.BadParameter(f"transition plan does not exist: {selected_transition}")
    if not selected_catalog.is_file():
        raise typer.BadParameter(f"asset catalog does not exist: {selected_catalog}")
    output = write_transition_sound_plan(
        _package_root(),
        layout,
        selected_transition.resolve(),
        selected_catalog.resolve(),
        policy_path=policy.resolve() if policy else None,
        brand_context=brand_context,
    )
    typer.echo(str(output))


@app.command("qa-transition-sound")
def qa_transition_sound_command(
    project_id: str,
    cue_id: str,
    source: Annotated[Path, typer.Option(help="Production or base-edit video")],
    catalog: Annotated[Path, typer.Option(help="Licensed local asset catalog JSON")],
    sound_plan: Annotated[Path | None, typer.Option(help="Sound plan JSON")] = None,
    output: Annotated[Path | None, typer.Option(help="QA mix output path")] = None,
    allow_proposed: Annotated[
        bool, typer.Option(help="Render a bounded preview before sound approval")
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = sound_plan or layout.artifacts / "sound-plan.json"
    payload = json.loads(selected_plan.resolve().read_text(encoding="utf-8"))
    cues = payload.get("cues", []) if isinstance(payload, dict) else []
    cue = next(
        (item for item in cues if isinstance(item, dict) and item.get("cue_id") == cue_id),
        None,
    )
    if cue is None:
        raise typer.BadParameter(f"sound cue does not exist: {cue_id}")
    selected_output = output or layout.review / "transition-sound" / f"{cue_id}-mix.mp4"
    report = write_transition_sound_qa(
        _package_root(),
        layout,
        source.resolve(),
        selected_plan.resolve(),
        catalog.resolve(),
        cue_id,
        selected_output.resolve(),
        allow_proposed=allow_proposed,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(report.read_text(encoding="utf-8"))


@app.command("mix-sound-plan")
def mix_sound_plan_command(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Immutable base-edit or production source video")],
    catalog: Annotated[Path, typer.Option(help="Current licensed local asset catalog JSON")],
    bundle: Annotated[Path | None, typer.Option(help="Approved cue-plan bundle JSON")] = None,
    approval: Annotated[
        Path | None, typer.Option(help="Current human cue_batch approval JSON")
    ] = None,
    sound_plan: Annotated[Path | None, typer.Option(help="Sound plan JSON")] = None,
    output: Annotated[Path | None, typer.Option(help="Project-local mixed output video")] = None,
    report: Annotated[Path | None, typer.Option(help="Project-local sound mix QA report")] = None,
    policy: Annotated[Path | None, typer.Option(help="Local sound policy YAML")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_bundle = bundle or layout.artifacts / "cue-plan-bundle.json"
    selected_approval = approval or layout.artifacts / "cue-plan-approval.json"
    if not selected_bundle.is_file():
        raise typer.BadParameter(f"cue plan bundle does not exist: {selected_bundle}")
    if not selected_approval.is_file():
        raise typer.BadParameter(f"cue plan approval does not exist: {selected_approval}")
    created = mix_approved_sound_plan(
        _package_root(),
        layout,
        source.resolve(),
        catalog.resolve(),
        selected_bundle.resolve(),
        selected_approval.resolve(),
        sound_plan_path=sound_plan.resolve() if sound_plan else None,
        output=output.resolve() if output else None,
        report=report.resolve() if report else None,
        policy_path=policy.resolve() if policy else None,
        revision_id=revision_id,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(created.read_text(encoding="utf-8"))


@app.command("manifest-assets")
def manifest_assets_command(
    project_id: str,
    catalog: Annotated[Path, typer.Option(help="Current licensed local asset catalog JSON")],
    bundle: Annotated[Path | None, typer.Option(help="Approved cue-plan bundle JSON")] = None,
    approval: Annotated[
        Path | None, typer.Option(help="Current human cue_batch approval JSON")
    ] = None,
    replacement_manifest: Annotated[
        list[Path] | None,
        typer.Option(help="Object replacement manifest JSON; repeat for multiple selections"),
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Project-local asset manifest JSON")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_bundle = bundle
    selected_approval = approval
    default_bundle = layout.artifacts / "cue-plan-bundle.json"
    default_approval = layout.artifacts / "cue-plan-approval.json"
    if selected_bundle is None and selected_approval is None:
        if default_bundle.is_file() or default_approval.is_file():
            selected_bundle = default_bundle
            selected_approval = default_approval
    created = write_project_asset_manifest(
        _package_root(),
        layout,
        catalog.resolve(),
        cue_bundle_path=selected_bundle.resolve() if selected_bundle else None,
        cue_approval_path=selected_approval.resolve() if selected_approval else None,
        replacement_manifest_paths=[path.resolve() for path in (replacement_manifest or [])],
        output=output.resolve() if output else None,
        revision_id=revision_id,
    )
    typer.echo(created.read_text(encoding="utf-8"))


@app.command("plan-provider-job")
def plan_provider_job_command(
    project_id: str,
    request_plan: Annotated[Path, typer.Option(help="B-roll/provider request plan JSON")],
    request_id: Annotated[str, typer.Option(help="Provider request ID")],
    provider: Annotated[str | None, typer.Option(help="Configured provider identifier")] = None,
    model: Annotated[str | None, typer.Option(help="Configured provider model")] = None,
    output: Annotated[Path | None, typer.Option(help="Project-local provider job JSON")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = plan_provider_job(
        _package_root(),
        layout,
        request_plan.resolve(),
        request_id,
        provider=provider,
        model=model,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(created.read_text(encoding="utf-8"))


@app.command("submit-provider-job")
def submit_provider_job_command(
    project_id: str,
    job: Annotated[Path, typer.Option(help="Project-local provider job JSON")],
    effect_approval: Annotated[Path, typer.Option(help="Current effect approval JSON")],
    spend_approval: Annotated[Path, typer.Option(help="Current bounded spend approval JSON")],
    network_enabled: Annotated[
        bool, typer.Option(help="Explicitly opt in to provider network access")
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = submit_provider_job(
        _package_root(),
        layout,
        job.resolve(),
        effect_approval.resolve(),
        spend_approval.resolve(),
        network_enabled=network_enabled,
    )
    typer.echo(created.read_text(encoding="utf-8"))


@app.command("compile-retimed-timeline")
def compile_retimed(
    project_id: str,
    retime_plan: Annotated[Path | None, typer.Option(help="Focus/pacing plan JSON")] = None,
    edl: Annotated[Path | None, typer.Option(help="Approved edit decision list JSON")] = None,
    approved_speedup_id: Annotated[
        list[str] | None,
        typer.Option(help="Review-approved speed-up id; repeat for multiple items"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = (retime_plan or layout.artifacts / "focus-pacing-plan.json").resolve()
    selected_edl = (edl or layout.artifacts / "edit-decision-list.json").resolve()
    plan = read_focus_pacing_plan(_package_root(), selected_plan)
    edl_value = json.loads(selected_edl.read_text(encoding="utf-8"))
    if not isinstance(edl_value, dict):
        raise typer.BadParameter("EDL must be a JSON object")
    payload = compile_retimed_timeline(
        package_root=_package_root(),
        project_id=layout.root.name,
        revision_id=plan.revision_id,
        source_duration_us=int(edl_value["source_duration_us"]),
        keep_ranges=[item for item in edl_value["keep_ranges"] if isinstance(item, dict)],
        speedups=[item.model_dump(mode="json") for item in plan.speedups],
        edit_decision_list_sha256=sha256_file(selected_edl),
        focus_pacing_plan_sha256=sha256_file(selected_plan),
        config_hash=plan.config_sha256,
        approved_speedup_ids=set(approved_speedup_id) if approved_speedup_id else None,
    )
    output = write_retimed_timeline(_package_root(), layout, payload)
    typer.echo(str(output))


@app.command("render-retimed")
def render_retimed(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Project-local immutable source video")],
    timeline: Annotated[Path | None, typer.Option(help="Retimed timeline JSON")] = None,
    output: Annotated[Path | None, typer.Option(help="Project-local output MP4")] = None,
    video_codec: Annotated[
        str | None, typer.Option(help="Optional explicit video codec for this render")
    ] = None,
    audio_codec: Annotated[str, typer.Option(help="Audio codec for this render")] = "aac",
    qp: Annotated[
        int | None, typer.Option(help="Explicit H.264 QP; use 0 for lossless libx264")
    ] = None,
    preset: Annotated[str, typer.Option(help="FFmpeg video encoder preset")] = "medium",
    audio_edge_fade_ms: Annotated[
        int, typer.Option(help="Micro fade at every retimed audio segment edge")
    ] = 0,
    strict_decode: Annotated[
        bool, typer.Option(help="Use FFmpeg -xerror during full decode validation")
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_timeline = (timeline or layout.artifacts / "retimed-timeline.json").resolve()
    manifest = render_retimed_timeline(
        _package_root(),
        layout,
        source.resolve(),
        selected_timeline,
        output=output.resolve() if output else None,
        adapter=_configured_ffmpeg_adapter(),
        video_codec=video_codec,
        audio_codec=audio_codec,
        qp=qp,
        preset=preset,
        audio_edge_fade_us=audio_edge_fade_ms * 1000,
        strict_decode=strict_decode,
    )
    typer.echo(str(manifest))


@app.command("render-base")
def render_base(
    project_id: str,
    output: Annotated[Path | None, typer.Option(help="Output MP4 path")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_output = output.resolve() if output else None
    manifest = render_base_timeline(
        _package_root(),
        layout,
        selected_output,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(manifest))


@app.command("validate-timeline")
def validate_timeline(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    timeline = TimelineSpec.model_validate(payload)
    typer.echo(f"valid {timeline.project_id} {timeline.duration_frames} frames")


@app.command("validate-visual-timeline")
def validate_visual_timeline_command(
    path: Path,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    timeline = service.load_timeline(path.resolve(), package_root=_package_root())
    service.write_props(timeline, path.resolve())
    typer.echo(f"valid {timeline.project_id} {timeline.duration_frames} frames")


@app.command("build-captions")
def build_captions(
    project_id: str,
    transcript: Annotated[Path | None, typer.Option(help="Transcript artifact JSON")] = None,
    render_manifest: Annotated[Path | None, typer.Option(help="Base render manifest JSON")] = None,
    brand: Annotated[Path | None, typer.Option(help="Local brand YAML profile")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    transcript_path = transcript or layout.artifacts / "transcript-output.json"
    if not transcript_path.is_file():
        transcript_path = layout.artifacts / "transcript.json"
    manifest_path = render_manifest or layout.artifacts / "render-rough.json"
    result = build_caption_plan(
        _package_root(),
        layout,
        transcript_path.resolve(),
        manifest_path.resolve(),
        brand_path=brand.resolve() if brand else None,
        revision_id=revision_id,
    )
    typer.echo(
        json.dumps(
            {
                "plan": str(result.plan_path),
                "ass": str(result.ass_path),
                "webvtt": str(result.webvtt_path),
                "text": str(result.text_path),
                "event_count": result.event_count,
                "warnings": list(result.warnings),
            },
            indent=2,
        )
    )


@app.command("bridge-render-manifest")
def bridge_render_manifest_command(
    project_id: str,
    base_render_manifest: Annotated[
        Path, typer.Option(help="Existing validated render manifest for the parent revision")
    ],
    revision_media: Annotated[Path, typer.Option(help="Current revision media manifest JSON")],
    revision_id: Annotated[str, typer.Option(help="Target project revision ID")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_revision_render_manifest(
        _package_root(),
        layout,
        base_render_manifest.resolve(),
        revision_media.resolve(),
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("bridge-retimed-render-manifest")
def bridge_retimed_render_manifest_command(
    project_id: str,
    base_retimed_manifest: Annotated[
        Path, typer.Option(help="Existing validated retimed render manifest for the parent")
    ],
    revision_media: Annotated[Path, typer.Option(help="Current revision media manifest JSON")],
    revision_id: Annotated[str, typer.Option(help="Target project revision ID")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_revision_retimed_render_manifest(
        _package_root(),
        layout,
        base_retimed_manifest.resolve(),
        revision_media.resolve(),
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("compose-visual")
def compose_visual(
    project_id: str,
    render_manifest: Annotated[Path | None, typer.Option(help="Base render manifest JSON")] = None,
    caption_plan: Annotated[Path | None, typer.Option(help="Caption plan JSON")] = None,
    subject: Annotated[
        Path | None, typer.Option(help="Optional local transparent subject video")
    ] = None,
    middle_text: Annotated[str, typer.Option(help="Text rendered behind the subject")] = (
        "TEXT BEHIND SUBJECT"
    ),
    front_label: Annotated[str, typer.Option(help="Front-layer label")] = "CODEX VIDEO AGENT",
    focus_pacing_plan: Annotated[
        Path | None, typer.Option(help="Optional approved focus/pacing plan JSON")
    ] = None,
    retimed_timeline: Annotated[
        Path | None, typer.Option(help="Optional retimed timeline used to rebase focus times")
    ] = None,
    approved_zoom_id: Annotated[
        list[str] | None,
        typer.Option(help="Review-approved zoom id; repeat for multiple items"),
    ] = None,
    transition_plan: Annotated[
        Path | None, typer.Option(help="Structural transition plan JSON")
    ] = None,
    approved_transition_id: Annotated[
        list[str] | None,
        typer.Option(help="Review-approved transition id; repeat for multiple items"),
    ] = None,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    manifest_path = render_manifest or layout.artifacts / "render-rough.json"
    caption_path = caption_plan or layout.artifacts / "caption-plan.json"
    transition_path = transition_plan or layout.artifacts / "transition-plan.json"
    if caption_plan is not None and not caption_path.is_file():
        raise typer.BadParameter(f"caption plan does not exist: {caption_path}")
    if transition_plan is not None and not transition_path.is_file():
        raise typer.BadParameter(f"transition plan does not exist: {transition_path}")
    result = build_visual_composition(
        _package_root(),
        layout,
        manifest_path.resolve(),
        remotion_directory=remotion_directory.resolve(),
        npm_path=_configured_remotion_npm_path(),
        caption_plan_path=caption_path.resolve() if caption_path.is_file() else None,
        subject_path=subject.resolve() if subject else None,
        middle_text=middle_text,
        front_label=front_label,
        revision_id=revision_id,
        focus_pacing_plan_path=focus_pacing_plan.resolve() if focus_pacing_plan else None,
        retimed_timeline_path=retimed_timeline.resolve() if retimed_timeline else None,
        approved_zoom_ids=set(approved_zoom_id) if approved_zoom_id else None,
        transition_plan_path=transition_path.resolve() if transition_path.is_file() else None,
        approved_transition_ids=(set(approved_transition_id) if approved_transition_id else None),
    )
    typer.echo(
        json.dumps(
            {
                "timeline": str(result.timeline_path),
                "code_bundle_sha256": result.code_bundle_sha256,
                "composition_bundle": str(result.composition_bundle_path),
                "staged_assets": list(result.staged_assets),
                "transition_warnings": list(result.transition_warnings),
            },
            indent=2,
        )
    )


@app.command("list-compositions")
def list_compositions(
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    typer.echo(service.list_compositions().encode("ascii", errors="replace").decode("ascii"))


@app.command()
def render(
    timeline: Path,
    output: Path,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    validated = service.load_timeline(timeline)
    service.write_props(validated, timeline)
    service.render(timeline, output)
    typer.echo(str(output))


@app.command("render-still")
def render_still(
    timeline: Path,
    output: Path,
    frame: Annotated[int, typer.Option(help="Zero-based frame to render")] = 0,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    validated = service.load_timeline(timeline.resolve(), package_root=_package_root())
    service.write_props(validated, timeline.resolve())
    service.render_still(timeline.resolve(), output.resolve(), frame=frame)
    typer.echo(str(output.resolve()))


@app.command("render-segment")
def render_segment(
    timeline: Path,
    output: Path,
    start_frame: Annotated[int, typer.Option(help="First frame, inclusive")],
    end_frame: Annotated[int, typer.Option(help="Last frame, inclusive")],
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    validated = service.load_timeline(timeline.resolve(), package_root=_package_root())
    service.write_props(validated, timeline.resolve())
    service.render_segment(
        timeline.resolve(),
        output.resolve(),
        start_frame=start_frame,
        end_frame=end_frame,
    )
    typer.echo(str(output.resolve()))


@app.command("preview-segments")
def preview_segments(
    project_id: str,
    media: Annotated[Path, typer.Option(help="Project-local media to preview")],
    transcript: Annotated[
        Path | None, typer.Option(help="Optional project-local canonical transcript JSON")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Optional segment preview plan JSON")] = None,
    revision_id: Annotated[
        str, typer.Option(help="Revision bound to the preview plan")
    ] = "rev_001",
    max_segment_seconds: Annotated[
        float, typer.Option(help="Maximum logical segment duration in seconds")
    ] = 10.0,
    context_before_ms: Annotated[
        int, typer.Option(help="Context before a transcript group in milliseconds")
    ] = 1000,
    context_after_ms: Annotated[
        int, typer.Option(help="Context after a transcript group in milliseconds")
    ] = 1000,
    gap_merge_ms: Annotated[
        int, typer.Option(help="Maximum speech gap to merge in milliseconds")
    ] = 1000,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    if max_segment_seconds <= 0:
        raise typer.BadParameter("max-segment-seconds must be positive")
    if min(context_before_ms, context_after_ms, gap_merge_ms) < 0:
        raise typer.BadParameter("context and gap values must be nonnegative")
    plan_path = write_segment_preview_plan(
        _package_root(),
        layout,
        media.resolve(),
        transcript.resolve() if transcript else None,
        output=output.resolve() if output else None,
        revision_id=revision_id,
        max_segment_duration_us=round(max_segment_seconds * 1_000_000),
        context_before_us=context_before_ms * 1000,
        context_after_us=context_after_ms * 1000,
        gap_merge_us=gap_merge_ms * 1000,
    )
    typer.echo(str(plan_path))


@app.command("review-segments")
def review_segments(
    project_id: str,
    preview_plan: Annotated[
        Path | None, typer.Option(help="Logical segment preview plan JSON")
    ] = None,
    transcript: Annotated[
        Path | None, typer.Option(help="Optional canonical transcript JSON")
    ] = None,
    artifact: Annotated[
        list[Path] | None,
        typer.Option(help="Current effect or asset artifact; repeat for multiple inputs"),
    ] = None,
    revision_id: Annotated[
        str, typer.Option(help="Revision bound to the review package")
    ] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = (preview_plan or layout.artifacts / "segment-preview.json").resolve()
    outputs = build_segment_review_packages(
        _package_root(),
        layout,
        selected_plan,
        transcript_path=transcript.resolve() if transcript else None,
        review_artifact_paths=[path.resolve() for path in artifact] if artifact else None,
        revision_id=revision_id,
    )
    typer.echo(json.dumps([str(path) for path in outputs], indent=2))


@app.command("import-review-markers")
def import_review_markers_command(
    project_id: str,
    markdown: Annotated[Path, typer.Option(help="Review Markdown containing timestamped markers")],
    package: Annotated[
        Path | None, typer.Option(help="Hash-bound segment review package JSON")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Optional review marker artifact JSON")
    ] = None,
    revision_id: Annotated[
        str, typer.Option(help="Revision bound to the marker import")
    ] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    marker_path = import_review_markers(
        _package_root(),
        layout,
        markdown.resolve(),
        package_path=package.resolve() if package else None,
        output=output.resolve() if output else None,
        revision_id=revision_id,
    )
    typer.echo(str(marker_path))


@app.command("apply-review-markers")
def apply_review_markers_command(
    project_id: str,
    markers: Annotated[Path, typer.Option(help="Schema-valid imported review marker artifact")],
    new_revision_id: Annotated[
        str | None, typer.Option(help="Optional explicit new revision id")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    request_path = apply_review_markers(
        _package_root(),
        layout,
        markers.resolve(),
        new_revision_id=new_revision_id,
    )
    typer.echo(str(request_path))


@app.command("recut-revision")
def recut_revision_command(
    project_id: str,
    revision_request: Annotated[
        Path, typer.Option(help="Schema-valid immutable revision request JSON")
    ],
    source: Annotated[Path, typer.Option(help="Project-local immutable source media")],
    video_codec: Annotated[
        str | None, typer.Option(help="Optional explicit video codec for the repair")
    ] = None,
    audio_codec: Annotated[str, typer.Option(help="Audio codec for the repair")] = "aac",
    qp: Annotated[
        int | None, typer.Option(help="Explicit H.264 QP; use 0 for lossless libx264")
    ] = None,
    preset: Annotated[str, typer.Option(help="FFmpeg video encoder preset")] = "medium",
    strict_decode: Annotated[
        bool, typer.Option(help="Use FFmpeg -xerror during repair validation")
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    manifest_path = recut_revision(
        _package_root(),
        layout,
        revision_request.resolve(),
        source.resolve(),
        video_codec=video_codec,
        audio_codec=audio_codec,
        qp=qp,
        preset=preset,
        strict_decode=strict_decode,
    )
    typer.echo(str(manifest_path))


@app.command("retranscribe-revision")
def retranscribe_revision_command(
    project_id: str,
    revision_media: Annotated[Path, typer.Option(help="Schema-valid revision media manifest JSON")],
    transcript: Annotated[Path, typer.Option(help="Canonical intended source transcript JSON")],
    model_name: Annotated[str | None, typer.Option(help="Local Whisper model name")] = None,
    model_path: Annotated[
        Path | None, typer.Option(help="Operator-supplied local Whisper model file")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_transcriber = _configured_whisper_adapter(model_path)
    comparison_path = retranscribe_revision(
        _package_root(),
        layout,
        revision_media.resolve(),
        transcript.resolve(),
        model_name=_configured_whisper_model(model_name),
        transcriber=selected_transcriber,
    )
    typer.echo(str(comparison_path))


@app.command("slice-segment-comparisons")
def slice_segment_comparisons_command(
    project_id: str,
    preview_plan: Annotated[Path, typer.Option(help="Schema-valid segment preview plan JSON")],
    comparison: Annotated[
        Path, typer.Option(help="Current revision-wide segment transcript comparison JSON")
    ],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output_paths = write_segment_transcript_comparisons(
        _package_root(),
        layout,
        preview_plan.resolve(),
        comparison.resolve(),
    )
    for output_path in output_paths:
        typer.echo(str(output_path))


@app.command("qa-segment")
def qa_segment_command(
    project_id: str,
    revision_media: Annotated[Path, typer.Option(help="Schema-valid revision media manifest JSON")],
    comparison: Annotated[
        Path | None, typer.Option(help="Optional rendered transcript comparison JSON")
    ] = None,
    caption_plan: Annotated[Path | None, typer.Option(help="Optional caption plan JSON")] = None,
    join_report: Annotated[
        Path | None, typer.Option(help="Optional rendered join QA report JSON")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    report_path = qa_segment_revision(
        _package_root(),
        layout,
        revision_media.resolve(),
        comparison_path=comparison.resolve() if comparison else None,
        caption_plan_path=caption_plan.resolve() if caption_plan else None,
        join_report_path=join_report.resolve() if join_report else None,
    )
    typer.echo(str(report_path))


@app.command("approve-qa-override")
def approve_qa_override_command(
    project_id: str,
    qa_report: Annotated[Path, typer.Option(help="Current schema-valid QA report JSON")],
    finding: Annotated[
        list[str],
        typer.Option(
            "--finding",
            help="Warning finding and retained evidence as FINDING_ID=PATH; repeatable",
        ),
    ],
    actor: Annotated[str, typer.Option(help="Human reviewer identity")],
    role: Annotated[str, typer.Option(help="Human reviewer role")],
    reason: Annotated[str, typer.Option(help="Why the warning is overridden")],
    classification: Annotated[
        str,
        typer.Option(
            help="reviewed_non_defect, intentional_static, false_positive, or accepted_risk"
        ),
    ] = "reviewed_non_defect",
    notes: Annotated[str, typer.Option(help="Additional review notes")] = "",
    expires_at: Annotated[
        str | None, typer.Option(help="Optional RFC3339 expiry timestamp")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Optional project-local output path")] = None,
    revision_id: Annotated[
        str | None, typer.Option(help="Expected QA report revision; defaults to the report")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    evidence_by_finding = _parse_qa_override_finding_specs(finding)
    created = create_qa_override(
        _package_root(),
        layout,
        qa_report.resolve(),
        evidence_by_finding,
        actor=actor,
        role=role,
        reason=reason,
        classification=classification,
        notes=notes,
        expires_at=expires_at,
        output_path=output.resolve() if output else None,
        revision_id=revision_id,
    )
    typer.echo(str(created))


@app.command("check-qa-override")
def check_qa_override_command(
    project_id: str,
    qa_report: Annotated[Path, typer.Option(help="Current schema-valid QA report JSON")],
    override: Annotated[Path, typer.Option(help="Hash-bound QA override JSON")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    result = evaluate_qa_override(_package_root(), layout, qa_report.resolve(), override.resolve())
    typer.echo(json.dumps(result, indent=2))
    if result["status"] != "ready":
        raise typer.Exit(code=10)


@app.command("qa-review-packet")
def qa_review_packet_command(
    project_id: str,
    candidate: Annotated[Path, typer.Option(help="Current candidate MP4")],
    final_qa: Annotated[Path, typer.Option(help="Current source-specific final QA JSON")],
    join_qa: Annotated[Path, typer.Option(help="Current rendered join QA JSON")],
    segment_qa: Annotated[Path, typer.Option(help="Current segment QA JSON")],
    review_gate: Annotated[str, typer.Option(help="gate2 or gate3 review packet")] = "gate3",
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_002",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_qa_review_packet(
        _package_root(),
        layout,
        candidate.resolve(),
        final_qa_path=final_qa.resolve(),
        join_qa_path=join_qa.resolve(),
        segment_qa_path=segment_qa.resolve(),
        revision_id=revision_id,
        review_gate=review_gate,
    )
    typer.echo(str(output))


@app.command("qa-review-visuals")
def qa_review_visuals_command(
    project_id: str,
    packet: Annotated[Path, typer.Option(help="Current hash-bound QA review packet JSON")],
    revision_id: Annotated[
        str | None, typer.Option(help="Expected packet revision; defaults to the packet")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Optional project-local output path")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = write_qa_review_visual_evidence(
        _package_root(),
        layout,
        packet.resolve(),
        revision_id=revision_id,
        output_path=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("record-qa-review")
def record_qa_review_command(
    project_id: str,
    packet: Annotated[Path, typer.Option(help="Current hash-bound QA review packet JSON")],
    decision: Annotated[list[str], typer.Option("--decision", help="ITEM_ID=DECISION; repeatable")],
    actor: Annotated[str, typer.Option(help="Human reviewer identity")],
    role: Annotated[str, typer.Option(help="Human reviewer role")],
    reason: Annotated[str, typer.Option(help="Reason applied to each selected item")],
    evidence: Annotated[
        list[str] | None,
        typer.Option("--evidence", help="Optional ITEM_ID=EVIDENCE_PATH; repeatable"),
    ] = None,
    notes: Annotated[str, typer.Option(help="Additional review notes")] = "",
    revision_id: Annotated[
        str | None, typer.Option(help="Expected packet revision; defaults to the packet")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Optional project-local output path")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    decisions = _parse_qa_review_decision_specs(decision)
    evidence_by_item = _parse_qa_review_evidence_specs(evidence or [])
    created = write_qa_review_decision(
        _package_root(),
        layout,
        packet.resolve(),
        decisions,
        actor=actor,
        role=role,
        reason=reason,
        evidence_by_item=evidence_by_item,
        notes=notes,
        revision_id=revision_id,
        output_path=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("qa-visual-segment")
def qa_visual_segment_command(
    project_id: str,
    review_package: Annotated[Path, typer.Option(help="Hash-bound segment review package JSON")],
    visual_timeline: Annotated[
        Path | None, typer.Option(help="Optional schema-valid visual timeline JSON")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    report_path = qa_visual_segment(
        _package_root(),
        layout,
        review_package.resolve(),
        visual_timeline_path=visual_timeline.resolve() if visual_timeline else None,
    )
    typer.echo(str(report_path))


@app.command("approve-segment")
def approve_segment_command(
    project_id: str,
    review_package: Annotated[Path, typer.Option(help="Hash-bound segment review package JSON")],
    transcript_comparison: Annotated[
        Path, typer.Option(help="Rendered transcript comparison JSON")
    ],
    segment_qa: Annotated[Path, typer.Option(help="Segment QA report JSON")],
    visual_qa: Annotated[Path, typer.Option(help="Segment visual QA report JSON")],
    composition_bundle: Annotated[
        Path, typer.Option(help="Composition code bundle or manifest to bind")
    ],
    actor: Annotated[str, typer.Option(help="Human reviewer identity")],
    role: Annotated[str, typer.Option(help="Human reviewer role")],
    decision: Annotated[
        str, typer.Option(help="approved, changes_requested, or rejected")
    ] = "approved",
    notes: Annotated[str, typer.Option(help="Reviewer notes")] = "",
    qa_override: Annotated[
        Path | None,
        typer.Option(help="Optional current hash-bound QA override for warning findings"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    review_path = approve_segment_gate2(
        _package_root(),
        layout,
        review_package.resolve(),
        transcript_comparison.resolve(),
        segment_qa.resolve(),
        visual_qa.resolve(),
        composition_bundle.resolve(),
        actor=actor,
        role=role,
        decision=decision,
        notes=notes,
        qa_override_path=qa_override.resolve() if qa_override else None,
    )
    typer.echo(str(review_path))


@app.command("lock-segment")
def lock_segment_command(
    project_id: str,
    review: Annotated[Path, typer.Option(help="Approved Gate 2 segment review JSON")],
    review_package: Annotated[Path, typer.Option(help="Hash-bound segment review package JSON")],
    transcript_comparison: Annotated[
        Path, typer.Option(help="Rendered transcript comparison JSON")
    ],
    segment_qa: Annotated[Path, typer.Option(help="Segment QA report JSON")],
    visual_qa: Annotated[Path, typer.Option(help="Segment visual QA report JSON")],
    composition_bundle: Annotated[Path, typer.Option(help="Composition code bundle or manifest")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    lock_path = lock_segment_revision(
        _package_root(),
        layout,
        review.resolve(),
        review_package.resolve(),
        transcript_comparison.resolve(),
        segment_qa.resolve(),
        visual_qa.resolve(),
        composition_bundle.resolve(),
    )
    typer.echo(str(lock_path))


@app.command("plan-marker-focus")
def plan_marker_focus_command(
    project_id: str,
    markers: Annotated[Path, typer.Option(help="Schema-valid imported review markers JSON")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    plan_path = build_marker_focus_pacing_plan(_package_root(), layout, markers.resolve())
    typer.echo(str(plan_path))


@app.command("make-demo")
def make_demo(
    project_id: str,
    render_output: Annotated[
        bool, typer.Option("--render/--no-render", help="Render Remotion passes")
    ] = False,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    result = build_demo(
        _workspace_path(workspace),
        project_id,
        render=render_output,
        adapter=_configured_ffmpeg_adapter(),
        npm_path=_configured_remotion_npm_path(),
    )
    typer.echo(json.dumps(result, indent=2))


@app.command("encode-mask")
def encode_mask(
    segmentation_result: Path,
    output: Path,
    fps: Annotated[int, typer.Option(help="Mask frame rate")] = 30,
) -> None:
    encoded = encode_segmentation_masks(
        _package_root(),
        segmentation_result.resolve(),
        output.resolve(),
        fps=fps,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(encoded))


@app.command("chroma-key")
def chroma_key(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Local source video to key")],
    output: Annotated[Path | None, typer.Option(help="Project-local transparent output")] = None,
    key_color: Annotated[str, typer.Option(help="FFmpeg key color as 0xRRGGBB")] = "0x00FF00",
    similarity: Annotated[float, typer.Option(help="FFmpeg chromakey similarity")] = 0.18,
    blend: Annotated[float, typer.Option(help="FFmpeg chromakey blend")] = 0.08,
    despill: Annotated[
        bool, typer.Option("--despill/--no-despill", help="Apply FFmpeg despill")
    ] = True,
    despill_color: Annotated[str, typer.Option(help="Despill color family")] = "green",
    despill_mix: Annotated[float, typer.Option(help="Despill mix")] = 0.5,
    edge_feather_px: Annotated[float, typer.Option(help="Alpha edge blur radius")] = 0.0,
    edge_erode_iterations: Annotated[int, typer.Option(help="Alpha edge erosion iterations")] = 0,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    from videoedit.adapters.ffmpeg import ChromaKeyConfig

    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    if not layout.root.is_dir():
        raise typer.BadParameter(f"project does not exist: {layout.root}")
    manifest_path = render_chroma_key_foreground(
        _package_root(),
        layout,
        source.resolve(),
        output.resolve() if output else None,
        config=ChromaKeyConfig(
            key_color=key_color,
            similarity=similarity,
            blend=blend,
            despill=despill,
            despill_color=despill_color,
            despill_mix=despill_mix,
            edge_feather_px=edge_feather_px,
            edge_erode_iterations=edge_erode_iterations,
        ),
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(manifest_path.read_text(encoding="utf-8"))


@app.command("validate-mask")
def validate_mask(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Source video aligned with the mask")],
    mask: Annotated[Path, typer.Option(help="Lossless local grayscale mask video")],
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    manifest_path = validate_local_mask(
        _package_root(),
        layout,
        source.resolve(),
        mask.resolve(),
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(manifest_path.read_text(encoding="utf-8"))


@app.command("validate-segmentation")
def validate_segmentation(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Source video used by the segmentation job")],
    result: Annotated[Path, typer.Option(help="SAM segmentation-result.json")],
    job: Annotated[Path | None, typer.Option(help="Hash-bound segmentation job JSON")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    job_payload = json.loads(job.resolve().read_text(encoding="utf-8")) if job is not None else None
    result_payload = json.loads(result.resolve().read_text(encoding="utf-8"))
    validation = validate_segmentation_result(
        _package_root(),
        result.resolve(),
        job=job_payload,
        adapter=_configured_ffmpeg_adapter(),
    )
    contact_sheets = write_segmentation_contact_sheets(
        layout,
        source.resolve(),
        result.resolve(),
        validation,
        adapter=_configured_ffmpeg_adapter(),
    )
    report = write_segmentation_validation(
        _package_root(),
        layout,
        result.resolve(),
        validation,
        source_path=source.resolve(),
        source_range=result_payload.get("source_range")
        or (job_payload or {}).get("source_range", {}),
        contact_sheets=contact_sheets,
        revision_id=(job_payload or {}).get("revision_id", "rev_001"),
    )
    typer.echo(report.read_text(encoding="utf-8"))
    if validation.status != "pass":
        raise typer.Exit(code=10)


@app.command("recolor-mask")
def recolor_mask(
    project_id: str,
    source: Annotated[Path, typer.Option(help="Source video to recolor")],
    mask: Annotated[Path, typer.Option(help="Validated lossless local grayscale mask")],
    output: Annotated[Path | None, typer.Option(help="Project-local recolored output")] = None,
    hue_degrees: Annotated[float, typer.Option(help="Hue rotation applied inside the mask")] = 100,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    manifest_path = recolor_local_mask(
        _package_root(),
        layout,
        source.resolve(),
        mask.resolve(),
        output.resolve() if output else None,
        hue_degrees=hue_degrees,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(manifest_path.read_text(encoding="utf-8"))


@app.command()
def recolor(
    source: Path,
    mask: Path,
    output: Path,
    hue_degrees: Annotated[
        float, typer.Option(help="Hue rotation applied inside the tracked mask")
    ] = 100,
) -> None:
    _configured_ffmpeg_adapter().recolor_with_mask(
        source.resolve(), mask.resolve(), output.resolve(), hue_degrees=hue_degrees
    )
    typer.echo(str(output.resolve()))


@app.command("prepare-matte")
def prepare_matte(
    matting_result: Path,
    output: Path,
) -> None:
    prepared = prepare_matting_overlay(
        _package_root(),
        matting_result.resolve(),
        output.resolve(),
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(prepared))


@app.command("verify-matte")
def verify_matte(
    result: Path,
    output: Annotated[
        Path | None,
        typer.Option(help="New verified-result artifact; raw worker result is preserved"),
    ] = None,
    require_contrast: Annotated[
        bool,
        typer.Option(
            "--require-contrast/--allow-pending-contrast",
            help="Require an already recorded contrasting-background review",
        ),
    ] = False,
) -> None:
    verified = verify_matting_result(
        _package_root(),
        result.resolve(),
        output_path=output.resolve() if output else None,
        require_contrasting_background=require_contrast,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(verified))


@app.command("review-matte-contrast")
def review_matte_contrast(
    result: Path,
    output: Annotated[
        Path | None,
        typer.Option(help="Separate output directory for the contrast review package"),
    ] = None,
) -> None:
    manifest = render_matting_contrast_previews(
        _package_root(),
        result.resolve(),
        output_dir=output.resolve() if output else None,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(manifest))


@app.command("review-matte-quality")
def review_matte_quality(
    result: Path,
    contrast_review: Annotated[
        Path,
        typer.Option(help="Hash-bound black/white contrast review manifest"),
    ],
    output: Annotated[
        Path | None,
        typer.Option(help="New pending matte-quality review artifact"),
    ] = None,
) -> None:
    report = build_matting_quality_review(
        _package_root(),
        result.resolve(),
        contrast_review.resolve(),
        output_path=output.resolve() if output else None,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(report))


@app.command("review-track")
def review_track(
    result: Path,
    segmentation_validation: Annotated[
        Path,
        typer.Option(help="Structural segmentation validation report"),
    ],
    actor: Annotated[str, typer.Option(help="Human reviewer identity")],
    output: Annotated[
        Path | None,
        typer.Option(help="Immutable object-track review artifact"),
    ] = None,
    object_id: Annotated[int, typer.Option(help="Stable SAM object ID")] = 1,
    decision: Annotated[
        str,
        typer.Option(help="pending, approved, or rejected"),
    ] = "pending",
    identity: Annotated[str, typer.Option(help="Identity finding status")] = "pending",
    continuity: Annotated[str, typer.Option(help="Continuity finding status")] = "pending",
    geometry: Annotated[str, typer.Option(help="Geometry finding status")] = "pending",
    occlusion: Annotated[str, typer.Option(help="Occlusion finding status")] = "pending",
    note: Annotated[list[str] | None, typer.Option(help="Reviewer note")] = None,
) -> None:
    review = write_object_track_review(
        _package_root(),
        result.resolve(),
        segmentation_validation.resolve(),
        output_path=output.resolve() if output else None,
        object_id=object_id,
        actor=actor,
        decision=decision,
        findings={
            "identity": identity,
            "continuity": continuity,
            "geometry": geometry,
            "occlusion": occlusion,
        },
        notes=note or (),
    )
    typer.echo(str(review))


@app.command("compile-track-keyframes")
def compile_track_keyframes(
    result: Path,
    segmentation_validation: Annotated[
        Path,
        typer.Option(help="Structural segmentation validation report"),
    ],
    track_review: Annotated[
        Path,
        typer.Option(help="Approved object-track review artifact"),
    ],
    output: Annotated[
        Path | None,
        typer.Option(help="Hash-bound keyframe manifest"),
    ] = None,
    width: Annotated[int, typer.Option(help="Remotion timeline width")] = 1920,
    height: Annotated[int, typer.Option(help="Remotion timeline height")] = 1080,
    object_id: Annotated[int, typer.Option(help="Stable SAM object ID")] = 1,
    padding: Annotated[float, typer.Option(help="Replacement box padding multiplier")] = 1.2,
    window_radius_frames: Annotated[
        int,
        typer.Option(help="Centered smoothing radius, limited to ten frames"),
    ] = 1,
    frame_rate: Annotated[
        str | None,
        typer.Option(help="Optional source frame rate such as 30000/1001"),
    ] = None,
) -> None:
    manifest = write_object_track_keyframes(
        _package_root(),
        result.resolve(),
        segmentation_validation.resolve(),
        track_review.resolve(),
        output_path=output.resolve() if output else None,
        timeline_width=width,
        timeline_height=height,
        object_id=object_id,
        padding=padding,
        window_radius_frames=window_radius_frames,
        frame_rate=frame_rate,
    )
    typer.echo(str(manifest))


@app.command("replace-object")
def replace_object(
    timeline: Path,
    segmentation_result: Path,
    segmentation_validation: Annotated[
        Path,
        typer.Option(help="Structural segmentation validation report"),
    ],
    track_review: Annotated[
        Path,
        typer.Option(help="Approved object-track review artifact"),
    ],
    asset_catalog: Annotated[
        Path,
        typer.Option(help="Current licensed local asset catalog"),
    ],
    asset_manifest: Annotated[
        Path,
        typer.Option(help="Current project asset selection manifest"),
    ],
    asset_id: Annotated[str, typer.Option(help="Licensed replacement_object asset ID")],
    output: Path,
    keyframes: Annotated[
        Path,
        typer.Option(help="Approved object-track keyframe manifest"),
    ],
    replacement_manifest: Annotated[
        Path | None,
        typer.Option(help="Provenance-bound replacement manifest"),
    ] = None,
    layer_id: Annotated[str, typer.Option(help="Remotion layer ID")] = "tracked-replacement",
    z_index: Annotated[int, typer.Option(help="Remotion layer z-index")] = 30,
    padding: Annotated[float, typer.Option(help="Replacement box padding multiplier")] = 1.2,
    start_frame_offset: Annotated[
        int, typer.Option(help="Add this many frames before the source range")
    ] = 0,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    manifest_path = write_object_replacement_manifest(
        _package_root(),
        keyframes.resolve(),
        asset_catalog.resolve(),
        asset_manifest.resolve(),
        asset_id,
        output_path=replacement_manifest.resolve() if replacement_manifest else None,
        layer_id=layer_id,
        z_index=z_index,
    )
    replacement_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    keyframe_payload = json.loads(keyframes.resolve().read_text(encoding="utf-8"))
    asset_file = Path(str(replacement_payload["asset"]["file"]["path"])).resolve()
    asset_sha256 = str(replacement_payload["asset"]["file"]["sha256"])
    service = _configured_remotion_service(remotion_directory)
    timeline_payload = service.load_timeline(timeline.resolve())
    staged_src = service.stage_asset(
        timeline_payload.project_id,
        asset_file,
        expected_sha256=asset_sha256,
    )
    created = append_tracked_image_layer(
        _package_root(),
        timeline.resolve(),
        segmentation_result.resolve(),
        staged_src,
        output.resolve(),
        segmentation_validation_path=segmentation_validation.resolve(),
        track_review_path=track_review.resolve(),
        keyframe_manifest_path=keyframes.resolve(),
        object_id=int(keyframe_payload["object_id"]),
        layer_id=layer_id,
        z_index=z_index,
        padding=padding,
        start_frame_offset=start_frame_offset,
        asset_id=asset_id,
        asset_sha256=asset_sha256,
    )
    typer.echo(json.dumps({"replacement_manifest": str(manifest_path), "timeline": str(created)}))


@app.command("search-assets")
def search_assets(
    project_id: str,
    catalog: Path,
    query: str,
    output: Annotated[Path | None, typer.Option(help="Project-local search result output")] = None,
    effect_intent: Annotated[str, typer.Option(help="Effect intent terms")] = "",
    asset_type: Annotated[str | None, typer.Option(help="Optional exact asset type filter")] = None,
    required_tag: Annotated[
        list[str] | None, typer.Option(help="Required catalog tag; repeat")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum result count")] = 10,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = search_local_assets(
        _package_root(),
        layout,
        catalog.resolve(),
        query=query,
        effect_intent=effect_intent,
        asset_type=asset_type,
        required_tags=required_tag or (),
        limit=limit,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("index-assets")
def index_assets(
    project_id: str,
    asset_root: Path,
    metadata: Path,
    output: Annotated[
        Path | None, typer.Option(help="Project-local indexed catalog output")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = index_local_asset_catalog(
        _package_root(),
        layout,
        asset_root.resolve(),
        metadata.resolve(),
        output.resolve() if output else None,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(created))


@app.command("plan-cues")
def plan_cues(
    project_id: str,
    transition_plan: Annotated[
        Path | None, typer.Option(help="Current structural transition plan JSON")
    ] = None,
    catalog: Annotated[
        Path | None, typer.Option(help="Current licensed local asset catalog JSON")
    ] = None,
    search_result: Annotated[
        Path | None, typer.Option(help="Transcript/effect-bound local asset search result JSON")
    ] = None,
    assets_policy: Annotated[
        Path | None, typer.Option(help="Local asset density and sound policy YAML")
    ] = None,
    transitions_policy: Annotated[
        Path | None, typer.Option(help="Structural motion density policy YAML")
    ] = None,
    timeline_duration_us: Annotated[
        int, typer.Option(help="Output timeline duration in integer microseconds")
    ] = 60_000_000,
    broll_start_us: Annotated[
        int | None, typer.Option(help="Optional local B-roll start time in microseconds")
    ] = None,
    broll_end_us: Annotated[
        int | None, typer.Option(help="Optional local B-roll end time in microseconds")
    ] = None,
    broll_context: Annotated[
        str, typer.Option(help="Transcript context for the optional B-roll request")
    ] = "",
    broll_rationale: Annotated[
        str, typer.Option(help="Editorial rationale for the optional B-roll request")
    ] = "",
    output: Annotated[Path | None, typer.Option(help="Project-local cue bundle output")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_transition = transition_plan or layout.artifacts / "transition-plan.json"
    selected_catalog = catalog or layout.artifacts / "asset-catalog.json"
    if not selected_transition.is_file():
        raise typer.BadParameter(f"transition plan does not exist: {selected_transition}")
    if not selected_catalog.is_file():
        raise typer.BadParameter(f"asset catalog does not exist: {selected_catalog}")
    if (broll_start_us is None) != (broll_end_us is None):
        raise typer.BadParameter("--broll-start-us and --broll-end-us must be supplied together")
    windows: list[dict[str, object]] = []
    if broll_start_us is not None and broll_end_us is not None:
        windows.append(
            {
                "start_us": broll_start_us,
                "end_us": broll_end_us,
                "transcript_context": broll_context,
                "rationale": broll_rationale,
            }
        )
    created = write_cue_plan_bundle(
        _package_root(),
        layout,
        selected_transition.resolve(),
        selected_catalog.resolve(),
        search_result_path=search_result.resolve() if search_result else None,
        assets_policy_path=assets_policy.resolve() if assets_policy else None,
        transitions_policy_path=transitions_policy.resolve() if transitions_policy else None,
        timeline_duration_us=timeline_duration_us,
        broll_windows=windows,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("approve-cues")
def approve_cues(
    project_id: str,
    actor: Annotated[str, typer.Option(help="Human reviewer approving the cue bundle")],
    bundle: Annotated[Path | None, typer.Option(help="Cue plan bundle JSON")] = None,
    output: Annotated[Path | None, typer.Option(help="Project-local approval output")] = None,
    role: Annotated[str, typer.Option(help="Reviewer role")] = "editor",
    reason: Annotated[str, typer.Option(help="Approval reason")] = "Cue plan approved after review",
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_bundle = bundle or layout.artifacts / "cue-plan-bundle.json"
    if not selected_bundle.is_file():
        raise typer.BadParameter(f"cue plan bundle does not exist: {selected_bundle}")
    created = approve_cue_plan_bundle(
        _package_root(),
        layout,
        selected_bundle.resolve(),
        actor=actor,
        role=role,
        reason=reason,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("plan-inpainting")
def plan_inpainting(
    project_id: str,
    source: Path,
    mask_validation: Path,
    output: Annotated[Path | None, typer.Option(help="Project-local request JSON output")] = None,
    start_frame: Annotated[int, typer.Option(help="First approved source frame, inclusive")] = 0,
    end_frame: Annotated[int, typer.Option(help="Last approved source frame, exclusive")] = 1,
    prompt: Annotated[str, typer.Option(help="Provider-neutral fill instruction")] = (
        "Fill the reviewed object region with surrounding background texture."
    ),
    provider: Annotated[str, typer.Option(help="Configured provider identifier")] = "disabled",
    model: Annotated[str, typer.Option(help="Configured model identifier")] = "operator-configured",
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = plan_inpainting_request(
        _package_root(),
        layout,
        source.resolve(),
        mask_validation.resolve(),
        start_frame=start_frame,
        end_frame=end_frame,
        prompt=prompt,
        provider=provider,
        model=model,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("submit-inpainting")
def submit_inpainting(
    project_id: str,
    request: Path,
    effect_approval: Path,
    spend_approval: Path,
    command: Annotated[
        str | None, typer.Option(help="Override the configured adapter command")
    ] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    settings = Settings()
    selected_command = command or settings.inpainting_provider_command
    if not selected_command or not selected_command.strip():
        raise typer.BadParameter(
            "Configure VIDEOEDIT_INPAINTING_PROVIDER_COMMAND or pass --command"
        )
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    response = submit_inpainting_request(
        _package_root(),
        layout,
        request.resolve(),
        effect_approval.resolve(),
        spend_approval.resolve(),
        adapter=CommandInpaintingAdapter(
            selected_command,
            network_enabled=settings.provider_network_enabled,
        ),
    )
    typer.echo(json.dumps(response, indent=2))


@app.command("render-occluder")
def render_occluder(
    project_id: str,
    segmentation_result: Path,
    segmentation_validation: Path,
    track_review: Path,
    output: Annotated[
        Path | None, typer.Option(help="Project-local transparent occluder output")
    ] = None,
    manifest: Annotated[
        Path | None, typer.Option(help="Project-local occluder manifest JSON")
    ] = None,
    object_id: Annotated[
        int, typer.Option(help="Stable SAM object ID for the foreground occluder")
    ] = 1,
    layer_id: Annotated[str, typer.Option(help="Remotion front-layer ID")] = "tracked-occluder",
    z_index: Annotated[int, typer.Option(help="Remotion front-layer z-index")] = 40,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = render_tracked_occluder(
        _package_root(),
        layout,
        segmentation_result.resolve(),
        segmentation_validation.resolve(),
        track_review.resolve(),
        output=output.resolve() if output else None,
        manifest_output=manifest.resolve() if manifest else None,
        object_id=object_id,
        layer_id=layer_id,
        z_index=z_index,
        revision_id=revision_id,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(created))


@app.command("add-occluder-layer")
def add_occluder_layer(
    timeline: Path,
    manifest: Path,
    output: Path,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    timeline_payload = service.load_timeline(timeline.resolve())
    manifest_payload = json.loads(manifest.resolve().read_text(encoding="utf-8"))
    output_path = Path(str(manifest_payload["output"]["path"])).resolve()
    output_sha256 = str(manifest_payload["output"]["sha256"])
    staged_src = service.stage_asset(
        timeline_payload.project_id,
        output_path,
        expected_sha256=output_sha256,
    )
    created = append_occluder_video_layer(
        _package_root(),
        timeline.resolve(),
        manifest.resolve(),
        staged_src,
        output.resolve(),
        asset_sha256=output_sha256,
    )
    typer.echo(str(created))


@app.command("track-overlay")
def track_overlay(
    timeline: Path,
    segmentation_result: Path,
    asset: Path,
    output: Path,
    segmentation_validation: Annotated[
        Path,
        typer.Option(help="Structural segmentation validation report"),
    ],
    track_review: Annotated[
        Path,
        typer.Option(help="Approved object-track review artifact"),
    ],
    object_id: Annotated[int, typer.Option(help="Stable SAM object ID")] = 1,
    layer_id: Annotated[str, typer.Option(help="Remotion layer ID")] = "tracked-replacement",
    z_index: Annotated[int, typer.Option(help="Remotion layer z-index")] = 30,
    padding: Annotated[float, typer.Option(help="Replacement box padding multiplier")] = 1.2,
    start_frame_offset: Annotated[
        int, typer.Option(help="Add this many frames before the SAM source range")
    ] = 0,
    remotion_directory: Annotated[Path, typer.Option(help="Remotion project directory")] = Path(
        "remotion"
    ),
) -> None:
    service = _configured_remotion_service(remotion_directory)
    timeline_payload = service.load_timeline(timeline.resolve())
    staged_src = service.stage_asset(timeline_payload.project_id, asset.resolve())
    created = append_tracked_image_layer(
        _package_root(),
        timeline.resolve(),
        segmentation_result.resolve(),
        staged_src,
        output.resolve(),
        segmentation_validation_path=segmentation_validation.resolve(),
        track_review_path=track_review.resolve(),
        object_id=object_id,
        layer_id=layer_id,
        z_index=z_index,
        padding=padding,
        start_frame_offset=start_frame_offset,
    )
    typer.echo(str(created))


@app.command("run-worker")
def run_worker(
    worker: Annotated[str, typer.Argument(help="sam3 or matanyone2")],
    job: Path,
    command: Annotated[
        str | None, typer.Option(help="Override the configured worker command")
    ] = None,
) -> None:
    settings = Settings()
    configured = {
        "sam3": settings.sam3_worker_command,
        "matanyone2": settings.matanyone_worker_command,
    }
    if worker not in configured:
        raise typer.BadParameter("worker must be sam3 or matanyone2")
    selected = command or configured[worker]
    if not selected.strip():
        raise typer.BadParameter(
            f"Configure VIDEOEDIT_{worker.upper()}_WORKER_COMMAND or pass --command"
        )
    schema_names = {
        "sam3": "segmentation",
        "matanyone2": "matting",
    }
    schema_name = schema_names[worker]
    payload = WorkerAdapter(
        selected,
        job_schema=_package_root() / "schemas" / f"{schema_name}_job.schema.json",
        result_schema=_package_root() / "schemas" / f"{schema_name}_result.schema.json",
    ).run(job)
    typer.echo(json.dumps(payload, indent=2))


@app.command("approve-worker-runtime")
def approve_worker_runtime_command(
    project_id: str,
    worker: Annotated[str, typer.Argument(help="sam3 or matanyone2")],
    upstream_commit: Annotated[
        str, typer.Option(help="Operator-accepted immutable upstream commit")
    ],
    checkpoint_id: Annotated[str, typer.Option(help="Operator-accepted checkpoint identifier")],
    checkpoint_sha256: Annotated[
        str, typer.Option(help="SHA-256 of the operator-supplied local checkpoint")
    ],
    pytorch: Annotated[str, typer.Option(help="Installed PyTorch identity")],
    cuda: Annotated[str, typer.Option(help="Installed CUDA identity")],
    device: Annotated[str, typer.Option(help="Target device identity")],
    actor: Annotated[str, typer.Option(help="Approving operator or licence owner")],
    role: Annotated[str, typer.Option(help="Approving operator role")] = "operator",
    reason: Annotated[
        str, typer.Option(help="Current licence, checkpoint, and hardware acceptance reason")
    ] = "Approved after licence and target-runtime review",
    output: Annotated[
        Path | None, typer.Option(help="Optional project-local approval output")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = approve_worker_runtime(
        _package_root(),
        layout,
        worker=worker,
        upstream_commit=upstream_commit,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        pytorch=pytorch,
        cuda=cuda,
        device=device,
        actor=actor,
        role=role,
        reason=reason,
        revision_id=revision_id,
        output=output.resolve() if output else None,
    )
    typer.echo(str(created))


@app.command("assemble-final")
def assemble_final(
    project_id: str,
    segment_spec: Annotated[
        list[Path],
        typer.Option(
            "--segment-spec",
            help="JSON array (or object with segments) of approved segment inputs; repeatable",
        ),
    ],
    output: Annotated[
        Path | None, typer.Option(help="Optional final candidate output path")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    if not segment_spec:
        raise typer.BadParameter("at least one --segment-spec is required")
    segments: list[dict[str, object]] = []
    for spec_path in segment_spec:
        value = _read_json_path(spec_path, "segment specification")
        if isinstance(value, dict) and isinstance(value.get("segments"), list):
            values = value["segments"]
        elif isinstance(value, list):
            values = value
        else:
            raise typer.BadParameter(
                "segment specification must be an array or object with segments"
            )
        for item in values:
            if not isinstance(item, dict):
                raise typer.BadParameter("segment specification entries must be objects")
            segments.append(item)
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    created = assemble_approved_segments(
        _package_root(),
        layout,
        segments,
        revision_id=revision_id,
        output=output.resolve() if output else None,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(created))


@app.command("qa-final")
def qa_final(
    project_id: str,
    assembly: Annotated[Path | None, typer.Option(help="Final assembly manifest JSON")] = None,
    source_manifest: Annotated[
        Path | None, typer.Option(help="Current source manifest JSON")
    ] = None,
    asset_manifest: Annotated[
        Path | None, typer.Option(help="Current project asset manifest JSON")
    ] = None,
    composition_bundle: Annotated[Path | None, typer.Option(help="Composition code bundle")] = None,
    plan: Annotated[
        list[str] | None, typer.Option("--plan", help="Plan binding SCHEMA_NAME=PATH; repeatable")
    ] = None,
    caption_plan: Annotated[Path | None, typer.Option(help="Final caption plan JSON")] = None,
    transcript: Annotated[
        Path | None, typer.Option(help="Final word-timed transcript JSON")
    ] = None,
    visual_timeline: Annotated[
        Path | None, typer.Option(help="Persisted visual timeline JSON")
    ] = None,
    visual_evidence: Annotated[
        list[Path] | None,
        typer.Option("--visual-evidence", help="Retained visual proof path; repeatable"),
    ] = None,
    gate2: Annotated[
        list[Path] | None,
        typer.Option("--gate2", help="Locked Gate 2 segment approval; repeatable"),
    ] = None,
    profile_id: Annotated[
        str, typer.Option(help="Delivery profile identifier")
    ] = "pro_youtube_1080p",
    profile: Annotated[
        Path | None, typer.Option(help="JSON delivery profile dimensions and frame rate")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_assembly = assembly or layout.artifacts / "final-assembly.json"
    profile_value: dict[str, Any] | None = None
    if profile is not None:
        value = _read_json_path(profile, "delivery profile")
        if not isinstance(value, dict):
            raise typer.BadParameter("delivery profile must be a JSON object")
        profile_value = value
    output = qa_final_candidate(
        _package_root(),
        layout,
        selected_assembly.resolve(),
        source_manifest_path=source_manifest.resolve() if source_manifest else None,
        asset_manifest_path=asset_manifest.resolve() if asset_manifest else None,
        composition_bundle_path=composition_bundle.resolve() if composition_bundle else None,
        plan_paths=_parse_plan_specs(plan or []),
        caption_plan_path=caption_plan.resolve() if caption_plan else None,
        transcript_path=transcript.resolve() if transcript else None,
        visual_timeline_path=visual_timeline.resolve() if visual_timeline else None,
        visual_evidence_paths=[path.resolve() for path in (visual_evidence or [])],
        gate2_paths=[path.resolve() for path in (gate2 or [])],
        profile_id=profile_id,
        profile=profile_value,
        revision_id=revision_id,
        adapter=_configured_ffmpeg_adapter(),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    typer.echo(json.dumps(payload, indent=2))
    if not payload["final_ready"]:
        raise typer.Exit(code=10)


@app.command("qa-source-candidate")
def qa_source_candidate_command(
    project_id: str,
    candidate: Annotated[Path, typer.Option(help="Worker-free candidate MP4")],
    source_manifest: Annotated[Path, typer.Option(help="Source manifest JSON")],
    retimed_render_manifest: Annotated[Path, typer.Option(help="Retimed render manifest JSON")],
    focus_pacing_qa: Annotated[Path, typer.Option(help="Focus and pacing QA JSON")],
    transcript_comparison: Annotated[
        Path, typer.Option(help="Rendered transcript comparison JSON")
    ],
    join_qa: Annotated[Path, typer.Option(help="Rendered join QA report JSON")],
    segment_qa: Annotated[Path, typer.Option(help="Segment QA report JSON")],
    join_plan: Annotated[Path, typer.Option(help="Retimed join plan JSON")],
    gate1: Annotated[Path, typer.Option(help="Current Gate 1 approval JSON")],
    backup_verification: Annotated[Path, typer.Option(help="Backup verification JSON")],
    caption_plan: Annotated[
        Path | None, typer.Option(help="Optional word-timed caption plan JSON")
    ] = None,
    visual_evidence: Annotated[
        list[Path] | None,
        typer.Option("--visual-evidence", help="Retained visual proof path; repeatable"),
    ] = None,
    profile_id: Annotated[
        str, typer.Option(help="Bound worker-free delivery profile identifier")
    ] = "profile_source_h264_qp0_pcm_f32le_2560x1440_60",
    profile: Annotated[Path | None, typer.Option(help="JSON profile override")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_002",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    profile_value: dict[str, Any] | None = None
    if profile is not None:
        value = _read_json_path(profile, "source candidate delivery profile")
        if not isinstance(value, dict):
            raise typer.BadParameter("source candidate delivery profile must be a JSON object")
        profile_value = value
    output = qa_source_candidate(
        _package_root(),
        layout,
        candidate.resolve(),
        source_manifest_path=source_manifest.resolve(),
        retimed_render_manifest_path=retimed_render_manifest.resolve(),
        focus_pacing_qa_path=focus_pacing_qa.resolve(),
        transcript_comparison_path=transcript_comparison.resolve(),
        join_qa_report_path=join_qa.resolve(),
        segment_qa_path=segment_qa.resolve(),
        join_plan_path=join_plan.resolve(),
        gate1_approval_path=gate1.resolve(),
        backup_verification_path=backup_verification.resolve(),
        visual_evidence_paths=[path.resolve() for path in (visual_evidence or [])],
        caption_plan_path=caption_plan.resolve() if caption_plan else None,
        profile_id=profile_id,
        profile=profile_value,
        revision_id=revision_id,
        adapter=_configured_ffmpeg_adapter(),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    typer.echo(json.dumps(payload, indent=2))
    if not payload["final_ready"]:
        raise typer.Exit(code=10)


@app.command("record-watchthrough")
def record_watchthrough_command(
    project_id: str,
    candidate: Annotated[Path, typer.Option(help="Final candidate reviewed by the operator")],
    actor: Annotated[str, typer.Option(help="Reviewer identity")],
    role: Annotated[str, typer.Option(help="Reviewer role")],
    protocol: Annotated[
        str, typer.Option(help="full_watch_through or approved_equivalent")
    ] = "full_watch_through",
    decision: Annotated[str, typer.Option(help="pass or fail")] = "pass",
    notes: Annotated[
        str, typer.Option(help="Review notes or equivalent protocol explanation")
    ] = "",
    evidence: Annotated[
        list[Path] | None, typer.Option("--evidence", help="Review evidence path; repeatable")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = record_watchthrough(
        _package_root(),
        layout,
        candidate.resolve(),
        actor=actor,
        role=role,
        protocol=protocol,
        decision=decision,
        notes=notes,
        evidence_paths=[path.resolve() for path in (evidence or [])],
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("approve-gate3")
def approve_gate3_command(
    project_id: str,
    final_qa: Annotated[Path, typer.Option(help="Final QA report JSON")],
    watchthrough: Annotated[Path, typer.Option(help="Watch-through record JSON")],
    asset_manifest: Annotated[Path, typer.Option(help="Project asset manifest JSON")],
    composition_bundle: Annotated[Path, typer.Option(help="Composition code bundle")],
    delivery_profile: Annotated[Path, typer.Option(help="Bound delivery profile file")],
    plan: Annotated[
        list[str] | None, typer.Option("--plan", help="Plan binding SCHEMA_NAME=PATH; repeatable")
    ],
    gate2: Annotated[
        list[Path] | None,
        typer.Option("--gate2", help="Locked Gate 2 segment approval; repeatable"),
    ],
    actor: Annotated[str, typer.Option(help="Approving reviewer identity")],
    role: Annotated[str, typer.Option(help="Approving reviewer role")],
    decision: Annotated[
        str, typer.Option(help="approved, changes_requested, or rejected")
    ] = "approved",
    notes: Annotated[str, typer.Option(help="Gate 3 review notes")] = "",
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = approve_gate3(
        _package_root(),
        layout,
        final_qa.resolve(),
        watchthrough.resolve(),
        asset_manifest.resolve(),
        composition_bundle.resolve(),
        delivery_profile.resolve(),
        _parse_plan_specs(plan or []),
        [path.resolve() for path in (gate2 or [])],
        actor=actor,
        role=role,
        decision=decision,
        notes=notes,
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("write-publishing-metadata")
def write_publishing_metadata_command(
    project_id: str,
    candidate: Annotated[Path, typer.Option(help="Gate 3 candidate JSON/MP4")],
    caption_plan: Annotated[Path, typer.Option(help="Caption plan with sidecar refs")],
    transcript: Annotated[Path, typer.Option(help="Final word-timed transcript")],
    boundaries: Annotated[
        Path | None, typer.Option(help="Optional structural boundary plan")
    ] = None,
    description: Annotated[str | None, typer.Option(help="Optional description draft")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_publishing_metadata(
        _package_root(),
        layout,
        candidate.resolve(),
        caption_plan.resolve(),
        transcript.resolve(),
        boundaries_path=boundaries.resolve() if boundaries else None,
        description_draft=description,
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("publish-delivery")
def publish_delivery_command(
    project_id: str,
    gate3: Annotated[Path, typer.Option(help="Approved Gate 3 record")],
    final_qa: Annotated[Path, typer.Option(help="Final QA report")],
    metadata: Annotated[Path, typer.Option(help="Publishing metadata")],
    delivery_profile: Annotated[Path, typer.Option(help="Bound delivery profile")],
    derivative: Annotated[
        list[str] | None, typer.Option("--derivative", help="NAME=WIDTHxHEIGHT; repeatable")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = publish_delivery(
        _package_root(),
        layout,
        gate3.resolve(),
        final_qa.resolve(),
        metadata.resolve(),
        delivery_profile.resolve(),
        derivatives=_parse_derivative_specs(derivative or []),
        revision_id=revision_id,
        adapter=_configured_ffmpeg_adapter(),
    )
    typer.echo(str(output))


@app.command("backup-verify")
def backup_verify(
    project_id: str,
    targets: Annotated[
        Path, typer.Option(help="JSON array of source_path, backup_path, and role objects")
    ],
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    value = _read_json_path(targets, "backup target specification")
    if isinstance(value, dict) and isinstance(value.get("targets"), list):
        value = value["targets"]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise typer.BadParameter("backup target specification must be an array of objects")
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = verify_backup_targets(_package_root(), layout, value, revision_id=revision_id)
    typer.echo(str(output))


@app.command("cleanup-plan")
def cleanup_plan(
    project_id: str,
    backup_verification: Annotated[
        Path | None, typer.Option(help="Passing backup verification JSON")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = backup_verification or layout.artifacts / "backup-verification.json"
    output = plan_cleanup(_package_root(), layout, selected.resolve(), revision_id=revision_id)
    typer.echo(str(output))


@app.command("approve-cleanup")
def approve_cleanup_command(
    project_id: str,
    actor: Annotated[str, typer.Option(help="Approving operator identity")],
    reason: Annotated[str, typer.Option(help="Cleanup approval reason")],
    cleanup_plan: Annotated[Path | None, typer.Option(help="Cleanup plan JSON")] = None,
    role: Annotated[str, typer.Option(help="Approving operator role")] = "operator",
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = cleanup_plan or layout.artifacts / "cleanup-plan.json"
    output = approve_cleanup(
        _package_root(),
        layout,
        selected.resolve(),
        actor=actor,
        role=role,
        reason=reason,
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("execute-cleanup")
def execute_cleanup_command(
    project_id: str,
    cleanup_plan: Annotated[Path | None, typer.Option(help="Cleanup plan JSON")] = None,
    approval: Annotated[Path | None, typer.Option(help="Explicit cleanup approval JSON")] = None,
    backup_verification: Annotated[
        Path | None, typer.Option(help="Passing backup verification JSON")
    ] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected_plan = cleanup_plan or layout.artifacts / "cleanup-plan.json"
    if approval is None or backup_verification is None:
        raise typer.BadParameter(
            "--approval and --backup-verification are required for cleanup execution"
        )
    output = execute_cleanup(
        _package_root(),
        layout,
        selected_plan.resolve(),
        approval.resolve(),
        backup_verification.resolve(),
        revision_id=revision_id,
    )
    typer.echo(str(output))


@app.command("status")
def project_status(
    project_id: str,
    watch: Annotated[
        bool, typer.Option("--watch", help="Retained for CLI compatibility; emit one snapshot")
    ] = False,
    events: Annotated[
        int, typer.Option("--events", help="Retained for CLI compatibility; event count hint")
    ] = 0,
    revision_id: Annotated[str | None, typer.Option(help="Optional revision to inspect")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    del watch, events
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = write_project_status(_package_root(), layout, revision_id=revision_id)
    typer.echo(output.read_text(encoding="utf-8"))


@app.command("retry")
def retry_stage(
    project_id: str,
    stage: Annotated[str, typer.Option(help="Stage name to retry")],
    reason: Annotated[str, typer.Option(help="Operator retry reason")],
    run_id: Annotated[str | None, typer.Option("--run", help="Optional stage run ID check")] = None,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    if run_id is not None:
        state_path = layout.stage_state / f"{stage}-{revision_id}.json"
        state = _read_json_path(state_path, "stage state")
        if not isinstance(state, dict) or state.get("stage_run_id") != run_id:
            raise typer.BadParameter("--run does not match the current stage state")
    output = request_stage_retry(
        _package_root(), layout, stage, revision_id=revision_id, reason=reason
    )
    typer.echo(str(output))


@app.command("cancel")
def cancel(
    project_id: str,
    stage: Annotated[str, typer.Option(help="Running stage name")],
    reason: Annotated[str, typer.Option(help="Operator cancellation reason")],
    retain_partial: Annotated[
        bool, typer.Option("--retain-partial", help="Keep declared staging files for diagnosis")
    ] = False,
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = cancel_stage(
        _package_root(),
        layout,
        stage,
        revision_id=revision_id,
        reason=reason,
        remove_partial=not retain_partial,
    )
    typer.echo(str(output))


@app.command("recover-stage")
def recover_stage(
    project_id: str,
    stage: Annotated[str, typer.Option(help="Orphaned running stage name")],
    reason: Annotated[str, typer.Option(help="Operator crash-recovery reason")],
    revision_id: Annotated[str, typer.Option(help="Project revision ID")] = "rev_001",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    output = recover_crashed_stage(
        _package_root(), layout, stage, revision_id=revision_id, reason=reason
    )
    typer.echo(str(output))


@app.command()
def qa(path: Path, report: Path | None = None) -> None:
    report_path = report or path.with_suffix(".qa.json")
    result = basic_media_qa(path, report_path, adapter=_configured_ffmpeg_adapter())
    typer.echo(json.dumps(result, indent=2))
    if result["status"] != "pass":
        raise typer.Exit(code=10)


@app.command("qa-project")
def qa_project(
    project_id: str,
    render_manifest: Annotated[Path | None, typer.Option(help="Render manifest to verify")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = render_manifest or layout.artifacts / "render-rough.json"
    output = qa_render(
        _package_root(),
        layout,
        selected,
        adapter=_configured_ffmpeg_adapter(),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    typer.echo(json.dumps(payload, indent=2))
    if not payload["final_ready"]:
        raise typer.Exit(code=10)


@app.command("approve-final")
def approve_final(
    project_id: str,
    actor: Annotated[str, typer.Option(help="Approving person or account")],
    render_manifest: Annotated[
        Path | None, typer.Option(help="Render manifest being approved")
    ] = None,
    reason: Annotated[str, typer.Option(help="Approval reason")] = "Approved after final review",
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    layout = ProjectLayout(_workspace_path(workspace) / "projects" / project_id)
    selected = render_manifest or layout.artifacts / "render-rough.json"
    output = approve_final_render(_package_root(), layout, selected, actor=actor, reason=reason)
    typer.echo(str(output))


@app.command()
def deliver(
    project_id: str,
    render_manifest: Annotated[Path | None, typer.Option(help="Approved render manifest")] = None,
    workspace: Annotated[Path | None, typer.Option(help="Repository workspace")] = None,
) -> None:
    del project_id, render_manifest, workspace
    raise ApprovalRequiredError(
        "the legacy deliver command is disabled; use publish-delivery after current Gate 3 approval"
    )


def run() -> None:
    try:
        app()
    except VideoeditError as exc:
        typer.echo(
            json.dumps(
                {
                    "command": "videoedit",
                    "status": "error",
                    "data": {},
                    "warnings": [],
                    "errors": [
                        {
                            "code": exc.code,
                            "message": exc.message,
                            "retryable": int(exc.exit_code) in (7, 8),
                        }
                    ],
                }
            ),
            err=True,
        )
        raise SystemExit(int(exc.exit_code)) from None


if __name__ == "__main__":
    run()
