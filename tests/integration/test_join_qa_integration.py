from __future__ import annotations

import json
from pathlib import Path

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.transcription import FixtureTranscriptionAdapter, TranscriptionResult
from videoedit.services.artifacts import config_sha256, write_validated_artifact
from videoedit.services.join_qa import qa_rendered_joins
from videoedit.services.project import initialize_project, sha256_file


def _load_example(root: Path, name: str) -> dict[str, object]:
    return json.loads((root / "examples" / name).read_text(encoding="utf-8"))


class _HashingFixtureTranscriber:
    adapter_id = "fixture-transcription"
    adapter_version = "1.0.0"
    device = "cpu"

    def __init__(self, fixture_result: dict[str, object], model_sha256: str) -> None:
        self.fixture_result = fixture_result
        self.model_sha256 = model_sha256
        self.calls = 0

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult:
        self.calls += 1
        result = FixtureTranscriptionAdapter(self.fixture_result).transcribe(
            audio_path,
            model_name,
        )
        return TranscriptionResult(
            raw_result=result.raw_result,
            model_identifier=result.model_identifier,
            device=result.device,
            adapter_id=result.adapter_id,
            adapter_version=result.adapter_version,
            model_sha256=self.model_sha256,
        )


class _FailingHashingFixtureTranscriber(_HashingFixtureTranscriber):
    def __init__(
        self,
        fixture_result: dict[str, object],
        model_sha256: str,
        fail_on_call: int,
    ) -> None:
        super().__init__(fixture_result, model_sha256)
        self.fail_on_call = fail_on_call

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult:
        if self.calls + 1 == self.fail_on_call:
            self.calls += 1
            raise RuntimeError("simulated interruption after a completed join")
        return super().transcribe(audio_path, model_name)


def test_rendered_join_preview_is_retranscribed_and_persisted(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "join_qa_demo")
    adapter = FFmpegAdapter()
    rendered_media = layout.work / "rendered.mp4"
    adapter.generate_edit_demo_source(rendered_media)

    render_manifest = _load_example(package_root, "render_manifest.example.json")
    render_manifest.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "expected_duration_us": 6_000_000,
            "actual_duration_us": 6_000_000,
            "output": {
                "path": str(rendered_media),
                "sha256": sha256_file(rendered_media),
                "size_bytes": rendered_media.stat().st_size,
            },
        }
    )
    render_manifest_path = layout.artifacts / "render-manifest.json"
    write_validated_artifact(
        package_root,
        "render_manifest",
        render_manifest_path,
        render_manifest,
    )

    transcript = _load_example(package_root, "transcript.example.json")
    transcript.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "source_duration_us": 6_000_000,
            "text": "this is the result",
            "segments": [
                {
                    "segment_id": "seg_001",
                    "text": "this is the result",
                    "start_us": 1_500_000,
                    "end_us": 3_700_000,
                    "word_ids": ["wrd_001", "wrd_002", "wrd_003", "wrd_004"],
                    "average_log_probability": -0.1,
                    "no_speech_probability": 0.01,
                }
            ],
            "words": [
                {
                    "word_id": "wrd_001",
                    "segment_id": "seg_001",
                    "text": "this",
                    "start_us": 1_500_000,
                    "end_us": 1_900_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_002",
                    "segment_id": "seg_001",
                    "text": "is",
                    "start_us": 2_000_000,
                    "end_us": 2_300_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_003",
                    "segment_id": "seg_001",
                    "text": "the",
                    "start_us": 2_500_000,
                    "end_us": 2_900_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_004",
                    "segment_id": "seg_001",
                    "text": "result",
                    "start_us": 3_100_000,
                    "end_us": 3_700_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
            ],
        }
    )
    transcript_path = layout.artifacts / "transcript-output.json"
    write_validated_artifact(package_root, "transcript", transcript_path, transcript)

    join_plan = _load_example(package_root, "join_plan.example.json")
    join_plan.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "output_duration_us": 6_000_000,
        }
    )
    join = join_plan["joins"][0]  # type: ignore[index]
    join["output_join_us"] = 3_000_000  # type: ignore[index]
    join["preview_range"] = {"start_us": 1_000_000, "end_us": 5_000_000}  # type: ignore[index]
    # The post-render transcript is output-clock.  Keep a deliberately
    # different source-clock range here to prove the default does not use it.
    join["source_preview_ranges"] = [  # type: ignore[index]
        {"start_us": 5_000_000, "end_us": 5_500_000},
    ]
    join_plan_path = layout.artifacts / "join-plan.json"
    write_validated_artifact(package_root, "join_plan", join_plan_path, join_plan)

    fixture = _HashingFixtureTranscriber(
        fixture_result={
            "language": "en",
            "text": "this is the result",
            "segments": [
                {
                    "start": 0.5,
                    "end": 2.7,
                    "text": "this is the result",
                    "words": [
                        {"word": "this", "start": 0.5, "end": 0.9, "probability": 1.0},
                        {"word": "is", "start": 1.0, "end": 1.3, "probability": 1.0},
                        {"word": "the", "start": 1.5, "end": 1.9, "probability": 1.0},
                        {"word": "result", "start": 2.1, "end": 2.7, "probability": 1.0},
                    ],
                    "avg_logprob": 0.0,
                    "no_speech_prob": 0.0,
                }
            ],
        },
        model_sha256="a" * 64,
    )
    media_evidence = {
        "join_001": {
            "audio": {
                "click_or_pop": False,
                "room_tone_jump": False,
                "speech_rhythm": "natural",
            },
            "visual": {
                "duplicate_frame": False,
                "face_or_body_jump": "none",
                "screen_state_jump": "none",
            },
        }
    }

    report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=fixture,
        model_name="fixture",
        adapter=adapter,
        media_evidence=media_evidence,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["overall_status"] == "pass"
    assert report["transcription_provenance"]["model_sha256"] == "a" * 64
    assert report["summary"]["total_joins"] == 1
    report_join = report["joins"][0]
    assert report_join["transcript_check"]["missing_words"] == []
    assert report_join["transcript_check"]["duplicate_words"] == []
    assert report_join["preview"]["full_decode_status"] == "pass"
    assert report_join["diagnostic_window"] == {"start_us": 2_500_000, "end_us": 3_500_000}
    assert report_join["audio_check"]["boundary_clipping_known"] is True
    assert report_join["visual_check"]["boundary_freeze_known"] is True
    assert Path(report_join["preview"]["file"]["path"]).is_file()
    preview_path = Path(report_join["preview"]["file"]["path"])
    original_preview_sha256 = report_join["preview"]["file"]["sha256"]

    changed_media_evidence = json.loads(json.dumps(media_evidence))
    changed_media_evidence["join_001"]["audio"]["click_or_pop"] = True
    evidence_changed_report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=fixture,
        model_name="fixture",
        adapter=adapter,
        media_evidence=changed_media_evidence,
    )
    evidence_changed_report = json.loads(evidence_changed_report_path.read_text(encoding="utf-8"))
    assert evidence_changed_report["overall_status"] == "fail"
    assert evidence_changed_report["joins"][0]["audio_check"]["click_or_pop"] is True

    preview_path.write_bytes(b"tampered join preview")
    repaired_report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=fixture,
        model_name="fixture",
        adapter=adapter,
        media_evidence=media_evidence,
    )
    repaired_report = json.loads(repaired_report_path.read_text(encoding="utf-8"))
    repaired_preview = repaired_report["joins"][0]["preview"]["file"]
    assert repaired_preview["sha256"] == original_preview_sha256
    assert sha256_file(preview_path) == original_preview_sha256
    tampered_report = json.loads(repaired_report_path.read_text(encoding="utf-8"))
    tampered_report["summary"]["total_joins"] = 0
    repaired_report_path.write_text(json.dumps(tampered_report), encoding="utf-8")
    restored_report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=fixture,
        model_name="fixture",
        adapter=adapter,
        media_evidence=media_evidence,
    )
    restored_report = json.loads(restored_report_path.read_text(encoding="utf-8"))
    assert restored_report["summary"]["total_joins"] == 1
    (layout.artifacts / "join-qa-report-pass.json").write_text(
        restored_report_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    broken_result = json.loads(json.dumps(fixture.fixture_result))
    broken_result["text"] = "this is is the result"
    broken_result["segments"][0]["text"] = "this is is the result"
    broken_result["segments"][0]["words"].insert(
        2,
        {"word": "is", "start": 1.4, "end": 1.5, "probability": 1.0},
    )
    broken = _HashingFixtureTranscriber(
        fixture_result=broken_result,
        model_sha256="b" * 64,
    )
    broken_report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=broken,
        model_name="fixture",
        adapter=adapter,
        media_evidence=media_evidence,
    )
    broken_report = json.loads(broken_report_path.read_text(encoding="utf-8"))
    assert broken_report["overall_status"] == "fail"
    assert broken_report["transcription_provenance"]["model_sha256"] == "b" * 64
    assert broken_report["joins"][0]["transcript_check"]["duplicate_words"] == ["is"]
    assert "adjusted_handles" in broken_report["joins"][0]["repair_action"]
    (layout.artifacts / "join-qa-report-broken.json").write_text(
        broken_report_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def test_rendered_join_qa_resumes_from_atomic_progress_checkpoint(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "join_qa_resume_demo")
    adapter = FFmpegAdapter()
    rendered_media = layout.work / "rendered.mp4"
    adapter.generate_edit_demo_source(rendered_media)

    render_manifest = _load_example(package_root, "render_manifest.example.json")
    render_manifest.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "expected_duration_us": 6_000_000,
            "actual_duration_us": 6_000_000,
            "output": {
                "path": str(rendered_media),
                "sha256": sha256_file(rendered_media),
                "size_bytes": rendered_media.stat().st_size,
            },
        }
    )
    render_manifest_path = layout.artifacts / "render-manifest.json"
    write_validated_artifact(package_root, "render_manifest", render_manifest_path, render_manifest)

    transcript = _load_example(package_root, "transcript.example.json")
    transcript.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "source_duration_us": 6_000_000,
            "text": "this is the result",
            "segments": [
                {
                    "segment_id": "seg_001",
                    "text": "this is the result",
                    "start_us": 1_500_000,
                    "end_us": 3_700_000,
                    "word_ids": ["wrd_001", "wrd_002", "wrd_003", "wrd_004"],
                    "average_log_probability": -0.1,
                    "no_speech_probability": 0.01,
                }
            ],
            "words": [
                {
                    "word_id": "wrd_001",
                    "segment_id": "seg_001",
                    "text": "this",
                    "start_us": 1_500_000,
                    "end_us": 1_900_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_002",
                    "segment_id": "seg_001",
                    "text": "is",
                    "start_us": 2_000_000,
                    "end_us": 2_300_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_003",
                    "segment_id": "seg_001",
                    "text": "the",
                    "start_us": 2_500_000,
                    "end_us": 2_900_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
                {
                    "word_id": "wrd_004",
                    "segment_id": "seg_001",
                    "text": "result",
                    "start_us": 3_100_000,
                    "end_us": 3_700_000,
                    "probability": 0.99,
                    "timing_status": "original",
                },
            ],
        }
    )
    transcript_path = layout.artifacts / "transcript-output.json"
    write_validated_artifact(package_root, "transcript", transcript_path, transcript)

    join_plan = _load_example(package_root, "join_plan.example.json")
    join_plan.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "output_duration_us": 6_000_000,
        }
    )
    first_join = dict(join_plan["joins"][0])  # type: ignore[index]
    first_join.update(
        {
            "join_id": "join_001",
            "output_join_us": 3_000_000,
            "preview_range": {"start_us": 1_000_000, "end_us": 5_000_000},
        }
    )
    second_join = dict(first_join)
    second_join.update({"join_id": "join_002", "output_join_us": 3_500_000})
    join_plan["joins"] = [first_join, second_join]  # type: ignore[index]
    join_plan_path = layout.artifacts / "join-plan.json"
    write_validated_artifact(package_root, "join_plan", join_plan_path, join_plan)

    fixture_result = {
        "language": "en",
        "text": "this is the result",
        "segments": [
            {
                "start": 0.5,
                "end": 2.7,
                "text": "this is the result",
                "words": [
                    {"word": "this", "start": 0.5, "end": 0.9, "probability": 1.0},
                    {"word": "is", "start": 1.0, "end": 1.3, "probability": 1.0},
                    {"word": "the", "start": 1.5, "end": 1.9, "probability": 1.0},
                    {"word": "result", "start": 2.1, "end": 2.7, "probability": 1.0},
                ],
                "avg_logprob": 0.0,
                "no_speech_prob": 0.0,
            }
        ],
    }
    media_evidence = {
        "join_001": {
            "audio": {
                "click_or_pop": False,
                "room_tone_jump": False,
                "speech_rhythm": "natural",
            },
            "visual": {
                "duplicate_frame": False,
                "face_or_body_jump": "none",
                "screen_state_jump": "none",
            },
        },
        "join_002": {
            "audio": {
                "click_or_pop": False,
                "room_tone_jump": False,
                "speech_rhythm": "natural",
            },
            "visual": {
                "duplicate_frame": False,
                "face_or_body_jump": "none",
                "screen_state_jump": "none",
            },
        },
    }

    interrupted = _FailingHashingFixtureTranscriber(fixture_result, "c" * 64, fail_on_call=2)
    try:
        qa_rendered_joins(
            package_root,
            layout,
            render_manifest_path,
            join_plan_path,
            transcript_path,
            transcriber=interrupted,
            model_name="fixture",
            adapter=adapter,
            media_evidence=media_evidence,
        )
    except RuntimeError as exc:
        assert "completed join" in str(exc)
    else:
        raise AssertionError("the interruption fixture must fail the first run")
    assert interrupted.calls == 2

    resumed = _HashingFixtureTranscriber(fixture_result, "c" * 64)
    report_path = qa_rendered_joins(
        package_root,
        layout,
        render_manifest_path,
        join_plan_path,
        transcript_path,
        transcriber=resumed,
        model_name="fixture",
        adapter=adapter,
        media_evidence=media_evidence,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["total_joins"] == 2
    assert resumed.calls == 1
