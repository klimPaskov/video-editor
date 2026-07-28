from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings. Project behavior belongs in project artifacts."""

    model_config = SettingsConfigDict(
        env_prefix="VIDEOEDIT_",
        env_file=".env",
        extra="ignore",
    )

    log_level: str = "INFO"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    video_codec: Literal["libx264", "h264_amf"] = "libx264"
    video_bitrate_bps: int = Field(default=4_000_000, ge=250_000, le=100_000_000)
    node_path: str = "node"
    npm_path: str = "npm"
    remotion_directory: Path = Path("remotion")
    whisper_model: str = "small"
    whisper_model_path: Path | None = None
    max_local_workers: int = Field(default=1, ge=1, le=64)
    provider_network_enabled: bool = False
    inpainting_provider_command: str = ""
    sam3_worker_command: str = ""
    matanyone_worker_command: str = ""
    workspace: Path = Path.cwd()
