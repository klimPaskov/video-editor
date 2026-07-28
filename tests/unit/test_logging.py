from __future__ import annotations

import json
import logging
from io import StringIO

from videoedit.logging import configure_logging, log_event


def test_structured_logging_redacts_message_and_fields() -> None:
    stream = StringIO()
    configure_logging("INFO", secrets=("secret-token",), stream=stream)
    log_event(
        logging.getLogger("videoedit.test"),
        "worker_started",
        "using secret-token",
        project_id="demo",
        stage="segmentation",
        command="secret-token",
    )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "worker_started"
    assert payload["project_id"] == "demo"
    assert payload["message"] == "using [REDACTED]"
    assert "secret-token" not in stream.getvalue()
