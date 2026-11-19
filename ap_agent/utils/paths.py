"""Locations of the corpus, fixtures, and local runtime data."""

from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """Return the repository root that contains ``fixtures`` and the corpus.

    Returns:
        Absolute path of the project directory.
    """
    return Path(__file__).resolve().parents[2]


def corpus_dir() -> Path:
    """Return the directory of markdown policy documents.

    The shipped zip nests the corpus one level down. A flat checkout is
    accepted as well.

    Returns:
        Directory whose ``*.md`` files are ingested into the policy index.
    """
    nested = project_root() / "finance_rag_corpus" / "finance_rag_corpus"
    if nested.is_dir():
        return nested
    return project_root() / "finance_rag_corpus"


def scenarios_dir() -> Path:
    """Return the directory of case request fixtures.

    Returns:
        Path of ``fixtures/scenarios``.
    """
    return project_root() / "fixtures" / "scenarios"


def tools_dir() -> Path:
    """Return the directory of per-tool mock tables.

    Returns:
        Path of ``fixtures/tools``.
    """
    return project_root() / "fixtures" / "tools"
