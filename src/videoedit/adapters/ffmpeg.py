from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from videoedit.adapters.process import LocalProcessRunner, ProcessRequest, ProcessResult
from videoedit.services.media import seconds_to_us

DEFAULT_VIDEO_CODEC = "libx264"
DEFAULT_VIDEO_BITRATE_BPS = 4_000_000
MIN_VIDEO_BITRATE_BPS = 250_000
MAX_VIDEO_BITRATE_BPS = 100_000_000


def _analysis_window_arguments(
    start_us: int | None,
    end_us: int | None,
) -> tuple[str, ...]:
    if (start_us is None) != (end_us is None):
        raise ValueError("analysis window requires both start_us and end_us")
    if start_us is None or end_us is None:
        return ()
    if start_us < 0 or end_us <= start_us:
        raise ValueError("analysis window must be a positive nonnegative time range")
    return (
        "-ss",
        f"{start_us / 1_000_000:.6f}",
        "-t",
        f"{(end_us - start_us) / 1_000_000:.6f}",
    )


def adapter_encoder_identity(adapter: object) -> dict[str, object]:
    """Read an encoder profile from a real or test adapter for stage binding."""

    identity_method = getattr(adapter, "encoder_identity", None)
    if callable(identity_method):
        value = identity_method()
        if isinstance(value, Mapping):
            return {
                "video_codec": str(value.get("video_codec", DEFAULT_VIDEO_CODEC)),
                "video_bitrate_bps": int(value.get("video_bitrate_bps", DEFAULT_VIDEO_BITRATE_BPS)),
            }
    return {
        "video_codec": str(getattr(adapter, "video_codec", DEFAULT_VIDEO_CODEC)),
        "video_bitrate_bps": int(getattr(adapter, "video_bitrate_bps", DEFAULT_VIDEO_BITRATE_BPS)),
    }


@dataclass(frozen=True, slots=True)
class ChromaKeyConfig:
    key_color: str = "0x00FF00"
    similarity: float = 0.18
    blend: float = 0.08
    despill: bool = True
    despill_color: str = "green"
    despill_mix: float = 0.5
    crop: tuple[int, int, int, int] | None = None
    edge_feather_px: float = 0.0
    edge_erode_iterations: int = 0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"0x[0-9A-Fa-f]{6}", self.key_color):
            raise ValueError("chroma key color must be a six-digit 0xRRGGBB value")
        for name, value, lower, upper in (
            ("similarity", self.similarity, 0.00001, 1.0),
            ("blend", self.blend, 0.0, 1.0),
            ("despill_mix", self.despill_mix, 0.0, 1.0),
            ("edge_feather_px", self.edge_feather_px, 0.0, 64.0),
        ):
            if not math.isfinite(value) or value < lower or value > upper:
                raise ValueError(f"{name} must be between {lower} and {upper}")
        if self.despill_color not in {"green", "blue"}:
            raise ValueError("despill_color must be green or blue")
        if self.edge_erode_iterations < 0 or self.edge_erode_iterations > 8:
            raise ValueError("edge_erode_iterations must be between 0 and 8")
        if self.crop is not None:
            if len(self.crop) != 4:
                raise ValueError("crop must be x, y, width, height")
            x, y, width, height = self.crop
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                raise ValueError("crop must contain nonnegative origin and positive dimensions")

    def filters(self) -> tuple[str, ...]:
        filters: list[str] = []
        if self.crop is not None:
            x, y, width, height = self.crop
            filters.append(f"crop={width}:{height}:{x}:{y}")
        filters.append(f"chromakey={self.key_color}:{self.similarity:.6f}:{self.blend:.6f}")
        if self.despill:
            filters.append(f"despill=type={self.despill_color}:mix={self.despill_mix:.6f}")
        return tuple(filters)


class FFmpegAdapter:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        runner: LocalProcessRunner | None = None,
        *,
        video_codec: str = DEFAULT_VIDEO_CODEC,
        video_bitrate_bps: int = DEFAULT_VIDEO_BITRATE_BPS,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.runner = runner or LocalProcessRunner()
        self.video_codec = self._validate_video_codec(video_codec)
        self.video_bitrate_bps = self._validate_video_bitrate(video_bitrate_bps)

    def encoder_identity(self) -> dict[str, object]:
        """Return the configured video encoder profile for cache and provenance binding."""

        return {
            "video_codec": self.video_codec,
            "video_bitrate_bps": self.video_bitrate_bps,
        }

    @staticmethod
    def _validate_video_codec(video_codec: str) -> str:
        if not video_codec or not re.fullmatch(r"[A-Za-z0-9_.-]+", video_codec):
            raise ValueError("video codec must be a safe non-empty process argument")
        return video_codec

    @staticmethod
    def _validate_video_bitrate(video_bitrate_bps: int) -> int:
        if not MIN_VIDEO_BITRATE_BPS <= video_bitrate_bps <= MAX_VIDEO_BITRATE_BPS:
            raise ValueError(
                "video bitrate must be between "
                f"{MIN_VIDEO_BITRATE_BPS} and {MAX_VIDEO_BITRATE_BPS} bits per second"
            )
        return video_bitrate_bps

    def _selected_video_codec(self, video_codec: str | None) -> str:
        return self._validate_video_codec(video_codec or self.video_codec)

    def _video_pixel_format(self, video_codec: str) -> str:
        return "nv12" if video_codec == "h264_amf" else "yuv420p"

    def _video_duration_us(self, source: Path) -> int:
        probe = self.probe(source)
        video_stream = next(
            (
                item
                for item in probe.get("streams", [])
                if isinstance(item, Mapping) and item.get("codec_type") == "video"
            ),
            None,
        )
        raw_duration = video_stream.get("duration") if isinstance(video_stream, Mapping) else None
        if raw_duration is None:
            format_payload = probe.get("format")
            raw_duration = (
                format_payload.get("duration") if isinstance(format_payload, Mapping) else None
            )
        duration_us = seconds_to_us(raw_duration)
        if duration_us is None or duration_us <= 0:
            raise ValueError("source video duration is required for audio-bound output")
        return duration_us

    def bound_audio_to_visual_duration(
        self,
        source: Path,
        output: Path,
        *,
        duration_us: int,
        audio_codec: str = "aac",
    ) -> ProcessResult:
        """Rebound a mixed audio stream to an authoritative visual duration.

        Remotion can leave codec/container padding after the final visual frame.
        Keep the rendered video packets intact and deterministically pad/trim the
        mixed audio stream to the frame-derived boundary before ``-shortest``.
        """

        if duration_us <= 0:
            raise ValueError("audio-bound visual duration must be positive")
        duration_text = f"{duration_us // 1_000_000}.{duration_us % 1_000_000:06d}"
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-hide_banner",
                    "-i",
                    str(source.resolve()),
                    "-filter_complex",
                    f"[0:a]apad,atrim=start=0:end={duration_text},asetpts=PTS-STARTPTS[aout]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    audio_codec,
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-t",
                    duration_text,
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "-1",
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(output.resolve()),
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=1800,
                stderr_limit_bytes=2_000_000,
            )
        )
        self._require_success(result, "audio-bound render finalization failed")
        return result

    def _video_encode_arguments(
        self,
        video_codec: str,
        *,
        crf: int,
        preset: str,
        qp: int | None = None,
    ) -> tuple[str, ...]:
        if video_codec == "h264_amf":
            if qp is not None:
                raise ValueError("explicit QP is not supported by the AMD AMF profile")
            return (
                "-c:v",
                video_codec,
                "-b:v",
                str(self.video_bitrate_bps),
                "-pix_fmt",
                self._video_pixel_format(video_codec),
            )
        if qp is not None and not 0 <= qp <= 51:
            raise ValueError("H.264 QP must be between 0 and 51")
        rate_control = ("-qp", str(qp)) if qp is not None else ("-crf", str(crf))
        return (
            "-c:v",
            video_codec,
            "-preset",
            preset,
            *rate_control,
            "-pix_fmt",
            self._video_pixel_format(video_codec),
        )

    def version(self, executable: str | None = None) -> str:
        selected = executable or self.ffmpeg_path
        result = self.runner.run(
            ProcessRequest(
                executable=selected,
                arguments=("-version",),
                timeout_seconds=30,
            )
        )
        self._require_success(result, f"{selected} version check failed")
        first_line = (result.stdout or result.stderr).splitlines()[0]
        match = re.search(r"version\s+([^\s]+)", first_line)
        return match.group(1) if match else first_line[:120]

    def probe(self, path: Path) -> dict[str, Any]:
        resolved_path = path.resolve()
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffprobe_path,
                arguments=(
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(resolved_path),
                ),
                working_directory=resolved_path.parent,
                timeout_seconds=120,
            )
        )
        self._require_success(result, "ffprobe failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("ffprobe output must be a JSON object")
        return payload

    def create_edit_proxy(
        self,
        source: Path,
        output: Path,
        *,
        width: int = 1280,
        height: int = 720,
    ) -> ProcessResult:
        if width <= 0 or height <= 0:
            raise ValueError("proxy dimensions must be positive")
        pixel_format = self._video_pixel_format(self.video_codec)
        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-hide_banner",
                "-i",
                str(source.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format={pixel_format}"
                ),
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=23,
                    preset="veryfast",
                ),
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "0",
                "-map_chapters",
                "-1",
                "-movflags",
                "+faststart",
                str(output.resolve()),
            ),
            working_directory=output.parent.resolve(),
            timeout_seconds=1800,
        )
        result = self.runner.run(request)
        self._require_success(result, "edit proxy generation failed")
        return result

    def create_speech_proxy(
        self,
        source: Path,
        output: Path,
        *,
        sample_rate_hz: int = 16_000,
    ) -> ProcessResult:
        if sample_rate_hz <= 0:
            raise ValueError("speech proxy sample rate must be positive")
        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-hide_banner",
                "-i",
                str(source.resolve()),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate_hz),
                "-c:a",
                "pcm_s16le",
                "-map_metadata",
                "-1",
                "-f",
                "wav",
                str(output.resolve()),
            ),
            working_directory=output.parent.resolve(),
            timeout_seconds=900,
        )
        result = self.runner.run(request)
        self._require_success(result, "speech proxy generation failed")
        return result

    def detect_silence(
        self,
        source: Path,
        threshold_db: float = -38.0,
        minimum_duration_us: int = 650_000,
    ) -> str:
        return self.detect_silence_result(
            source,
            threshold_db=threshold_db,
            minimum_duration_us=minimum_duration_us,
        ).stderr

    def detect_silence_result(
        self,
        source: Path,
        threshold_db: float = -38.0,
        minimum_duration_us: int = 650_000,
    ) -> ProcessResult:
        if minimum_duration_us <= 0:
            raise ValueError("minimum silence duration must be positive")
        if not math.isfinite(threshold_db) or threshold_db >= 0:
            raise ValueError("silence threshold must be a finite negative number")
        minimum_seconds = minimum_duration_us / 1_000_000
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "info",
                "-hide_banner",
                "-nostats",
                "-i",
                str(source),
                "-af",
                f"silencedetect=noise={threshold_db}dB:d={minimum_seconds:.6f}",
                "-f",
                "null",
                "-",
            ),
            working_directory=source.parent,
            timeout_seconds=600,
        )
        result = self.runner.run(request)
        self._require_success(result, "silence detection failed")
        return result

    def render_keep_ranges(
        self,
        source: Path,
        keep_ranges: list[tuple[int, int]],
        output: Path,
        video_codec: str | None = None,
        audio_codec: str = "aac",
        crf: int = 18,
        preset: str = "medium",
        qp: int | None = None,
    ) -> ProcessResult:
        if not keep_ranges:
            raise ValueError("at least one keep range is required")
        for start_us, end_us in keep_ranges:
            if start_us < 0 or end_us <= start_us:
                raise ValueError(f"invalid keep range {start_us}:{end_us}")

        output.parent.mkdir(parents=True, exist_ok=True)
        selected_video_codec = self._selected_video_codec(video_codec)
        pixel_format = self._video_pixel_format(selected_video_codec)
        probe = self.probe(source)
        has_audio = any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))

        # Join QA and segment review normally request one contiguous preview.
        # Seek before opening the source in that case so FFmpeg does not decode
        # the complete long-form recording for every small review window.  The
        # multi-range path below remains the exact filter-graph implementation
        # used by base-edit compilation.
        if len(keep_ranges) == 1 and has_audio:
            start_us, end_us = keep_ranges[0]
            duration_text = f"{(end_us - start_us) / 1_000_000:.6f}"
            fast_arguments = [
                "-y",
                "-hide_banner",
                "-ss",
                f"{start_us / 1_000_000:.6f}",
                "-i",
                str(source.resolve()),
                "-t",
                duration_text,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                f"format={pixel_format}",
                *self._video_encode_arguments(
                    selected_video_codec,
                    crf=crf,
                    preset=preset,
                    qp=qp,
                ),
                "-c:a",
                audio_codec,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-movflags",
                "+faststart",
                "-shortest",
                str(output.resolve()),
            ]
            result = self.runner.run(
                ProcessRequest(
                    executable=self.ffmpeg_path,
                    arguments=tuple(fast_arguments),
                    working_directory=output.parent.resolve(),
                    timeout_seconds=1800,
                    stderr_limit_bytes=2_000_000,
                )
            )
            self._require_success(result, "base timeline preview render failed")
            return result

        arguments: list[str] = ["-y", "-i", str(source.resolve())]
        if not has_audio:
            arguments.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                ]
            )

        graph_parts: list[str] = []
        concat_inputs: list[str] = []
        for index, (start_us, end_us) in enumerate(keep_ranges):
            start = start_us / 1_000_000
            end = end_us / 1_000_000
            duration = (end_us - start_us) / 1_000_000
            graph_parts.append(
                f"[0:v]trim=start={start:.6f}:end={end:.6f},"
                f"setpts=PTS-STARTPTS,format={pixel_format}[v{index}]"
            )
            if has_audio:
                graph_parts.append(
                    f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                graph_parts.append(
                    f"[1:a]atrim=start=0:end={duration:.6f},asetpts=PTS-STARTPTS[a{index}]"
                )
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])

        graph_parts.append(
            "".join(concat_inputs) + f"concat=n={len(keep_ranges)}:v=1:a=1[vout][aout]"
        )
        arguments.extend(
            [
                "-filter_complex",
                ";".join(graph_parts),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                *self._video_encode_arguments(
                    selected_video_codec,
                    crf=crf,
                    preset=preset,
                    qp=qp,
                ),
                "-c:a",
                audio_codec,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(output.resolve()),
            ]
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=tuple(arguments),
            working_directory=output.parent.resolve(),
            timeout_seconds=3600,
        )
        result = self.runner.run(request)
        self._require_success(result, "base timeline render failed")
        return result

    def concat_media(
        self,
        inputs: Sequence[Path],
        output: Path,
        *,
        video_codec: str | None = None,
        audio_codec: str = "aac",
        crf: int = 18,
        preset: str = "medium",
        qp: int | None = None,
    ) -> ProcessResult:
        """Concatenate approved, local A/V segments through a typed FFmpeg graph."""

        selected = [path.resolve() for path in inputs]
        if not selected:
            raise ValueError("at least one media input is required")
        selected_video_codec = self._selected_video_codec(video_codec)
        pixel_format = self._video_pixel_format(selected_video_codec)
        for path in selected:
            if not path.is_file():
                raise FileNotFoundError(path)
            probe = self.probe(path)
            streams = probe.get("streams", [])
            if not isinstance(streams, list) or not any(
                isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
            ):
                raise ValueError(f"media input has no video stream: {path}")
            if not any(
                isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
            ):
                raise ValueError(f"media input has no production audio stream: {path}")

        graph: list[str] = []
        concat_inputs: list[str] = []
        for index in range(len(selected)):
            graph.append(f"[{index}:v:0]setpts=PTS-STARTPTS,format={pixel_format}[v{index}]")
            graph.append(
                f"[{index}:a:0]aresample=48000,aformat=sample_rates=48000:"
                f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]"
            )
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
        graph.append("".join(concat_inputs) + f"concat=n={len(selected)}:v=1:a=1[vout][aout]")
        graph.append("[aout]apad[aout_padded]")
        arguments: list[str] = ["-y", "-hide_banner"]
        for path in selected:
            arguments.extend(["-i", str(path)])
        arguments.extend(
            [
                "-filter_complex",
                ";".join(graph),
                "-map",
                "[vout]",
                "-map",
                "[aout_padded]",
                *self._video_encode_arguments(
                    selected_video_codec,
                    crf=crf,
                    preset=preset,
                    qp=qp,
                ),
                "-c:a",
                audio_codec,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-movflags",
                "+faststart",
                "-shortest",
                str(output.resolve()),
            ]
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=tuple(arguments),
                working_directory=output.parent.resolve(),
                timeout_seconds=3600,
                stderr_limit_bytes=2_000_000,
            )
        )
        self._require_success(result, "approved segment assembly failed")
        return result

    def render_scaled_derivative(
        self,
        source: Path,
        output: Path,
        *,
        width: int,
        height: int,
        video_codec: str | None = None,
        audio_codec: str = "aac",
        crf: int = 20,
    ) -> ProcessResult:
        """Render one deterministic platform derivative from the approved master."""

        if width <= 0 or height <= 0:
            raise ValueError("derivative dimensions must be positive")
        selected_video_codec = self._selected_video_codec(video_codec)
        pixel_format = self._video_pixel_format(selected_video_codec)
        video_duration_us = self._video_duration_us(source)
        video_duration_text = (
            f"{video_duration_us // 1_000_000}.{video_duration_us % 1_000_000:06d}"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-hide_banner",
                    "-i",
                    str(source.resolve()),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format={pixel_format}",
                    *self._video_encode_arguments(
                        selected_video_codec,
                        crf=crf,
                        preset="medium",
                    ),
                    "-c:a",
                    audio_codec,
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-af",
                    f"apad,atrim=duration={video_duration_text}",
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "-1",
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(output.resolve()),
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=3600,
                stderr_limit_bytes=2_000_000,
            )
        )
        self._require_success(result, "platform derivative render failed")
        return result

    def render_retimed_segments(
        self,
        source: Path,
        segments: Sequence[Mapping[str, Any]],
        output: Path,
        *,
        video_codec: str | None = None,
        audio_codec: str = "aac",
        crf: int = 18,
        preset: str = "medium",
        qp: int | None = None,
        audio_edge_fade_us: int = 0,
        duration_us: int | None = None,
        frame_rate: Fraction | None = None,
    ) -> ProcessResult:
        """Render an ordered retimed map with identical picture/audio boundaries."""

        if not segments:
            raise ValueError("retimed render requires at least one segment")
        selected_video_codec = self._selected_video_codec(video_codec)
        if audio_edge_fade_us < 0 or audio_edge_fade_us > 100_000:
            raise ValueError("audio edge fade must be between 0 and 100000 microseconds")
        if duration_us is not None and duration_us <= 0:
            raise ValueError("retimed output duration must be positive")
        if frame_rate is not None and (frame_rate.numerator <= 0 or frame_rate.denominator <= 0):
            raise ValueError("retimed output frame rate must be positive")
        pixel_format = self._video_pixel_format(selected_video_codec)
        probe = self.probe(source)
        has_audio = any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio"
            for stream in probe.get("streams", [])
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        arguments: list[str] = ["-y", "-i", str(source.resolve())]
        if not has_audio:
            arguments.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        audio_input = "0:a" if has_audio else "1:a"
        graph: list[str] = []
        concat_inputs: list[str] = []
        for index, segment in enumerate(segments):
            source_range = segment.get("source_range")
            if not isinstance(source_range, Mapping):
                raise ValueError("retimed segment is missing source_range")
            start_us = int(source_range["start_us"])
            end_us = int(source_range["end_us"])
            if start_us < 0 or end_us <= start_us:
                raise ValueError("retimed segment source range is invalid")
            rate = float(segment.get("playback_rate", 1))
            if rate < 1 or rate > 8:
                raise ValueError("retimed segment playback rate must be between 1 and 8")
            start = start_us / 1_000_000
            end = end_us / 1_000_000
            graph.append(
                f"[0:v]trim=start={start:.6f}:end={end:.6f},"
                f"setpts=(PTS-STARTPTS)/{rate:.9f},format={pixel_format}[v{index}]"
            )
            audio_filters = [
                f"atrim=start={start:.6f}:end={end:.6f}",
                "asetpts=PTS-STARTPTS",
            ]
            if rate > 1:
                audio_mode = str(segment.get("audio_mode", "audible_pitch_preserved"))
                if audio_mode == "muted":
                    audio_filters.append("volume=0")
                else:
                    audio_filters.extend(self._atempo_chain(rate))
            if audio_edge_fade_us > 0:
                output_duration = (end_us - start_us) / 1_000_000 / rate
                fade_duration = min(audio_edge_fade_us / 1_000_000, output_duration / 2)
                if fade_duration > 0:
                    audio_filters.extend(
                        [
                            f"afade=t=in:st=0:d={fade_duration:.6f}:curve=hsin",
                            f"afade=t=out:st={max(0.0, output_duration - fade_duration):.6f}:"
                            f"d={fade_duration:.6f}:curve=hsin",
                        ]
                    )
            graph.append(f"[{audio_input}]" + ",".join(audio_filters) + f"[a{index}]")
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
        graph.append("".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[vout][aout]")
        graph.append("[aout]apad[aout_padded]")
        filter_graph = ";".join(graph)
        filter_arguments: list[str]
        if len(filter_graph) > 24_000:
            filter_script = output.parent / f".{output.stem}.filtergraph.txt"
            filter_script.write_text(filter_graph, encoding="utf-8")
            filter_arguments = ["-filter_complex_script", str(filter_script.resolve())]
        else:
            filter_arguments = ["-filter_complex", filter_graph]
        duration_arguments = (
            ["-t", f"{duration_us / 1_000_000:.6f}"] if duration_us is not None else []
        )
        frame_rate_arguments = (
            ["-fps_mode", "cfr", "-r", f"{frame_rate.numerator}/{frame_rate.denominator}"]
            if frame_rate is not None
            else []
        )
        arguments.extend(
            [
                *filter_arguments,
                "-map",
                "[vout]",
                "-map",
                "[aout_padded]",
                *self._video_encode_arguments(
                    selected_video_codec,
                    crf=crf,
                    preset=preset,
                    qp=qp,
                ),
                *frame_rate_arguments,
                "-c:a",
                audio_codec,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                "-shortest",
                *duration_arguments,
                str(output.resolve()),
            ]
        )
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=tuple(arguments),
                working_directory=output.parent.resolve(),
                timeout_seconds=3600,
            )
        )
        self._require_success(result, "retimed segment render failed")
        return result

    @staticmethod
    def _atempo_chain(playback_rate: float) -> list[str]:
        remaining = playback_rate
        filters: list[str] = []
        while remaining > 2.0:
            filters.append("atempo=2.000000")
            remaining /= 2.0
        filters.append(f"atempo={remaining:.9f}")
        return filters

    def measure_loudness(
        self,
        source: Path,
        *,
        stream: str = "0:a:0",
    ) -> ProcessResult:
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "info",
                "-hide_banner",
                "-nostats",
                "-i",
                str(source.resolve()),
                "-map",
                stream,
                "-af",
                "ebur128=framelog=verbose:peak=true",
                "-f",
                "null",
                "-",
            ),
            working_directory=source.parent.resolve(),
            timeout_seconds=900,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "loudness measurement failed")
        return result

    def normalize_loudness(
        self,
        source: Path,
        output: Path,
        measurement: Mapping[str, float],
        *,
        integrated_target_lufs: float = -16.0,
        true_peak_target_dbfs: float = -1.5,
        loudness_range_target_lu: float = 11.0,
    ) -> ProcessResult:
        values = {
            "input_i": float(measurement["integrated_lufs"]),
            "input_tp": float(measurement["true_peak_dbfs"]),
            "input_lra": float(measurement["loudness_range_lu"]),
            "input_thresh": float(measurement["threshold_lufs"]),
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("loudness measurement contains non-finite values")
        if not math.isfinite(integrated_target_lufs) or not math.isfinite(true_peak_target_dbfs):
            raise ValueError("loudness targets must be finite")
        if not math.isfinite(loudness_range_target_lu) or loudness_range_target_lu <= 0:
            raise ValueError("loudness range target must be positive and finite")
        output.parent.mkdir(parents=True, exist_ok=True)
        filter_value = (
            f"loudnorm=I={integrated_target_lufs:.3f}:TP={true_peak_target_dbfs:.3f}:"
            f"LRA={loudness_range_target_lu:.3f}:measured_I={values['input_i']:.3f}:"
            f"measured_TP={values['input_tp']:.3f}:measured_LRA={values['input_lra']:.3f}:"
            f"measured_thresh={values['input_thresh']:.3f}:offset=0.000:linear=true:"
            "print_format=json"
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-hide_banner",
                "-i",
                str(source.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "copy",
                "-af",
                filter_value,
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "0",
                "-map_chapters",
                "-1",
                "-movflags",
                "+faststart",
                str(output.resolve()),
            ),
            working_directory=output.parent.resolve(),
            timeout_seconds=1800,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "loudness normalization failed")
        return result

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
        duck_speech: bool = True,
    ) -> ProcessResult:
        """Mix one licensed transition cue into production audio deterministically."""

        if start_us < 0:
            raise ValueError("transition sound start must be nonnegative")
        if not math.isfinite(gain_db) or gain_db < -60 or gain_db > 12:
            raise ValueError("transition sound gain must be between -60 and 12 dB")
        if fade_in_us < 0 or fade_out_us < 0:
            raise ValueError("transition sound fades must be nonnegative")
        source_probe = self.probe(source)
        sound_probe = self.probe(sound)
        source_audio = next(
            (item for item in source_probe.get("streams", []) if item.get("codec_type") == "audio"),
            None,
        )
        sound_audio = next(
            (item for item in sound_probe.get("streams", []) if item.get("codec_type") == "audio"),
            None,
        )
        if not isinstance(source_audio, dict):
            raise ValueError("transition sound mix source has no production audio stream")
        if not isinstance(sound_audio, dict):
            raise ValueError("transition sound asset has no audio stream")
        sound_duration_us = seconds_to_us(sound_audio.get("duration")) or 0
        if sound_duration_us <= 0:
            raise ValueError("transition sound asset has no positive duration")
        if fade_in_us + fade_out_us >= sound_duration_us:
            raise ValueError("transition sound fades consume the complete asset")

        effect_filters = [
            "asetpts=PTS-STARTPTS",
            f"volume={gain_db:.3f}dB",
        ]
        if fade_in_us > 0:
            effect_filters.append(f"afade=t=in:st=0:d={fade_in_us / 1_000_000:.6f}")
        if fade_out_us > 0:
            fade_start_us = max(0, sound_duration_us - fade_out_us)
            effect_filters.append(
                f"afade=t=out:st={fade_start_us / 1_000_000:.6f}:d={fade_out_us / 1_000_000:.6f}"
            )
        effect_filters.append(f"adelay={round(start_us / 1_000)}:all=1")
        graph = [f"[1:a]{','.join(effect_filters)}[effect]"]
        if duck_speech:
            graph.append(
                "[effect][0:a]sidechaincompress=threshold=0.020:ratio=6:"
                "attack=10:release=180:makeup=1:link=average[ducked]"
            )
            effect_label = "[ducked]"
        else:
            effect_label = "[effect]"
        graph.append(
            f"[0:a]{effect_label}amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        graph.append("[aout]apad[aout_padded]")
        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-hide_banner",
                "-i",
                str(source.resolve()),
                "-i",
                str(sound.resolve()),
                "-filter_complex",
                ";".join(graph),
                "-map",
                "0:v:0",
                "-map",
                "[aout_padded]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "0",
                "-map_chapters",
                "-1",
                "-movflags",
                "+faststart",
                "-shortest",
                str(output.resolve()),
            ),
            working_directory=output.parent.resolve(),
            timeout_seconds=1800,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "transition sound mix failed")
        return result

    def probe_frame_count(self, path: Path) -> int | None:
        resolved_path = path.resolve()
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffprobe_path,
                arguments=(
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_frames",
                    "-show_entries",
                    "stream=nb_read_frames,nb_frames",
                    "-of",
                    "json",
                    str(resolved_path),
                ),
                working_directory=resolved_path.parent,
                timeout_seconds=120,
            )
        )
        self._require_success(result, "ffprobe frame count failed")
        payload = json.loads(result.stdout)
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
            return None
        stream = streams[0]
        for key in ("nb_read_frames", "nb_frames"):
            value = stream.get(key)
            if value not in (None, "N/A", ""):
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count >= 0:
                    return count
        return None

    def measure_clipping(
        self,
        source: Path,
        *,
        stream: str = "0:a:0",
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> ProcessResult:
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "info",
                "-hide_banner",
                "-nostats",
                "-i",
                str(source.resolve()),
                *_analysis_window_arguments(start_us, end_us),
                "-map",
                stream,
                "-af",
                "astats=metadata=1:reset=0",
                "-f",
                "null",
                "-",
            ),
            working_directory=source.parent.resolve(),
            timeout_seconds=900,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "audio clipping measurement failed")
        return result

    def full_decode_check(self, source: Path, *, strict: bool = False) -> ProcessResult:
        resolved_source = source.resolve()
        strict_arguments = ("-xerror",) if strict else ()
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "error",
                *strict_arguments,
                "-i",
                str(resolved_source),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ),
            working_directory=resolved_source.parent,
            timeout_seconds=3600,
        )
        return self.runner.run(request)

    def detect_black_frames(
        self,
        source: Path,
        *,
        amount_percent: float = 98.0,
        threshold: int = 32,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> ProcessResult:
        if not 0 <= amount_percent <= 100:
            raise ValueError("black-frame amount must be between 0 and 100 percent")
        if not 0 <= threshold <= 255:
            raise ValueError("black-frame threshold must be between 0 and 255")
        resolved_source = source.resolve()
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-v",
                    "info",
                    "-hide_banner",
                    "-nostats",
                    "-i",
                    str(resolved_source),
                    *_analysis_window_arguments(start_us, end_us),
                    "-vf",
                    f"blackframe=amount={amount_percent:.3f}:threshold={threshold}",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ),
                working_directory=resolved_source.parent,
                timeout_seconds=900,
                stderr_limit_bytes=2_000_000,
            )
        )
        self._require_success(result, "black-frame detection failed")
        return result

    def detect_freeze_frames(
        self,
        source: Path,
        *,
        minimum_duration_us: int = 200_000,
        noise_db: float = -60.0,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> ProcessResult:
        if minimum_duration_us <= 0:
            raise ValueError("freeze detection duration must be positive")
        if not math.isfinite(noise_db) or noise_db >= 0:
            raise ValueError("freeze detection noise must be a finite negative number")
        resolved_source = source.resolve()
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-v",
                    "info",
                    "-hide_banner",
                    "-nostats",
                    "-i",
                    str(resolved_source),
                    *_analysis_window_arguments(start_us, end_us),
                    "-vf",
                    f"freezedetect=n={noise_db:.3f}dB:d={minimum_duration_us / 1_000_000:.6f}",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ),
                working_directory=resolved_source.parent,
                timeout_seconds=900,
                stderr_limit_bytes=2_000_000,
            )
        )
        self._require_success(result, "freeze-frame detection failed")
        return result

    def generate_demo_source(self, output: Path, duration_seconds: int = 6) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        video_filter = (
            "drawbox=x='440+20*sin(t*2)':y=130:w=320:h=520:color=0xD6A17A:t=fill,"
            "drawbox=x=515:y=335:w=84:h=84:color=0x2864FF:t=fill"
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x00FF00:s=1280x720:r=30:d={duration_seconds}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=48000:duration={duration_seconds}",
                "-vf",
                video_filter,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=18,
                    preset="medium",
                ),
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=180,
        )
        result = self.runner.run(request)
        self._require_success(result, "demo source generation failed")
        return result

    def generate_demo_mask(self, output: Path, duration_seconds: int = 6) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=1280x720:r=30:d={duration_seconds}",
                "-vf",
                "drawbox=x=515:y=335:w=84:h=84:color=white:t=fill",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=180,
        )
        result = self.runner.run(request)
        self._require_success(result, "demo mask generation failed")
        return result

    def generate_demo_plate(self, output: Path, duration_seconds: int = 6) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x182848:s=1280x720:r=30:d={duration_seconds}",
                "-vf",
                (
                    "drawbox=x=0:y=0:w=iw:h=ih:color=0x182848:t=fill,"
                    "drawbox=x=0:y=360:w=iw:h=360:color=0x4B6CB7:t=fill,"
                    "drawtext=text='TEXT BEHIND SUBJECT':fontcolor=white:"
                    "fontsize=68:x=(w-text_w)/2:y=(h-text_h)/2"
                ),
                "-an",
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=18,
                    preset="medium",
                ),
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=180,
        )
        result = self.runner.run(request)
        self._require_success(result, "demo plate generation failed")
        return result

    def generate_edit_demo_source(self, output: Path) -> ProcessResult:
        """Create a six second test clip with a two second silent interval."""

        output.parent.mkdir(parents=True, exist_ok=True)
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=640x360:r=30:d=6",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:sample_rate=48000:duration=2",
                "-filter_complex",
                "[1:a][2:a][3:a]concat=n=3:v=0:a=1[aout]",
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=18,
                    preset="medium",
                ),
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=180,
        )
        result = self.runner.run(request)
        self._require_success(result, "edit demo source generation failed")
        return result

    def recolor_with_mask(
        self,
        source: Path,
        mask: Path,
        output: Path,
        hue_degrees: float = 100,
    ) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        pixel_format = self._video_pixel_format(self.video_codec)
        graph = (
            "[0:v]format=yuv444p[base];"
            f"[0:v]format=yuv444p,hue=h={hue_degrees}:s=1.4,format=rgba[colored];"
            "[1:v]format=gray[mask];"
            f"[colored][mask]alphamerge[overlay];[base][overlay]"
            f"overlay=shortest=1:format=auto,format={pixel_format}[outv]"
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-i",
                str(source),
                "-i",
                str(mask),
                "-filter_complex",
                graph,
                "-map",
                "[outv]",
                "-map",
                "0:a?",
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=18,
                    preset="medium",
                ),
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=300,
        )
        result = self.runner.run(request)
        self._require_success(result, "mask recolor failed")
        return result

    def chroma_key_foreground(
        self,
        source: Path,
        output: Path,
        key_color: str = "0x00FF00",
        similarity: float = 0.18,
        blend: float = 0.08,
        *,
        config: ChromaKeyConfig | None = None,
        despill: bool = True,
        despill_color: str = "green",
        despill_mix: float = 0.5,
        crop: tuple[int, int, int, int] | None = None,
        edge_feather_px: float = 0.0,
        edge_erode_iterations: int = 0,
    ) -> ProcessResult:
        selected = config or ChromaKeyConfig(
            key_color=key_color,
            similarity=similarity,
            blend=blend,
            despill=despill,
            despill_color=despill_color,
            despill_mix=despill_mix,
            crop=crop,
            edge_feather_px=edge_feather_px,
            edge_erode_iterations=edge_erode_iterations,
        )
        probe = self.probe(source)
        video_stream = next(
            (
                stream
                for stream in probe.get("streams", [])
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(video_stream, dict):
            raise ValueError("chroma-key source has no video stream")
        if selected.crop is not None:
            x, y, width, height = selected.crop
            source_width = int(video_stream.get("width", 0))
            source_height = int(video_stream.get("height", 0))
            if x + width > source_width or y + height > source_height:
                raise ValueError("chroma-key crop exceeds source video dimensions")
        output.parent.mkdir(parents=True, exist_ok=True)
        pre_key = ",".join(selected.filters())
        alpha_filters = ["alphaextract", "format=gray"]
        if selected.edge_feather_px > 0:
            alpha_filters.append(f"boxblur=luma_radius={selected.edge_feather_px:.3f}:luma_power=1")
        for _ in range(selected.edge_erode_iterations):
            alpha_filters.append("erosion=coordinates=255")
        filter_graph = (
            f"[0:v]{pre_key},split[fg][alpha_source];"
            f"[alpha_source]{','.join(alpha_filters)}[alpha];"
            "[fg][alpha]alphamerge,format=yuva444p10le[outv]"
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-y",
                "-i",
                str(source),
                "-filter_complex",
                filter_graph,
                "-map",
                "[outv]",
                "-an",
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4",
                "-pix_fmt",
                "yuva444p10le",
                str(output),
            ),
            working_directory=output.parent,
            timeout_seconds=300,
        )
        result = self.runner.run(request)
        self._require_success(result, "chroma-key extraction failed")
        return result

    def measure_alpha(self, source: Path, *, frame_index: int | None = None) -> ProcessResult:
        if frame_index is not None and frame_index < 0:
            raise ValueError("alpha measurement frame index must be nonnegative")
        resolved_source = source.resolve()
        alpha_filter = "alphaextract,format=gray,signalstats,metadata=print:file=-"
        if frame_index is not None:
            alpha_filter = f"select=eq(n\\,{frame_index}),{alpha_filter}"
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "info",
                "-hide_banner",
                "-nostats",
                "-i",
                str(resolved_source),
                "-vf",
                alpha_filter,
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ),
            working_directory=resolved_source.parent,
            timeout_seconds=900,
            stdout_limit_bytes=2_000_000,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "alpha measurement failed")
        return result

    def measure_mask(self, source: Path, *, frame_index: int | None = None) -> ProcessResult:
        if frame_index is not None and frame_index < 0:
            raise ValueError("mask measurement frame index must be nonnegative")
        resolved_source = source.resolve()
        mask_filter = "format=gray,signalstats,metadata=print:file=-"
        if frame_index is not None:
            mask_filter = f"select=eq(n\\,{frame_index}),{mask_filter}"
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=(
                "-v",
                "info",
                "-hide_banner",
                "-nostats",
                "-i",
                str(resolved_source),
                "-vf",
                mask_filter,
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ),
            working_directory=resolved_source.parent,
            timeout_seconds=900,
            stdout_limit_bytes=2_000_000,
            stderr_limit_bytes=2_000_000,
        )
        result = self.runner.run(request)
        self._require_success(result, "mask measurement failed")
        return result

    def overlay_foreground(
        self,
        plate: Path,
        foreground: Path,
        output: Path,
        audio_source: Path | None = None,
    ) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        arguments: list[str] = [
            "-y",
            "-i",
            str(plate),
            "-i",
            str(foreground),
        ]
        audio_index = 0
        if audio_source is not None:
            arguments.extend(["-i", str(audio_source)])
            audio_index = 2
        pixel_format = self._video_pixel_format(self.video_codec)
        visual_duration_text: str | None = None
        if audio_source is not None:
            # AAC/container timestamps can end a few milliseconds before the
            # final visual frame. Bind padded production audio to the shorter
            # visual input explicitly; relying on ``-shortest`` alone can make
            # AMF intermittently retain an AAC/container tail beyond the last
            # encoded frame.
            visual_duration_us = min(
                self._video_duration_us(plate),
                self._video_duration_us(foreground),
            )
            visual_duration_text = (
                f"{visual_duration_us // 1_000_000}.{visual_duration_us % 1_000_000:06d}"
            )
            audio_filter = (
                f"[2:a]apad,atrim=start=0:end={visual_duration_text},asetpts=PTS-STARTPTS[outa]"
            )
        else:
            audio_filter = None
        filter_graph = f"[0:v][1:v]overlay=shortest=1:format=auto,format={pixel_format}[outv]"
        if audio_filter is not None:
            filter_graph = f"{filter_graph};{audio_filter}"
        arguments.extend(
            [
                "-filter_complex",
                filter_graph,
                "-map",
                "[outv]",
                "-map",
                "[outa]" if audio_filter is not None else f"{audio_index}:a?",
                *self._video_encode_arguments(
                    self.video_codec,
                    crf=18,
                    preset="medium",
                ),
                "-c:a",
                "aac",
                *(("-t", visual_duration_text) if visual_duration_text is not None else ()),
                "-shortest",
                str(output),
            ]
        )
        request = ProcessRequest(
            executable=self.ffmpeg_path,
            arguments=tuple(arguments),
            working_directory=output.parent,
            timeout_seconds=300,
        )
        result = self.runner.run(request)
        self._require_success(result, "foreground overlay failed")
        return result

    def encode_mask_sequence(
        self,
        mask_pattern: Path,
        output: Path,
        *,
        fps: int | str,
        start_number: int = 0,
        frame_count: int | None = None,
    ) -> ProcessResult:
        try:
            frame_rate = Fraction(str(fps))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("fps must be a positive rational frame rate") from exc
        if frame_rate.numerator <= 0 or frame_rate.denominator <= 0:
            raise ValueError("fps must be a positive rational frame rate")
        if start_number < 0:
            raise ValueError("start_number must be nonnegative")
        if frame_count is not None and frame_count <= 0:
            raise ValueError("frame_count must be positive")
        output.parent.mkdir(parents=True, exist_ok=True)
        arguments: list[str] = [
            "-y",
            "-framerate",
            f"{frame_rate.numerator}/{frame_rate.denominator}",
            "-start_number",
            str(start_number),
            "-i",
            str(mask_pattern),
        ]
        if frame_count is not None:
            arguments.extend(["-frames:v", str(frame_count)])
        arguments.extend(
            [
                "-an",
                "-c:v",
                "ffv1",
                "-level",
                "3",
                "-pix_fmt",
                "gray",
                str(output),
            ]
        )
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=tuple(arguments),
                working_directory=output.parent,
                timeout_seconds=900,
            )
        )
        self._require_success(result, "mask sequence encoding failed")
        return result

    def extract_video_frame_range(
        self,
        source: Path,
        output: Path,
        *,
        start_frame: int,
        end_frame: int,
    ) -> ProcessResult:
        """Extract a half-open decoded video frame range without audio."""

        if start_frame < 0 or end_frame <= start_frame:
            raise ValueError("video frame range must be nonempty and half-open")
        output.parent.mkdir(parents=True, exist_ok=True)
        frame_count = end_frame - start_frame
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-i",
                    str(source.resolve()),
                    "-map",
                    "0:v:0",
                    "-vf",
                    f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS",
                    "-frames:v",
                    str(frame_count),
                    "-an",
                    "-c:v",
                    "ffv1",
                    "-level",
                    "3",
                    "-pix_fmt",
                    "yuv444p10le",
                    str(output.resolve()),
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=1800,
            )
        )
        self._require_success(result, "video frame-range extraction failed")
        return result

    def transcode_mask_lossless(self, source: Path, output: Path) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-i",
                    str(source.resolve()),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    "format=gray",
                    "-c:v",
                    "ffv1",
                    "-level",
                    "3",
                    "-pix_fmt",
                    "gray",
                    str(output.resolve()),
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=900,
            )
        )
        self._require_success(result, "lossless mask transcode failed")
        return result

    def make_contact_sheet(
        self,
        source: Path,
        output: Path,
        frame_indices: Sequence[int],
        *,
        scale_width: int = 320,
        tile_columns: int | None = None,
        filter_prefix: str | None = None,
        input_start_number: int | None = None,
    ) -> ProcessResult:
        if not frame_indices or any(index < 0 for index in frame_indices):
            raise ValueError("contact sheet requires nonnegative frame indices")
        if scale_width <= 0:
            raise ValueError("contact sheet scale width must be positive")
        columns = tile_columns or len(frame_indices)
        if columns <= 0 or columns > len(frame_indices):
            raise ValueError("contact sheet tile columns must fit the selected frames")
        rows = math.ceil(len(frame_indices) / columns)
        input_offsets = (
            [index - input_start_number for index in frame_indices]
            if input_start_number is not None
            else list(frame_indices)
        )
        if any(index < 0 for index in input_offsets):
            raise ValueError("contact sheet frame indices precede the input sequence")
        selection = "+".join(f"eq(n\\,{index})" for index in input_offsets)
        filters = [f"select='{selection}'"]
        if filter_prefix:
            filters.append(filter_prefix)
        filters.extend([f"scale={scale_width}:-1", f"tile={columns}x{rows}"])
        output.parent.mkdir(parents=True, exist_ok=True)
        input_arguments = ["-y"]
        if input_start_number is not None:
            input_arguments.extend(["-start_number", str(input_start_number)])
        input_arguments.extend(["-i", str(source.resolve())])
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=tuple(
                    [
                        *input_arguments,
                        "-vf",
                        ",".join(filters),
                        "-frames:v",
                        "1",
                        "-an",
                        "-update",
                        "1",
                        str(output.resolve()),
                    ]
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=900,
            )
        )
        self._require_success(result, "contact sheet generation failed")
        return result

    def attach_alpha(
        self,
        foreground: Path,
        alpha: Path,
        output: Path,
    ) -> ProcessResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        graph = "[1:v]format=gray[alpha];[0:v][alpha]alphamerge,format=yuva444p10le[outv]"
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-i",
                    str(foreground),
                    "-i",
                    str(alpha),
                    "-filter_complex",
                    graph,
                    "-map",
                    "[outv]",
                    "-an",
                    "-c:v",
                    "prores_ks",
                    "-profile:v",
                    "4",
                    "-pix_fmt",
                    "yuva444p10le",
                    "-shortest",
                    str(output),
                ),
                working_directory=output.parent,
                timeout_seconds=1800,
            )
        )
        self._require_success(result, "alpha attachment failed")
        return result

    def render_contrasting_background(
        self,
        foreground: Path,
        alpha: Path,
        output: Path,
        *,
        color: str,
    ) -> ProcessResult:
        """Render a matte over a deterministic solid background for review.

        The foreground and grayscale alpha remain separate inputs so this
        preview cannot accidentally consume an attached-alpha output with
        ambiguous polarity. The preview is review evidence, not approval.
        """

        if color not in {"black", "white"}:
            raise ValueError("contrast preview color must be black or white")
        foreground_probe = self.probe(foreground)
        stream = next(
            (
                value
                for value in foreground_probe.get("streams", [])
                if isinstance(value, dict) and value.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(stream, dict):
            raise ValueError("contrast preview foreground has no video stream")
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError("contrast preview foreground dimensions are invalid")
        rate_value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        try:
            rate = Fraction(str(rate_value))
        except (ValueError, ZeroDivisionError):
            raise ValueError("contrast preview foreground frame rate is invalid") from None
        if rate.numerator <= 0 or rate.denominator <= 0:
            raise ValueError("contrast preview foreground frame rate is invalid")
        output.parent.mkdir(parents=True, exist_ok=True)
        pixel_format = self._video_pixel_format(self.video_codec)
        graph = (
            "[0:v]format=rgb24[fg];"
            "[1:v]format=gray[alpha];"
            "[fg][alpha]alphamerge[rgba];"
            f"[2:v][rgba]overlay=shortest=1:format=auto,format={pixel_format}[outv]"
        )
        result = self.runner.run(
            ProcessRequest(
                executable=self.ffmpeg_path,
                arguments=(
                    "-y",
                    "-hide_banner",
                    "-i",
                    str(foreground.resolve()),
                    "-i",
                    str(alpha.resolve()),
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s={width}x{height}:r={rate.numerator}/{rate.denominator}",
                    "-filter_complex",
                    graph,
                    "-map",
                    "[outv]",
                    "-an",
                    *self._video_encode_arguments(
                        self.video_codec,
                        crf=18,
                        preset="medium",
                    ),
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(output.resolve()),
                ),
                working_directory=output.parent.resolve(),
                timeout_seconds=1800,
            )
        )
        self._require_success(result, "contrasting-background preview failed")
        return result

    @staticmethod
    def _require_success(result: ProcessResult, message: str) -> None:
        if result.exit_code != 0:
            detail = result.stderr.strip()[-2000:]
            raise RuntimeError(f"{message}: {detail}")
