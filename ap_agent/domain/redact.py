"""Mask account-like numbers before they reach logs or audit rows."""

from __future__ import annotations

import re
from typing import Any

from ap_agent.domain.models import ProcessingRequest

_DIGITS = re.compile(r"\d{6,18}")


def redact(value: str) -> str:
    """Mask digit runs that look like account numbers.

    Args:
        value: Text that may contain an account or card number.

    Returns:
        The same text with each 6 to 18 digit run replaced by ``****``
        and its last four digits.
    """
    def _mask(match: re.Match[str]) -> str:
        """Keep the last four digits of one matched run."""
        digits = match.group(0)
        return f"****{digits[-4:]}"

    return _DIGITS.sub(_mask, value)


def redact_data(value: Any) -> Any:
    """Redact account-like numbers inside nested JSON values.

    Args:
        value: A string, list, or object that may be written to an event.

    Returns:
        The same structure with digit runs masked in every string.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return value


def redact_request(request: ProcessingRequest) -> ProcessingRequest:
    """Return a copy of the case whose notes and attachments are masked.

    The rules engine still sees instruction-like phrases. Only long digit
    runs, such as an account number pasted into a note, are masked.

    Args:
        request: Invoice case as the caller submitted it.

    Returns:
        A request safe to store on the session and in the audit log.
    """
    justification = request.non_po_justification
    if justification is not None:
        justification = justification.model_copy(
            update={
                "reason": redact(justification.reason),
                "cost_centre": redact(justification.cost_centre),
                "receipt_evidence_ref": redact(justification.receipt_evidence_ref),
                "retrospective_review_ref": (
                    None
                    if justification.retrospective_review_ref is None
                    else redact(justification.retrospective_review_ref)
                ),
            }
        )
    return request.model_copy(
        update={
            "notes": redact(request.notes),
            "attachments": [
                item.model_copy(update={"text": redact(item.text)}) for item in request.attachments
            ],
            "non_po_justification": justification,
        }
    )
