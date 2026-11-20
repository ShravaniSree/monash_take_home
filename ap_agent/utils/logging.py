"""One JSON object per log line, for audit and debugging."""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id: ContextVar[str] = ContextVar("request_id", default="")


class JsonFormatter(logging.Formatter):
    """Write timestamp, level, logger name, and the event payload as one object."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize one log record.

        Args:
            record: Record whose message is either JSON or plain text.

        Returns:
            A single JSON line.
        """
        raw = record.getMessage()
        try:
            message: Any = json.loads(raw)
        except json.JSONDecodeError:
            message = raw
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            },
            default=str,
        )


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes JSON lines and does not add a second handler.

    Args:
        name: Logger name, usually the module.

    Returns:
        The configured logger.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_ap_json", False):
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    logger._ap_json = True  # type: ignore[attr-defined]
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log one event. The current request id is included when the API set one.

    Args:
        logger: Destination logger.
        event: Stable event name.
        fields: Extra values. Nested objects are stringified.
    """
    payload = {"event": event, **fields}
    current = request_id.get()
    if current:
        payload["request_id"] = current
    logger.info(json.dumps(payload, default=str))
