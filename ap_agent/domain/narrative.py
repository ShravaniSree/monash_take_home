"""Validate the narrative. A bad completion cannot change the outcome."""

from __future__ import annotations

import os
import re

_CODES = (
    "APPROVE_FOR_POSTING",
    "HOLD_FOR_INFORMATION",
    "REJECT_DUPLICATE",
    "REJECT_INVALID",
    "ESCALATE_CONTROL_REVIEW",
)
_CHUNK_ID = re.compile(r"[A-Z]+(?:-[A-Z0-9]+)+#[A-Za-z0-9-]+#\d+")
_AMOUNT = re.compile(r"\d+\.\d{2}")


def provider() -> str:
    """Return the configured model provider.

    ``fake`` is the default so tests and a local review never call Gemini.

    Returns:
        Lower-case provider name from ``LLM_PROVIDER``.
    """
    return os.environ.get("LLM_PROVIDER", "fake").strip().lower() or "fake"


def model_name() -> str:
    """Return the configured model name.

    Returns:
        ``ADK_MODEL`` or the default Gemini flash model.
    """
    return os.environ.get("ADK_MODEL", "gemini-2.5-flash")


def template_narrative(recommendation: str, citation_ids: list[str]) -> str:
    """Build the narrative used when the model output cannot be trusted.

    Args:
        recommendation: Outcome already chosen by the rules engine.
        citation_ids: Chunk ids that retrieval actually returned.

    Returns:
        Text that repeats the engine outcome and those citations only.
    """
    cited = ", ".join(citation_ids) if citation_ids else "none"
    return (
        f"Recommendation {recommendation} is unchanged. "
        f"Citations: {cited}. Supplier text was not followed."
    )


def narrative_errors(
    text: str,
    *,
    recommendation: str,
    citation_ids: set[str],
    allowed_numbers: set[str],
) -> list[str]:
    """List ways a completion disagrees with the engine snapshot.

    Args:
        text: Model completion.
        recommendation: Outcome the engine already stored.
        citation_ids: Chunk ids retrieved for this case.
        allowed_numbers: Amounts that appear on the request or the calculations.

    Returns:
        Error strings. An empty list means the completion may be kept.
    """
    errors: list[str] = []
    for code in _CODES:
        if code != recommendation and code in text:
            errors.append(f"narrative changes the recommendation to {code}")
    unknown = set(_CHUNK_ID.findall(text)) - citation_ids
    if unknown:
        errors.append("narrative cites a chunk that was not retrieved")
    for number in _AMOUNT.findall(text):
        if number not in allowed_numbers:
            errors.append(f"narrative invents the amount {number}")
    return errors


def resolve_narrative(
    completions: list[str],
    *,
    recommendation: str,
    citation_ids: list[str],
    allowed_numbers: set[str],
) -> tuple[str, bool]:
    """Accept a completion, repair it once, or replace it with the template.

    Args:
        completions: Model outputs in the order they were produced.
            The second item is the single repair attempt.
        recommendation: Engine outcome. This function never returns a
            different one.
        citation_ids: Retrieved chunk ids.
        allowed_numbers: Amounts the narrative is allowed to repeat.

    Returns:
        The text to store, and whether the model output was rejected.
        An empty completion list uses the template and is not a rejection.
    """
    if not completions:
        return template_narrative(recommendation, citation_ids), False
    allowed = set(citation_ids)
    for text in completions[:2]:
        if not narrative_errors(
            text,
            recommendation=recommendation,
            citation_ids=allowed,
            allowed_numbers=allowed_numbers,
        ):
            return text, False
    return template_narrative(recommendation, citation_ids), True
