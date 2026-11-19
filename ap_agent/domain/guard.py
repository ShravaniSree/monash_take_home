"""Detect instruction-like supplier text. Detection is not the control."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "BYPASS_REQUEST",
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior)\s+(policies|instructions)"
            r"|skip\s+duplicate"
            r"|do\s+not\s+ask\s+a\s+human"
            r"|call\s+the\s+payment\s+tool"
            r"|mark\s+this\s+document\s+as\s+verified"
            r"|system\s+prompt"
            r"|you\s+are\s+now",
            re.IGNORECASE,
        ),
    ),
    (
        "BANK_CHANGE_LANGUAGE",
        re.compile(
            r"bank\s+account\s+has\s+changed|new\s+account\s+ending|bank\s+details",
            re.IGNORECASE,
        ),
    ),
    (
        "URGENT_OR_SECRET",
        re.compile(r"\burgent(?:ly|cy)?\b|\basap\b|\bsecret\b|\bimmediately\b", re.IGNORECASE),
    ),
]


def scan(text: str) -> list[str]:
    """Find instruction-like phrases in one piece of supplier text.

    A match is evidence. It does not change the recommendation by itself.

    Args:
        text: Notes or attachment text. Empty text matches nothing.

    Returns:
        Indicator names in pattern order, with duplicates removed.
    """
    if not text:
        return []
    found: list[str] = []
    for name, pattern in _PATTERNS:
        if pattern.search(text) and name not in found:
            found.append(name)
    return found


def scan_many(parts: list[str]) -> list[str]:
    """Scan several text parts and merge the indicators.

    Args:
        parts: Notes and attachment texts, in case order.

    Returns:
        Indicator names, first occurrence first.
    """
    found: list[str] = []
    for part in parts:
        for name in scan(part):
            if name not in found:
                found.append(name)
    return found
