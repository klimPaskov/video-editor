from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

STRUCTURED_FIELDS = (
    "event",
    "project_id",
    "revision_id",
    "stage",
    "run_id",
    "artifact_id",
    "command",
    "elapsed_ms",
    "exit_code",
    "retry_count",
    "redacted",
)


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    return value


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(value for value in secrets if value)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.getMessage(), self._secrets)
        record.args = ()
        for key, value in tuple(record.__dict__.items()):
            if key not in {"msg", "args"}:
                setattr(record, key, _redact_value(value, self._secrets))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def configure_logging(
    level: str,
    secrets: Iterable[str] = (),
    stream: TextIO | None = None,
) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    logger.log(level, message, extra={"event": event, **fields})
