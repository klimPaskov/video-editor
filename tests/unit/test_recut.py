from __future__ import annotations

import json
from pathlib import Path

from videoedit.adapters.process import ProcessResult
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.recut import derive_recut_ranges, recut_revision

ROOT = Path(__file__).resolve().parents[2]


class _FakeRecutAdapter:
    def __init__(self) -> None:
        self.render_calls: list[list[tuple[int, int]]] = []
        self.render_options: list[dict[str, object]] = []
        self.strict_values: list[bool] = []
        self.output_durations: dict[Path, int] = {}

    def probe(self, path: Path) -> dict[str, object]:
        duration_us = self.output_durations.get(path.resolve(), 6_000_000)
        duration = f"{duration_us / 1_000_000:.6f}"
        return {
            "format": {"duration": duration},
            "streams": [
                {"codec_type": "video", "duration": duration},
                {"codec_type": "audio", "duration": duration},
            ],
        }

    def render_keep_ranges(
        self,
        _source: Path,
        keep_ranges: list[tuple[int, int]],
        output: Path,
        *,
        video_codec: str | None,
        audio_codec: str,
        crf: int,
        preset: str,
        qp: int | None,
    ) -> ProcessResult:
        self.render_options.append(
            {
                "video_codec": video_codec,
                "audio_codec": audio_codec,
                "crf": crf,
                "preset": preset,
                "qp": qp,
            }
        )
        self.render_calls.append(keep_ranges)
        self.output_durations[output.resolve()] = sum(
            end_us - start_us for start_us, end_us in keep_ranges
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"recut-fixture")
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def full_decode_check(self, _path: Path, *, strict: bool = False) -> ProcessResult:
        self.strict_values.append(strict)
        return ProcessResult(("ffmpeg",), 0, "", "", 1)

    def version(self, _executable: str | None = None) -> str:
        return "fixture"


def test_derive_recut_ranges_applies_removable_fix_and_keep_override() -> None:
    removed, kept, warnings = derive_recut_ranges(
        [
            {
                "kind": "FIX",
                "instruction": "Remove the first duplicate phrase.",
                "range_us": {"start_us": 4_800_000, "end_us": 5_000_000},
            },
            {
                "kind": "KEEP",
                "instruction": "Keep the complete second take.",
                "range_us": {"start_us": 4_900_000, "end_us": 5_100_000},
            },
        ],
        6_000_000,
    )
    assert removed == [(4_800_000, 4_900_000)]
    assert kept == [(0, 4_800_000), (4_900_000, 6_000_000)]
    assert warnings == []


def test_recut_revision_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "recut_fixture")
    source = layout.work / "source.mp4"
    source.write_bytes(b"immutable-source")
    source_sha = sha256_file(source)

    marker_payload = json.loads(
        (ROOT / "examples" / "review_markers.example.json").read_text(encoding="utf-8")
    )
    marker_payload["project_id"] = layout.root.name
    marker_payload["source_markdown"]["path"] = str(layout.review / "fixes.md")
    marker_payload["source_package"]["path"] = str(layout.review / "review-package.json")
    marker_path = layout.artifacts / "review-markers.json"
    write_validated_artifact(ROOT, "review_markers", marker_path, marker_payload)

    revision_root = layout.revision_root("rev_002")
    revision_root.mkdir(parents=True)
    write_validated_artifact(
        ROOT,
        "project_revision",
        revision_root / "revision.json",
        {
            "schema_name": "project_revision",
            "schema_version": "1.0.0",
            "project_id": layout.root.name,
            "revision_id": "rev_002",
            "parent_revision_id": "rev_001",
            "created_at": "2026-07-24T12:00:00Z",
            "active": True,
            "directories": {
                "artifacts": str(layout.artifacts),
                "review": str(layout.review),
                "work": str(layout.work),
                "output": str(layout.output),
            },
        },
    )
    request_path = revision_root / "revision-request.json"
    request_payload = {
        "schema_name": "revision_request",
        "schema_version": "1.0.0",
        "artifact_id": "art_revision_request_rev_002",
        "project_id": layout.root.name,
        "revision_id": "rev_002",
        "parent_revision_id": "rev_001",
        "created_at": "2026-07-24T12:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "revision-request",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "source_markers": {
            "artifact_id": marker_payload["artifact_id"],
            "path": str(marker_path),
            "sha256": sha256_file(marker_path),
        },
        "markers": [
            {
                "marker_id": "marker_duplicate",
                "kind": "FIX",
                "instruction": "Remove the first duplicate phrase.",
                "range_us": {"start_us": 4_800_000, "end_us": 5_000_000},
            }
        ],
        "invalidated_stages": ["edit", "render"],
        "preserved_inputs": [],
        "status": "created",
    }
    write_validated_artifact(ROOT, "revision_request", request_path, request_payload)

    adapter = _FakeRecutAdapter()
    manifest_path = recut_revision(
        ROOT,
        layout,
        request_path,
        source,
        adapter=adapter,  # type: ignore[arg-type]
        video_codec="libx264",
        audio_codec="pcm_f32le",
        qp=0,
        preset="ultrafast",
        strict_decode=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "revision_media_manifest", manifest)
    assert manifest["removed_ranges"] == [{"start_us": 4_800_000, "end_us": 5_000_000}]
    assert manifest["output_duration_us"] == 5_800_000
    assert adapter.render_calls == [[(0, 4_800_000), (5_000_000, 6_000_000)]]
    assert adapter.render_options == [
        {
            "video_codec": "libx264",
            "audio_codec": "pcm_f32le",
            "crf": 18,
            "preset": "ultrafast",
            "qp": 0,
        }
    ]
    assert adapter.strict_values == [True]
    assert sha256_file(source) == source_sha

    second = recut_revision(ROOT, layout, request_path, source, adapter=adapter)  # type: ignore[arg-type]
    assert second == manifest_path
    assert len(adapter.render_calls) == 1
