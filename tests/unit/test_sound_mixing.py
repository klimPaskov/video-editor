from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.cue_planning import approve_cue_plan_bundle, write_cue_plan_bundle
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file
from videoedit.services.sound_mixing import mix_approved_sound_plan

ROOT = Path(__file__).resolve().parents[2]


class _FakeSoundAdapter:
    def __init__(self, clipped_samples: int = 0) -> None:
        self.clipped_samples = clipped_samples
        self.mix_calls: list[dict[str, object]] = []

    def mix_transition_sound(
        self,
        source: Path,
        sound: Path,
        output: Path,
        *,
        start_us: int,
        gain_db: float,
        fade_in_us: int,
        fade_out_us: int,
        duck_speech: bool,
    ) -> ProcessResult:
        self.mix_calls.append(
            {
                "source": source,
                "sound": sound,
                "start_us": start_us,
                "gain_db": gain_db,
                "fade_in_us": fade_in_us,
                "fade_out_us": fade_out_us,
                "duck_speech": duck_speech,
            }
        )
        output.write_bytes(b"mixed-output")
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def full_decode_check(self, source: Path) -> ProcessResult:
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def measure_clipping(self, source: Path) -> ProcessResult:
        return ProcessResult(
            ("ffmpeg",),
            0,
            "",
            f"Number of clipped samples: {self.clipped_samples}",
            1,
        )

    def measure_loudness(self, source: Path) -> str:
        return """
        Integrated loudness:
            I: -18.0 LUFS
            Threshold: -28.0 LUFS
        Loudness range:
            LRA: 5.0 LU
        True peak:
            Peak: -2.0 dBFS
        """


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path, Path, Path]:
    layout = initialize_project(tmp_path, "sound_mix_fixture")
    source = layout.work / "source.mp4"
    source.write_bytes(b"source-video")
    asset_root = layout.root / "assets"
    asset_root.mkdir()
    sound = asset_root / "whoosh.wav"
    sound.write_bytes(b"sound-asset")
    catalog_payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_sound_mix_fixture",
        "created_at": "2026-07-24T10:00:00Z",
        "updated_at": "2026-07-24T10:00:00Z",
        "root_path": str(asset_root.resolve()),
        "assets": [
            {
                "asset_id": "snd_fixture_whoosh",
                "asset_type": "sound_effect",
                "file": {
                    "path": sound.name,
                    "sha256": sha256_file(sound),
                    "size_bytes": sound.stat().st_size,
                    "mime_type": "audio/wav",
                    "width": None,
                    "height": None,
                    "duration_us": 300_000,
                },
                "description": "Fixture speech-safe whoosh",
                "tags": ["whoosh", "transition", "speech-safe"],
                "source": "Owned local fixture",
                "licence_status": "owned",
                "licence_reference": "license_sound_mix_fixture",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
                "audio_metadata": {
                    "integrated_loudness_lufs": -28.0,
                    "true_peak_dbtp": -6.0,
                    "transient_peak_offset_us": 120_000,
                    "intended_transition_types": ["swipe_left"],
                    "intensity": "subtle",
                    "speech_safe": True,
                    "minimum_reuse_interval_us": 45_000_000,
                    "brand_contexts": ["demo"],
                },
            }
        ],
    }
    catalog = layout.artifacts / "asset-catalog.json"
    write_validated_artifact(ROOT, "asset_catalog", catalog, catalog_payload)
    transition = json.loads(
        (ROOT / "examples" / "transition_plan.example.json").read_text(encoding="utf-8")
    )
    transition["project_id"] = layout.root.name
    transition_path = layout.artifacts / "transition-plan.json"
    write_validated_artifact(ROOT, "transition_plan", transition_path, transition)
    bundle = write_cue_plan_bundle(
        ROOT,
        layout,
        transition_path,
        catalog,
        timeline_duration_us=60_000_000,
    )
    approval = approve_cue_plan_bundle(ROOT, layout, bundle, actor="fixture-reviewer")
    return layout, source, catalog, bundle, approval


def test_mix_approved_sound_plan_records_speech_priority_and_qa(tmp_path: Path) -> None:
    layout, source, catalog, bundle, approval = _fixture(tmp_path)
    adapter = _FakeSoundAdapter()
    report = mix_approved_sound_plan(
        ROOT,
        layout,
        source,
        catalog,
        bundle,
        approval,
        adapter=adapter,  # type: ignore[arg-type]
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["qa_status"] == "pass"
    assert payload["clipping_status"] == "pass"
    assert payload["speech_priority"] is True
    assert payload["loudness"]["integrated_lufs"] == -18.0  # type: ignore[index]
    assert payload["cue_results"][0]["status"] == "pass"
    assert adapter.mix_calls[0]["duck_speech"] is True
    assert adapter.mix_calls[0]["gain_db"] == -14.0
    assert Path(str(payload["output"]["path"])).is_file()  # type: ignore[index]


def test_mix_approved_sound_plan_preserves_failed_clipping_evidence_and_rejects_stale_asset(
    tmp_path: Path,
) -> None:
    layout, source, catalog, bundle, approval = _fixture(tmp_path)
    report = mix_approved_sound_plan(
        ROOT,
        layout,
        source,
        catalog,
        bundle,
        approval,
        adapter=_FakeSoundAdapter(clipped_samples=4),  # type: ignore[arg-type]
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["qa_status"] == "fail"
    assert payload["failures"] == ["clipping_detected"]
    failed_output = Path(str(payload["output"]["path"])).resolve()  # type: ignore[index]
    assert ".failed-" in failed_output.name
    assert failed_output.is_file()

    sound = layout.root / "assets" / "whoosh.wav"
    sound.write_bytes(b"changed")
    with pytest.raises(PlanningValidationError, match="hash mismatch"):
        mix_approved_sound_plan(
            ROOT,
            layout,
            source,
            catalog,
            bundle,
            approval,
            adapter=_FakeSoundAdapter(),  # type: ignore[arg-type]
            report=layout.artifacts / "stale-report.json",
        )
