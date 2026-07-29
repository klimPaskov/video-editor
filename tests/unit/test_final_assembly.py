from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.errors import StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.final_assembly import assemble_approved_segments
from videoedit.services.project import initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


class FixtureAssemblyAdapter:
    def __init__(self) -> None:
        self.normalize_called = False

    def version(self) -> str:
        return "fixture"

    @staticmethod
    def _result(text: str = "") -> ProcessResult:
        return ProcessResult(
            arguments=("fixture-ffmpeg",),
            exit_code=0,
            stdout=text,
            stderr=text,
            elapsed_ms=1,
        )

    def concat_media(self, _inputs: list[Path], output: Path, **_kwargs: object) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pre-normalized")
        return self._result()

    def measure_loudness(self, _source: Path) -> ProcessResult:
        return self._result(
            "Integrated loudness:\n I: -16.0 LUFS\n Threshold: -26.0 LUFS\n"
            "LRA: 5.0 LU\nTrue peak:\n Peak: -1.5 dBFS\n"
        )

    def normalize_loudness(
        self, _source: Path, output: Path, _measurement: object, **_kwargs: object
    ) -> ProcessResult:
        self.normalize_called = True
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"normalized")
        return self._result()

    def measure_clipping(self, _source: Path) -> ProcessResult:
        return self._result("Number of clipped samples: 0")

    def full_decode_check(self, _source: Path) -> ProcessResult:
        return self._result()

    def probe(self, _source: Path) -> dict[str, object]:
        return {
            "format": {"duration": "1.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "duration": "1.0",
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "30/1",
                },
                {"codec_type": "audio", "duration": "1.0"},
            ],
        }


def test_final_assembly_accepts_only_locked_media_and_reuses_manifest(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "final_assembly_fixture")
    media_path = layout.revision_root("rev_001") / "segment.mp4"
    media_path.write_bytes(b"segment-media")
    source_path = layout.revision_root("rev_001") / "source.mp4"
    source_path.write_bytes(b"source-media")
    marker_path = layout.revision_root("rev_001") / "markers.json"
    marker_path.write_bytes(b"markers")

    media_manifest = json.loads(
        (ROOT / "examples" / "revision_media_manifest.example.json").read_text(encoding="utf-8")
    )
    media_manifest.update(
        {
            "artifact_id": "art_revision_media_fixture",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "parent_revision_id": "rev_000",
            "source_markers": {
                "artifact_id": "art_markers",
                "path": str(marker_path),
                "sha256": sha256_file(marker_path),
            },
            "source": {
                "artifact_id": "art_source",
                "path": str(source_path),
                "sha256": sha256_file(source_path),
            },
            "output": {
                "artifact_id": "art_segment_media",
                "path": str(media_path),
                "sha256": sha256_file(media_path),
            },
            "source_duration_us": 1_000_000,
            "output_duration_us": 1_000_000,
            "removed_ranges": [],
            "keep_ranges": [{"start_us": 0, "end_us": 1_000_000}],
            "source_to_output_mapping": [
                {
                    "source_start_us": 0,
                    "source_end_us": 1_000_000,
                    "output_start_us": 0,
                    "output_end_us": 1_000_000,
                }
            ],
        }
    )
    media_manifest_path = layout.revision_root("rev_001") / "segment-media.json"
    write_validated_artifact(ROOT, "revision_media_manifest", media_manifest_path, media_manifest)

    lock = json.loads((ROOT / "examples" / "segment_lock.example.json").read_text(encoding="utf-8"))
    lock.update(
        {
            "artifact_id": "art_segment_lock_fixture",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "segment_id": "segment_000001",
        }
    )
    lock_path = layout.review / "gate2" / "segment-000001-lock.json"
    write_validated_artifact(ROOT, "segment_lock", lock_path, lock)

    spec = [
        {
            "segment_id": "segment_000001",
            "lock_path": str(lock_path),
            "media_manifest_path": str(media_manifest_path),
            "source_range": {"start_us": 0, "end_us": 1_000_000},
        }
    ]
    adapter = FixtureAssemblyAdapter()
    manifest_path = assemble_approved_segments(
        ROOT,
        layout,
        spec,
        adapter=adapter,  # type: ignore[arg-type]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "final_assembly_manifest", manifest)
    assert manifest["status"] == "complete"
    assert manifest["loudness"]["clipped_samples"] == 0
    assert manifest["output"]["size_bytes"] > 0
    assert assemble_approved_segments(ROOT, layout, spec, adapter=adapter) == manifest_path  # type: ignore[arg-type]

    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["status"] = "warning"
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(StateConflictError, match="stale contents"):
        assemble_approved_segments(ROOT, layout, spec, adapter=adapter)  # type: ignore[arg-type]


def test_final_assembly_can_preserve_one_preassembled_candidate_without_normalization(
    tmp_path: Path,
) -> None:
    layout = initialize_project(tmp_path, "final_assembly_preserve_fixture")
    media_path = layout.revision_root("rev_001") / "candidate.mp4"
    media_path.write_bytes(b"preassembled-candidate")
    source_path = layout.revision_root("rev_001") / "source.mp4"
    source_path.write_bytes(b"source-media")
    marker_path = layout.revision_root("rev_001") / "markers.json"
    marker_path.write_bytes(b"markers")

    media_manifest = json.loads(
        (ROOT / "examples" / "revision_media_manifest.example.json").read_text(encoding="utf-8")
    )
    media_manifest.update(
        {
            "artifact_id": "art_revision_media_preserved_fixture",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "parent_revision_id": "rev_000",
            "source_markers": {
                "artifact_id": "art_markers",
                "path": str(marker_path),
                "sha256": sha256_file(marker_path),
            },
            "source": {
                "artifact_id": "art_source",
                "path": str(source_path),
                "sha256": sha256_file(source_path),
            },
            "output": {
                "artifact_id": "art_preassembled_candidate",
                "path": str(media_path),
                "sha256": sha256_file(media_path),
            },
            "source_duration_us": 1_000_000,
            "output_duration_us": 1_000_000,
            "removed_ranges": [],
            "keep_ranges": [{"start_us": 0, "end_us": 1_000_000}],
            "source_to_output_mapping": [
                {
                    "source_start_us": 0,
                    "source_end_us": 1_000_000,
                    "output_start_us": 0,
                    "output_end_us": 1_000_000,
                }
            ],
        }
    )
    media_manifest_path = layout.revision_root("rev_001") / "candidate-media.json"
    write_validated_artifact(ROOT, "revision_media_manifest", media_manifest_path, media_manifest)

    lock = json.loads((ROOT / "examples" / "segment_lock.example.json").read_text(encoding="utf-8"))
    lock.update(
        {
            "artifact_id": "art_segment_lock_preserved_fixture",
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "segment_id": "segment_000001",
        }
    )
    lock_path = layout.review / "gate2" / "segment-000001-preserved-lock.json"
    write_validated_artifact(ROOT, "segment_lock", lock_path, lock)
    adapter = FixtureAssemblyAdapter()
    output = layout.output / "preserved-candidate.mp4"
    manifest_path = assemble_approved_segments(
        ROOT,
        layout,
        [
            {
                "segment_id": "segment_000001",
                "lock_path": str(lock_path),
                "media_manifest_path": str(media_manifest_path),
                "source_range": {"start_us": 0, "end_us": 1_000_000},
            }
        ],
        output=output,
        normalization="none",
        adapter=adapter,  # type: ignore[arg-type]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "final_assembly_manifest", manifest)
    assert output.read_bytes() == media_path.read_bytes()
    assert adapter.normalize_called is False
    assert manifest["warnings"] == ["loudness_normalization_disabled_by_delivery_profile"]
