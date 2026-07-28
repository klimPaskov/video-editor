from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import LocalProcessRunner, ProcessRequest
from videoedit.domain.models import TimelineSpec
from videoedit.domain.timeline import frame_to_microseconds
from videoedit.services.artifacts import canonical_sha256, write_text_atomically
from videoedit.services.media import seconds_to_us
from videoedit.services.project import sha256_file
from videoedit.services.visual_timeline import validate_visual_timeline, write_visual_timeline

REMOTION_RENDER_CONCURRENCY = 1


class RemotionService:
    def __init__(
        self,
        remotion_directory: Path,
        npm_path: str = "npm",
        runner: LocalProcessRunner | None = None,
        package_root: Path | None = None,
        ffmpeg_adapter: FFmpegAdapter | None = None,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ) -> None:
        self.remotion_directory = remotion_directory.resolve()
        self.npm_path = shutil.which(npm_path) or shutil.which(f"{npm_path}.cmd") or npm_path
        self.runner = runner or LocalProcessRunner()
        self.package_root = (package_root or Path(__file__).resolve().parents[3]).resolve()
        self.ffmpeg = ffmpeg_adapter or FFmpegAdapter(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            runner=self.runner,
        )

    def stage_asset(
        self,
        project_id: str,
        source: Path,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        digest = sha256_file(source)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError(f"asset hash does not match expected SHA-256: {source}")
        target_root = self.remotion_directory / "public" / "generated" / project_id
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / f"{digest[:16]}-{source.name}"
        if target.is_file():
            if sha256_file(target) != digest:
                raise ValueError(f"staged asset path has a different hash: {target}")
            return f"generated/{project_id}/{target.name}"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".part",
            dir=target_root,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != digest:
                raise ValueError(f"asset changed while staging: {source}")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return f"generated/{project_id}/{target.name}"

    def write_props(self, timeline: TimelineSpec, path: Path) -> Path:
        asset_root = self.remotion_directory / "public"
        if timeline.code_bundle_sha256 is not None:
            current_bundle = self.code_bundle_sha256()
            if current_bundle != timeline.code_bundle_sha256:
                raise ValueError("visual timeline code bundle hash is stale")
        validate_visual_timeline(
            self.package_root,
            timeline.model_dump(mode="json"),
            asset_root=asset_root,
        )
        return write_visual_timeline(
            self.package_root,
            path,
            timeline,
            asset_root=asset_root,
        )

    def code_bundle_sha256(self) -> str:
        return canonical_sha256(self.code_bundle_entries())

    def code_bundle_entries(self) -> list[dict[str, str]]:
        """Return the deterministic source/package entries bound to a composition."""

        candidates = [
            path
            for root in (self.remotion_directory / "src",)
            if root.is_dir()
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx", ".json"}
        ]
        for name in ("package.json", "package-lock.json"):
            path = self.remotion_directory / name
            if path.is_file():
                candidates.append(path)
        if not candidates:
            raise RuntimeError("Remotion code bundle is empty")
        entries = [
            {
                "path": path.relative_to(self.remotion_directory).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in sorted(set(candidates))
        ]
        return entries

    def write_code_bundle(self, path: Path) -> Path:
        """Persist a hash-identical manifest of the Remotion code bundle.

        Gate approvals bind to a project-local file. The file deliberately contains
        only the canonical relative-path/hash entries; the source and package lock
        remain in ``remotion/`` and are rehashed whenever this method is called.
        """

        entries = self.code_bundle_entries()
        expected = canonical_sha256(entries)
        serialized = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(serialized.encode("utf-8")).hexdigest() != expected:
            raise RuntimeError("composition code bundle manifest hash construction failed")
        selected = path.expanduser().resolve()
        if selected.is_file():
            if sha256_file(selected) != expected:
                raise ValueError(f"composition code bundle is stale: {selected}")
            return selected
        write_text_atomically(selected, serialized)
        if sha256_file(selected) != expected:
            raise RuntimeError("composition code bundle manifest changed while writing")
        return selected

    def list_compositions(self) -> str:
        result = self.runner.run(
            ProcessRequest(
                executable=self.npm_path,
                arguments=(
                    "exec",
                    "--",
                    "remotion",
                    "compositions",
                    "src/index.ts",
                    "--log=verbose",
                ),
                working_directory=self.remotion_directory,
                timeout_seconds=180,
            )
        )
        if result.exit_code != 0:
            detail = result.stderr.strip()[-4000:]
            raise RuntimeError(f"Remotion composition listing failed: {detail}")
        return "\n".join(part for part in (result.stdout, result.stderr) if part)

    def _render(
        self,
        props_path: Path,
        output: Path,
        *,
        frames: str | None = None,
        expected_duration_us: int | None = None,
    ) -> None:
        self._ensure_dependencies()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.part{output.suffix}")
        arguments = [
            "exec",
            "--",
            "remotion",
            "render",
            "src/index.ts",
            "AgentEdit",
            str(temporary.resolve()),
            f"--props={props_path.resolve()}",
        ]
        if frames is not None:
            arguments.append(f"--frames={frames}")
        arguments.append(f"--concurrency={REMOTION_RENDER_CONCURRENCY}")
        arguments.append("--log=warn")
        result = self.runner.run(
            ProcessRequest(
                executable=self.npm_path,
                arguments=tuple(arguments),
                working_directory=self.remotion_directory,
                timeout_seconds=1800,
            )
        )
        if result.exit_code != 0:
            temporary.unlink(missing_ok=True)
            detail = result.stderr.strip()[-4000:]
            raise RuntimeError(f"Remotion render failed: {detail}")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Remotion completed without a non-empty render output")
        try:
            if expected_duration_us is not None:
                self._bound_render_audio(temporary, expected_duration_us)
            os.replace(temporary, output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _bound_render_audio(self, rendered: Path, duration_us: int) -> None:
        probe = self.ffmpeg.probe(rendered)
        streams = probe.get("streams", [])
        has_audio = isinstance(streams, list) and any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
        )
        if not has_audio:
            return
        bounded = rendered.with_name(
            f".{rendered.stem}.{uuid.uuid4().hex}.audio.part{rendered.suffix}"
        )
        try:
            self.ffmpeg.bound_audio_to_visual_duration(
                rendered,
                bounded,
                duration_us=duration_us,
            )
            if not bounded.is_file() or bounded.stat().st_size == 0:
                raise RuntimeError("audio-bound Remotion output is empty")
            self._validate_bound_render(bounded, duration_us)
            os.replace(bounded, rendered)
        except BaseException:
            bounded.unlink(missing_ok=True)
            raise

    def _validate_bound_render(self, bounded: Path, duration_us: int) -> None:
        probe = self.ffmpeg.probe(bounded)
        format_value = probe.get("format")
        format_duration_us = (
            seconds_to_us(format_value.get("duration"))
            if isinstance(format_value, Mapping)
            else None
        )
        stream_durations: dict[str, int | None] = {"video": None, "audio": None}
        streams = probe.get("streams", [])
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, Mapping):
                    continue
                codec_type = str(stream.get("codec_type", ""))
                if codec_type in stream_durations and stream_durations[codec_type] is None:
                    stream_durations[codec_type] = seconds_to_us(stream.get("duration"))
        durations: dict[str, int | None] = {
            "format": format_duration_us,
            **stream_durations,
        }
        if any(value != duration_us for value in durations.values()):
            raise RuntimeError(
                "audio-bound Remotion output duration mismatch: "
                f"expected {duration_us}, got {durations}"
            )

    def _ensure_dependencies(self) -> None:
        if not (self.remotion_directory / "node_modules").is_dir():
            raise RuntimeError("Remotion dependencies are missing. Run npm install in remotion/")

    def render(self, props_path: Path, output: Path) -> None:
        timeline = self.load_timeline(props_path, package_root=self.package_root)
        fps = timeline.fps
        if isinstance(fps, int):
            numerator, denominator = fps, 1
        else:
            numerator, denominator = fps.numerator, fps.denominator
        duration_us = frame_to_microseconds(timeline.duration_frames, numerator, denominator)
        self._render(props_path, output, expected_duration_us=duration_us)

    def render_still(self, props_path: Path, output: Path, *, frame: int = 0) -> None:
        if frame < 0:
            raise ValueError("still frame must be nonnegative")
        self._ensure_dependencies()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_directory = Path(tempfile.mkdtemp(prefix="videoedit-remotion-still-"))
        temporary_output = output.with_name(
            f".{output.stem}.{uuid.uuid4().hex}.part{output.suffix}"
        )
        try:
            result = self.runner.run(
                ProcessRequest(
                    executable=self.npm_path,
                    arguments=(
                        "exec",
                        "--",
                        "remotion",
                        "render",
                        "src/index.ts",
                        "AgentEdit",
                        str(temporary_directory.resolve()),
                        f"--props={props_path.resolve()}",
                        f"--frames={frame}-{frame}",
                        "--sequence",
                        "--log=warn",
                    ),
                    working_directory=self.remotion_directory,
                    timeout_seconds=1800,
                )
            )
            if result.exit_code != 0:
                detail = result.stderr.strip()[-4000:]
                raise RuntimeError(f"Remotion still render failed: {detail}")
            frames = sorted(temporary_directory.glob("*.png"))
            if len(frames) != 1 or frames[0].stat().st_size == 0:
                raise RuntimeError("Remotion still render did not produce exactly one PNG")
            shutil.copyfile(frames[0], temporary_output)
            if temporary_output.stat().st_size == 0:
                raise RuntimeError("Remotion still render produced an empty PNG")
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)
            shutil.rmtree(temporary_directory, ignore_errors=True)

    def render_segment(
        self,
        props_path: Path,
        output: Path,
        *,
        start_frame: int,
        end_frame: int,
    ) -> None:
        if start_frame < 0 or end_frame < start_frame:
            raise ValueError("segment frame bounds are invalid")
        self._render(props_path, output, frames=f"{start_frame}-{end_frame}")

    @staticmethod
    def load_timeline(path: Path, package_root: Path | None = None) -> TimelineSpec:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return validate_visual_timeline(
            (package_root or Path(__file__).resolve().parents[3]).resolve(),
            payload,
        )
