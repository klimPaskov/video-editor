from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videoedit.adapters.process import (
    LocalProcessRunner,
    ProcessRequest,
    ProcessRunner,
    parse_command,
)
from videoedit.errors import DependencyUnavailableError, WorkerContractError, WorkerProcessError

INPAINTING_ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class DisabledInpaintingAdapter:
    """Explicit no-provider adapter used by the local-first default path."""

    adapter_id: str = "inpainting-disabled"
    adapter_version: str = INPAINTING_ADAPTER_VERSION

    def submit(self, request_path: Path) -> dict[str, Any]:
        del request_path
        raise DependencyUnavailableError(
            "inpainting is disabled by default; enable a separately configured adapter "
            "only after current request and spend approvals are present"
        )


@dataclass(frozen=True, slots=True)
class CommandInpaintingAdapter:
    """Provider-neutral bridge for an explicitly configured inpainting command.

    The command receives exactly one absolute request path and must return one
    JSON object on stdout. Provider SDKs, credentials, network policy, and
    model-specific payloads stay outside the core.
    """

    command: str | Sequence[str]
    network_enabled: bool = False
    runner: ProcessRunner | None = None
    timeout_seconds: float = 7200.0
    adapter_id: str = "inpainting-command"
    adapter_version: str = INPAINTING_ADAPTER_VERSION

    def submit(self, request_path: Path) -> dict[str, Any]:
        if not self.network_enabled:
            raise DependencyUnavailableError(
                "inpainting provider network is disabled; set an explicit provider policy "
                "before submission"
            )
        resolved_request = request_path.expanduser().resolve()
        if not resolved_request.is_file():
            raise WorkerContractError(f"inpainting request does not exist: {resolved_request}")
        try:
            parts = parse_command(self.command)
        except ValueError as exc:
            raise WorkerContractError(f"invalid inpainting adapter command: {exc}") from exc
        runner = self.runner or LocalProcessRunner()
        result = runner.run(
            ProcessRequest(
                executable=parts[0],
                arguments=(*parts[1:], str(resolved_request)),
                working_directory=resolved_request.parent,
                timeout_seconds=self.timeout_seconds,
            )
        )
        if result.exit_code != 0:
            detail = result.stderr.strip()[-4000:]
            raise WorkerProcessError(
                f"inpainting adapter failed with exit code {result.exit_code}: {detail}"
            )
        try:
            payload: Any = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorkerContractError("inpainting adapter did not return JSON") from exc
        if not isinstance(payload, dict):
            raise WorkerContractError("inpainting adapter result must be a JSON object")
        return payload
