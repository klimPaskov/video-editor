from __future__ import annotations

import pytest

from videoedit.cli import deliver
from videoedit.errors import ApprovalRequiredError


def test_legacy_deliver_command_cannot_bypass_gate3() -> None:
    with pytest.raises(ApprovalRequiredError, match="current Gate 3 approval"):
        deliver("legacy_project")
