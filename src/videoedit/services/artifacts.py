from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from videoedit import __version__
from videoedit.services.project import ProjectLayout, sha256_file

ZERO_SHA256 = "0" * 64
ATOMIC_REPLACE_ATTEMPTS = 4
ATOMIC_REPLACE_DELAY_SECONDS = 0.05


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def config_sha256(layout: ProjectLayout) -> str:
    path = layout.config / "project.yaml"
    return sha256_file(path) if path.is_file() else ZERO_SHA256


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def producer(stage: str, adapter: str, adapter_version: str = "1") -> dict[str, str]:
    return {
        "application_version": __version__,
        "stage": stage,
        "adapter": adapter,
        "adapter_version": adapter_version,
    }


def artifact_input(artifact_id: str, path: Path) -> dict[str, str]:
    return {"artifact_id": artifact_id, "sha256": sha256_file(path)}


def validate_artifact(package_root: Path, schema_name: str, payload: dict[str, Any]) -> None:
    schema_path = package_root / "schemas" / f"{schema_name}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        formatted = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "root"
            formatted.append(f"{location}: {error.message}")
        raise ValueError(f"Artifact failed {schema_name} validation: {'; '.join(formatted)}")


def _replace_atomically_with_retry(source: str, destination: Path) -> None:
    """Promote a staged file despite short Windows sharing violations.

    Antivirus scanners and media probes can briefly hold a just-written artifact
    on Windows. Retry only permission-style failures; all other errors and a
    persistent permission failure remain visible to the caller.
    """

    for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= ATOMIC_REPLACE_ATTEMPTS:
                raise
            time.sleep(ATOMIC_REPLACE_DELAY_SECONDS)


def write_validated_artifact(
    package_root: Path,
    schema_name: str,
    path: Path,
    payload: dict[str, Any],
) -> Path:
    validate_artifact(package_root, schema_name, payload)
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
        _replace_atomically_with_retry(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return path


def write_text_atomically(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_atomically_with_retry(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return path
