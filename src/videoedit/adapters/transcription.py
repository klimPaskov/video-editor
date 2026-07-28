from __future__ import annotations

import errno
import hashlib
import importlib
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from videoedit.errors import DependencyUnavailableError, TranscriptionOutputError

WHISPER_TRANSIENT_ERRNOS = frozenset(
    value
    for value in (
        errno.EACCES,
        errno.EBUSY,
        errno.EINVAL,
        errno.EPERM,
        getattr(errno, "ETXTBSY", None),
    )
    if value is not None
)
WHISPER_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
WHISPER_TRANSIENT_ATTEMPTS = 2
WHISPER_TRANSIENT_DELAY_SECONDS = 0.05


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    raw_result: dict[str, Any]
    model_identifier: str
    device: str
    adapter_id: str
    adapter_version: str
    model_sha256: str | None = None


class TranscriptionAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def device(self) -> str: ...

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult: ...


@lru_cache(maxsize=4)
def _load_whisper_model(model_path: str, device: str) -> Any:
    """Load one local Whisper checkpoint once per process and device.

    Join QA transcribes many short previews with the same immutable checkpoint.
    Re-loading the model for every preview is both needlessly expensive and
    prone to transient Windows file/allocator errors; the cache remains keyed
    by the resolved checkpoint path and device so a different operator-supplied
    model cannot be mistaken for the current one.
    """

    whisper = importlib.import_module("whisper")
    return whisper.load_model(model_path, device=device)


def _is_transient_whisper_os_error(error: OSError) -> bool:
    return (
        error.errno in WHISPER_TRANSIENT_ERRNOS
        or getattr(error, "winerror", None) in WHISPER_TRANSIENT_WINERRORS
    )


@dataclass(frozen=True, slots=True)
class WhisperAdapter:
    """Local OpenAI Whisper adapter with network-free model loading by default."""

    model_path: Path | None = None
    device: str = "cpu"
    adapter_id: str = "openai-whisper-local"
    adapter_version: str = "unknown"

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult:
        resolved_audio = audio_path.expanduser().resolve()
        if not resolved_audio.is_file():
            raise TranscriptionOutputError(f"speech proxy does not exist: {resolved_audio}")
        if self.model_path is None:
            raise DependencyUnavailableError(
                "Local Whisper requires an operator-supplied local model_path; "
                "network model downloads are disabled"
            )
        resolved_model = self.model_path.expanduser().resolve()
        if not resolved_model.is_file():
            raise DependencyUnavailableError(
                f"local Whisper model does not exist: {resolved_model}"
            )
        model_sha256 = _sha256_file(resolved_model)
        try:
            whisper = importlib.import_module("whisper")
        except ImportError as exc:
            raise DependencyUnavailableError(
                "Local Whisper is not installed. Install the optional whisper extra "
                "in the active Python 3.11 environment."
            ) from exc

        try:
            for attempt in range(WHISPER_TRANSIENT_ATTEMPTS):
                try:
                    model = _load_whisper_model(str(resolved_model), self.device)
                    raw_result = model.transcribe(
                        str(resolved_audio),
                        word_timestamps=True,
                        verbose=False,
                    )
                    break
                except OSError as exc:
                    if (
                        not _is_transient_whisper_os_error(exc)
                        or attempt + 1 >= WHISPER_TRANSIENT_ATTEMPTS
                    ):
                        raise
                    time.sleep(WHISPER_TRANSIENT_DELAY_SECONDS)
        except Exception as exc:
            raise TranscriptionOutputError(f"local Whisper transcription failed: {exc}") from exc
        if not isinstance(raw_result, dict):
            raise TranscriptionOutputError("local Whisper returned a non-object result")
        model_device = str(getattr(model, "device", self.device))
        module_version = str(getattr(whisper, "__version__", self.adapter_version))
        return TranscriptionResult(
            raw_result=json.loads(json.dumps(raw_result)),
            model_identifier=f"whisper-{model_name}",
            device=model_device,
            adapter_id=self.adapter_id,
            adapter_version=module_version,
            model_sha256=model_sha256,
        )


@dataclass(frozen=True, slots=True)
class FixtureTranscriptionAdapter:
    """Deterministic local adapter used for contract and pipeline tests."""

    fixture_result: dict[str, Any]
    device: str = "cpu"
    adapter_id: str = "fixture-transcription"
    adapter_version: str = "1.0.0"
    model_path: Path | None = None

    def transcribe(self, audio_path: Path, model_name: str) -> TranscriptionResult:
        if not audio_path.is_file():
            raise TranscriptionOutputError(f"fixture audio input does not exist: {audio_path}")
        try:
            copied_result = json.loads(json.dumps(self.fixture_result))
        except (TypeError, ValueError) as exc:
            raise TranscriptionOutputError(
                "fixture transcription result is not JSON-serializable"
            ) from exc
        return TranscriptionResult(
            raw_result=copied_result,
            model_identifier=f"fixture-{model_name}",
            device=self.device,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )
