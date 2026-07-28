from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from videoedit.services.artifacts import validate_artifact


def write_phase_result(
    package_root: Path,
    path: Path,
    payload: dict[str, Any],
) -> Path:
    """Validate and atomically promote one phase result artifact."""

    validate_artifact(package_root, "codex_phase_result", payload)
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return destination
