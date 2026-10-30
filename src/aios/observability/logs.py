"""Structured JSON logs with run and trace ids, and secret redaction (WP 1.6; FR-OBS-09)."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRETS = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|(?i:api[_-]?key|token|password|secret)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{6,})"
)
_FIELDS = ("run_id", "agent", "step", "trace_id", "tenant", "app")


def redact(text: str) -> str:
    """Mask API keys, bearer tokens and ``key=value`` secrets."""
    return _SECRETS.sub("[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for name in _FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                entry[name] = value
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str = "info", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
