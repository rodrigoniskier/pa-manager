from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord(None, 0, "", 0, "", (), None).__dict__)
_BLOCKED_KEYS = {"password", "token", "api_token", "authorization", "cookie", "secret"}


def _safe_extra(record: logging.LogRecord) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED or key.startswith("_"):
            continue
        if any(blocked in key.lower() for blocked in _BLOCKED_KEYS):
            extra[key] = "[REDACTED]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            extra[key] = value
        else:
            extra[key] = str(value)
    return extra


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_safe_extra(record))
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
