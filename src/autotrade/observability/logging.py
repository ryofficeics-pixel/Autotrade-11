"""Structured JSON logging to stderr with URL/secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys

_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_KEYISH = re.compile(r"(?i)(api[-_]?key|token|secret|authorization)=([^&\s]+)")


def redact(text: str) -> str:
    """Strip URLs (which may embed API keys) and key=value secrets from free text."""
    return _KEYISH.sub(r"\1=***", _URL.sub("<url>", text))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("autotrade")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def log_event(
    logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object
) -> None:
    safe = {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}
    logger.log(level, event, extra={"fields": safe})
