from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from videoedit.adapters.process import (
    LocalProcessRunner,
    ProcessRequest,
    ProcessRunner,
    parse_command,
)
from videoedit.errors import WorkerContractError, WorkerProcessError


class WorkerAdapter:
    """Invoke one isolated worker through a versioned JSON contract."""

    def __init__(
        self,
        command: str | Sequence[str],
        runner: ProcessRunner | None = None,
        timeout_seconds: float = 7200,
        job_schema: Path | None = None,
        result_schema: Path | None = None,
    ) -> None:
        try:
            parts = parse_command(command)
        except ValueError as exc:
            raise WorkerContractError(f"invalid worker command: {exc}") from exc
        self.executable = parts[0]
        self.prefix_arguments = parts[1:]
        self.runner = runner or LocalProcessRunner()
        self.timeout_seconds = timeout_seconds
        self.job_schema = job_schema.resolve() if job_schema else None
        self.result_schema = result_schema.resolve() if result_schema else None

    def run(self, job_path: Path, result_path: Path | None = None) -> dict[str, object]:
        resolved_job = job_path.resolve()
        if not resolved_job.is_file():
            raise WorkerContractError(f"worker job does not exist: {resolved_job}")
        job = self._read_object(resolved_job, "worker job")
        self._validate_contract(job, self.job_schema, "worker job")
        schema_version = self._required_string(job, "schema_version", "worker job")
        job_id = self._required_string(job, "job_id", "worker job")

        result = self.runner.run(
            ProcessRequest(
                executable=self.executable,
                arguments=(*self.prefix_arguments, str(resolved_job)),
                working_directory=resolved_job.parent,
                timeout_seconds=self.timeout_seconds,
            )
        )
        if result.exit_code != 0:
            detail = result.stderr.strip()[-4000:]
            raise WorkerProcessError(f"worker failed with exit code {result.exit_code}: {detail}")
        payload = self._parse_stdout(result.stdout)
        self._validate_contract(payload, self.result_schema, "worker result")
        result_version = self._required_string(payload, "schema_version", "worker result")
        result_job_id = self._required_string(payload, "job_id", "worker result")
        if result_version != schema_version:
            raise WorkerContractError(
                "worker result schema_version "
                f"{result_version!r} does not match job {schema_version!r}"
            )
        if result_job_id != job_id:
            raise WorkerContractError(
                f"worker result job_id {result_job_id!r} does not match job {job_id!r}"
            )
        if result_path is not None:
            self._write_result_atomically(result_path.resolve(), payload)
        return payload

    @staticmethod
    def _read_object(path: Path, label: str) -> dict[str, object]:
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerContractError(f"could not read {label}: {path}") from exc
        if not isinstance(payload, dict):
            raise WorkerContractError(f"{label} must be a JSON object")
        return payload

    @staticmethod
    def _parse_stdout(stdout: str) -> dict[str, object]:
        try:
            payload: Any = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise WorkerContractError("worker did not return JSON on stdout") from exc
        if not isinstance(payload, dict):
            raise WorkerContractError("worker result must be a JSON object")
        return payload

    @staticmethod
    def _required_string(payload: dict[str, object], field: str, label: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise WorkerContractError(f"{label} field {field!r} must be a non-empty string")
        return value

    @staticmethod
    def _validate_contract(
        payload: dict[str, object], schema_path: Path | None, label: str
    ) -> None:
        if schema_path is None:
            return
        if not schema_path.is_file():
            raise WorkerContractError(f"{label} schema does not exist: {schema_path}")
        try:
            schema: Any = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerContractError(f"could not read {label} schema: {schema_path}") from exc
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            location = ".".join(str(part) for part in errors[0].path) or "root"
            raise WorkerContractError(
                f"{label} failed schema validation at {location}: {errors[0].message}"
            )

    @staticmethod
    def _write_result_atomically(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            finally:
                raise
