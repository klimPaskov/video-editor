from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact
from videoedit.services.captions import _word_values, build_caption_plan
from videoedit.services.project import initialize_project, sha256_file


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_caption_plan_groups_word_timing_and_writes_sidecars(tmp_path: Path) -> None:
    root = package_root()
    fixture = root / "tests" / "fixtures"
    source_transcript = json.loads(
        (fixture / "segment-transcript.json").read_text(encoding="utf-8")
    )
    source_manifest = json.loads(
        (fixture / "caption-render-manifest.json").read_text(encoding="utf-8")
    )
    layout = initialize_project(tmp_path, "caption_test")
    source_transcript["project_id"] = "caption_test"
    transcript_path = layout.artifacts / "transcript-output.json"
    transcript_path.write_text(json.dumps(source_transcript), encoding="utf-8")
    copied_video = layout.output / "base.mp4"
    copied_video.write_bytes(b"synthetic caption fixture")
    source_manifest["project_id"] = "caption_test"
    source_manifest["output"]["path"] = str(copied_video)
    source_manifest["output"]["sha256"] = sha256_file(copied_video)
    source_manifest["output"]["size_bytes"] = copied_video.stat().st_size
    manifest_path = layout.artifacts / "render-rough.json"
    manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")

    result = build_caption_plan(
        root,
        layout,
        transcript_path,
        manifest_path,
        brand_path=root / "config" / "brand.example.yaml",
    )
    payload = json.loads(result.plan_path.read_text(encoding="utf-8"))
    validate_artifact(root, "caption_plan", payload)

    assert result.event_count == 2
    assert payload["events"][0]["word_ids"] == ["wrd_000001"]
    assert payload["events"][1]["lines"] == ["Go go buy"]
    assert payload["safe_area"] == {"left": 0.08, "right": 0.08, "top": 0.1, "bottom": 0.1}
    assert "caption_font_unconfigured:caption_primary" in payload["warnings"]
    assert result.ass_path.is_file()
    assert result.webvtt_path.is_file()
    assert result.text_path.is_file()
    assert payload["outputs"]["webvtt"]["sha256"] == sha256_file(result.webvtt_path)


def test_caption_word_values_clamp_only_bounded_whisper_overlap() -> None:
    warnings: list[str] = []
    words = _word_values(
        {
            "words": [
                {"word_id": "wrd_000001", "text": "one", "start_us": 0, "end_us": 1_000},
                {
                    "word_id": "wrd_000002",
                    "text": "two",
                    "start_us": 999,
                    "end_us": 2_000,
                },
            ]
        },
        2_000,
        warnings=warnings,
    )

    assert words[1]["start_us"] == 1_000
    assert warnings == ["caption_word_start_clamped:wrd_000002:1us"]
