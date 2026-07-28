from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.approval import approve_final_render
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    write_validated_artifact,
)
from videoedit.services.delivery import build_delivery
from videoedit.services.editing import (
    compile_edl,
    create_gate1_approval,
    plan_silence_edits,
)
from videoedit.services.media import ingest_and_probe
from videoedit.services.project import initialize_project
from videoedit.services.qa import qa_render
from videoedit.services.rendering import render_base_timeline
from videoedit.services.silence import detect_project_silence
from videoedit.services.transcription import normalize_whisper_result


@pytest.mark.integration
def test_full_local_workflow(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "demo_flow")
    source = tmp_path / "recording.mp4"
    adapter = FFmpegAdapter()
    adapter.generate_edit_demo_source(source)
    manifest = ingest_and_probe(package_root, layout, source, adapter=adapter)
    assert manifest["proxy_artifact_ids"] == {
        "edit": "art_proxy_edit",
        "speech": "art_proxy_speech",
    }
    for proxy_kind in ("edit", "speech"):
        proxy_path = layout.artifacts / f"media-proxy-{proxy_kind}.json"
        proxy = json.loads(proxy_path.read_text(encoding="utf-8"))
        assert Path(proxy["output"]["path"]).is_file()
        assert proxy["output"]["sha256"]

    fake_whisper = {
        "language": "en",
        "text": "before after",
        "segments": [
            {
                "start": 0.5,
                "end": 1.2,
                "text": "before",
                "words": [{"word": "before", "start": 0.5, "end": 1.2, "probability": 1.0}],
                "avg_logprob": 0.0,
                "no_speech_prob": 0.0,
            },
            {
                "start": 4.5,
                "end": 5.2,
                "text": "after",
                "words": [{"word": "after", "start": 4.5, "end": 5.2, "probability": 1.0}],
                "avg_logprob": 0.0,
                "no_speech_prob": 0.0,
            },
        ],
    }
    manifest_path = layout.artifacts / "source-manifest.json"
    transcript = normalize_whisper_result(
        result=fake_whisper,
        project_id=layout.root.name,
        revision_id="rev_001",
        duration_us=int(manifest["media_duration_us"]),
        model_name="fixture",
        source_input=artifact_input("art_source", manifest_path),
        config_hash=config_sha256(layout),
    )
    transcript_path = layout.artifacts / "transcript.json"
    write_validated_artifact(package_root, "transcript", transcript_path, transcript)

    silence_path = detect_project_silence(
        package_root,
        layout,
        threshold_db=-45,
        minimum_duration_us=500_000,
    )
    silence = json.loads(silence_path.read_text(encoding="utf-8"))
    assert any(item["classification"] == "inter_word" for item in silence["intervals"])

    proposals_path, decisions_path = plan_silence_edits(package_root, layout)
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    assert proposals["proposals"]
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions["reviewer"] = {"actor": "test@example.com", "role": "editor"}
    for decision in decisions["decisions"]:
        decision["decision"] = "approve"
        decision["reason"] = "Approved in integration test"
    decisions_path.write_text(json.dumps(decisions, indent=2), encoding="utf-8")

    gate1_path = create_gate1_approval(
        package_root,
        layout,
        decisions_path,
        layout.artifacts / "effect-plan.json",
        actor="test@example.com",
    )
    edl_path = compile_edl(
        package_root,
        layout,
        decisions_path,
        gate1_approval_path=gate1_path,
    )
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    render_path = render_base_timeline(package_root, layout, adapter=adapter)
    render_manifest = json.loads(render_path.read_text(encoding="utf-8"))
    assert render_manifest["validation"] == {
        "full_decode": "pass",
        "frame_count": "pass",
        "duration": "pass",
        "audio_duration": "pass",
        "clipping": "pass",
        "loudness": "pass",
    }
    assert render_manifest["loudness"]["output"]["integrated_lufs"] == pytest.approx(-16.0, abs=1.0)
    assert render_manifest["av_sync"]["drift_us"] <= 100_000
    assert (
        abs(render_manifest["actual_duration_us"] - render_manifest["expected_duration_us"])
        <= 100_000
    )
    assert (
        abs(render_manifest["video_duration_us"] - render_manifest["expected_duration_us"])
        <= 100_000
    )
    assert (
        abs(render_manifest["audio_duration_us"] - render_manifest["expected_duration_us"])
        <= 100_000
    )
    assert render_manifest["source_to_output_mapping"] == edl["source_to_output_mapping"]
    assert (layout.artifacts / "transcript-output.json").is_file()
    qa_path = qa_render(package_root, layout, render_path, adapter=adapter)
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    assert qa["final_ready"] is True
    approval_path = approve_final_render(
        package_root,
        layout,
        render_path,
        actor="test@example.com",
    )
    delivery_path = build_delivery(package_root, layout, render_path)

    assert edl_path.is_file()
    assert approval_path.is_file()
    assert delivery_path.is_file()
    delivered = json.loads(delivery_path.read_text(encoding="utf-8"))
    master = next(item for item in delivered["outputs"] if item["role"] == "master")
    assert Path(master["file"]["path"]).is_file()
