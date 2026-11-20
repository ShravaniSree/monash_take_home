"""Structured log lines stay valid JSON and keep the request id."""

import json
import logging

from ap_agent.utils.logging import JsonFormatter
from ap_agent.utils.logging import get_logger
from ap_agent.utils.logging import log_event
from ap_agent.utils.logging import request_id


def test_log_line_is_one_json_object():
    record = logging.LogRecord(
        name="ap_agent.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=json.dumps({"event": "tool_call", "tool": "get_vendor_record", "success": True}),
        args=(),
        exc_info=None,
    )
    body = json.loads(JsonFormatter().format(record))
    assert body["level"] == "INFO"
    assert body["logger"] == "ap_agent.test"
    assert body["message"]["event"] == "tool_call"
    assert body["message"]["success"] is True


def test_request_id_is_added_while_the_api_call_is_active():
    logger = get_logger("ap_agent.test.correlation")
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger.addHandler(_Capture())
    token = request_id.set("req-1")
    try:
        log_event(logger, "decision_made", recommendation="APPROVE_FOR_POSTING")
    finally:
        request_id.reset(token)
    payload = json.loads(captured[-1])
    assert payload["request_id"] == "req-1"
    assert payload["recommendation"] == "APPROVE_FOR_POSTING"
