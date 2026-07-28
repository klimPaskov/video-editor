from __future__ import annotations

from pathlib import Path

import pytest

from videoedit.adapters.process import ProcessResult
from videoedit.services.project import sha256_file
from videoedit.services.remotion import RemotionService


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_stage_asset_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "licensed-local.png"
    source.write_bytes(b"local licensed fixture")
    service = RemotionService(tmp_path / "remotion", package_root=package_root())

    staged_ref = service.stage_asset("asset_test", source, expected_sha256=sha256_file(source))
    staged_path = tmp_path / "remotion" / "public" / staged_ref

    assert staged_path.is_file()
    assert sha256_file(staged_path) == sha256_file(source)
    assert service.stage_asset("asset_test", source) == staged_ref
    with pytest.raises(ValueError, match="does not match"):
        service.stage_asset("asset_test", source, expected_sha256="0" * 64)


def test_code_bundle_hash_is_deterministic() -> None:
    service = RemotionService(package_root() / "remotion", package_root=package_root())
    first = service.code_bundle_sha256()
    second = service.code_bundle_sha256()
    assert len(first) == 64
    assert first == second


def test_code_bundle_manifest_has_the_same_hash_as_the_bound_sources(tmp_path: Path) -> None:
    service = RemotionService(package_root() / "remotion", package_root=package_root())
    bundle = service.write_code_bundle(tmp_path / "composition-bundle.json")

    assert bundle.is_file()
    assert sha256_file(bundle) == service.code_bundle_sha256()
    assert service.write_code_bundle(bundle) == bundle

    bundle.write_bytes(b"stale composition bundle")
    with pytest.raises(ValueError, match="stale"):
        service.write_code_bundle(bundle)


def test_remotion_process_uses_explicit_npm_path(tmp_path: Path) -> None:
    calls: list[object] = []

    class Runner:
        def run(self, request):
            calls.append(request)
            return ProcessResult(
                arguments=(request.executable, *request.arguments),
                exit_code=0,
                stdout="AgentEdit",
                stderr="",
                elapsed_ms=1,
            )

    configured_npm = tmp_path / "node-v22" / "npm.cmd"
    service = RemotionService(
        tmp_path / "remotion",
        npm_path=str(configured_npm),
        runner=Runner(),
        package_root=package_root(),
    )

    assert service.list_compositions() == "AgentEdit"
    assert len(calls) == 1
    assert calls[0].executable == str(configured_npm)


def test_frame_render_uses_serial_concurrency_for_deterministic_video_decode(
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    remotion_directory = tmp_path / "remotion"
    (remotion_directory / "node_modules").mkdir(parents=True)

    class Runner:
        def run(self, request):
            calls.append(request)
            output = Path(request.arguments[6])
            output.write_bytes(b"rendered fixture")
            return ProcessResult(
                arguments=(request.executable, *request.arguments),
                exit_code=0,
                stdout="",
                stderr="",
                elapsed_ms=1,
            )

    service = RemotionService(
        remotion_directory,
        runner=Runner(),
        package_root=package_root(),
    )
    props = tmp_path / "props.json"
    props.write_text("{}", encoding="utf-8")
    output = tmp_path / "render.mp4"

    service.render_segment(props, output, start_frame=0, end_frame=0)

    assert output.read_bytes() == b"rendered fixture"
    assert len(calls) == 1
    assert "--concurrency=1" in calls[0].arguments


def test_full_render_bounds_audio_before_atomic_promotion(tmp_path: Path) -> None:
    calls: list[object] = []
    remotion_directory = tmp_path / "remotion"
    (remotion_directory / "node_modules").mkdir(parents=True)

    class Runner:
        def run(self, request):
            calls.append(request)
            if request.executable == "fake-npm":
                output = Path(request.arguments[6])
                output.write_bytes(b"remotion render")
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout="",
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffprobe":
                duration = (
                    "1.000000" if request.arguments[-1].endswith("audio.part.mp4") else "1.037333"
                )
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout=(
                        f'{{"streams":[{{"codec_type":"video","duration":"{duration}"}},'
                        f'{{"codec_type":"audio","duration":"{duration}"}}],'
                        f'"format":{{"duration":"{duration}"}}}}'
                    ),
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffmpeg":
                Path(request.arguments[-1]).write_bytes(b"bounded render")
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout="",
                    stderr="",
                    elapsed_ms=1,
                )
            raise AssertionError(f"unexpected executable: {request.executable}")

    props = tmp_path / "props.json"
    props.write_text(
        '{"schema_version":"1.0","project_id":"render-test","width":640,'
        '"height":360,"fps":30,"duration_frames":30,'
        '"background":{"kind":"solid","value":"#000000"},"layers":[],'
        '"audio":[],"captions":[],"transitions":[],"assets":[]}',
        encoding="utf-8",
    )
    output = tmp_path / "render.mp4"
    service = RemotionService(
        remotion_directory,
        npm_path="fake-npm",
        runner=Runner(),
        package_root=package_root(),
        ffmpeg_path="fake-ffmpeg",
        ffprobe_path="fake-ffprobe",
    )

    service.render(props, output)

    assert output.read_bytes() == b"bounded render"
    assert [request.executable for request in calls] == [
        "fake-npm",
        "fake-ffprobe",
        "fake-ffmpeg",
        "fake-ffprobe",
    ]
    assert not list(tmp_path.glob("*.part"))


def test_full_render_does_not_promote_when_audio_boundary_fails(tmp_path: Path) -> None:
    remotion_directory = tmp_path / "remotion"
    (remotion_directory / "node_modules").mkdir(parents=True)

    class Runner:
        def run(self, request):
            if request.executable == "fake-npm":
                Path(request.arguments[6]).write_bytes(b"remotion render")
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout="",
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffprobe":
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout=(
                        '{"streams":[{"codec_type":"video","duration":"1.000000"},'
                        '{"codec_type":"audio","duration":"1.037333"}],'
                        '"format":{"duration":"1.037333"}}'
                    ),
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffmpeg":
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=1,
                    stdout="",
                    stderr="bounded failure",
                    elapsed_ms=1,
                )
            raise AssertionError(f"unexpected executable: {request.executable}")

    props = tmp_path / "props.json"
    props.write_text(
        '{"schema_version":"1.0","project_id":"render-failure-test","width":640,'
        '"height":360,"fps":30,"duration_frames":30,'
        '"background":{"kind":"solid","value":"#000000"},"layers":[],'
        '"audio":[],"captions":[],"transitions":[],"assets":[]}',
        encoding="utf-8",
    )
    output = tmp_path / "render.mp4"
    service = RemotionService(
        remotion_directory,
        npm_path="fake-npm",
        runner=Runner(),
        package_root=package_root(),
        ffmpeg_path="fake-ffmpeg",
        ffprobe_path="fake-ffprobe",
    )

    with pytest.raises(RuntimeError, match="audio-bound render finalization failed"):
        service.render(props, output)

    assert not output.exists()
    assert not list(tmp_path.glob("*.part*"))


def test_full_render_does_not_promote_when_bound_duration_is_wrong(tmp_path: Path) -> None:
    remotion_directory = tmp_path / "remotion"
    (remotion_directory / "node_modules").mkdir(parents=True)
    probe_count = 0

    class Runner:
        def run(self, request):
            nonlocal probe_count
            if request.executable == "fake-npm":
                Path(request.arguments[6]).write_bytes(b"remotion render")
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout="",
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffprobe":
                probe_count += 1
                if probe_count == 1:
                    payload = (
                        '{"streams":[{"codec_type":"video","duration":"1.000000"},'
                        '{"codec_type":"audio","duration":"1.037333"}],'
                        '"format":{"duration":"1.037333"}}'
                    )
                else:
                    payload = (
                        '{"streams":[{"codec_type":"video","duration":"1.000000"},'
                        '{"codec_type":"audio","duration":"0.999000"}],'
                        '"format":{"duration":"1.000000"}}'
                    )
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout=payload,
                    stderr="",
                    elapsed_ms=1,
                )
            if request.executable == "fake-ffmpeg":
                Path(request.arguments[-1]).write_bytes(b"bounded render")
                return ProcessResult(
                    arguments=(request.executable, *request.arguments),
                    exit_code=0,
                    stdout="",
                    stderr="",
                    elapsed_ms=1,
                )
            raise AssertionError(f"unexpected executable: {request.executable}")

    props = tmp_path / "props.json"
    props.write_text(
        '{"schema_version":"1.0","project_id":"render-duration-failure-test",'
        '"width":640,"height":360,"fps":30,"duration_frames":30,'
        '"background":{"kind":"solid","value":"#000000"},"layers":[],'
        '"audio":[],"captions":[],"transitions":[],"assets":[]}',
        encoding="utf-8",
    )
    output = tmp_path / "render.mp4"
    service = RemotionService(
        remotion_directory,
        npm_path="fake-npm",
        runner=Runner(),
        package_root=package_root(),
        ffmpeg_path="fake-ffmpeg",
        ffprobe_path="fake-ffprobe",
    )

    with pytest.raises(RuntimeError, match="duration mismatch"):
        service.render(props, output)

    assert not output.exists()
    assert not list(tmp_path.glob("*.part*"))
