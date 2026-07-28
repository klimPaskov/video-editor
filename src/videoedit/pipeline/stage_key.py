from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def make_stage_key(
    stage: str,
    implementation_version: str,
    input_hashes: Sequence[str],
    configuration: Mapping[str, Any],
) -> str:
    payload = {
        "stage": stage,
        "implementation_version": implementation_version,
        "input_hashes": sorted(input_hashes),
        "configuration": configuration,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
