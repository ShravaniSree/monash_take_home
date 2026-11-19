"""Ingest the finance corpus and retrieve ranked chunks.

FTS5 supplies the lexical score. Trust and staleness are applied in code so
superseded and adversarial documents stay visible and cannot outrank current policy
by accident.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from ap_agent.domain.models import DocType, RetrievedChunk, Trust

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_ALLOWED = {"internal", "external-unverified"}
_SYNONYMS = {
    "grn": "goods receipt",
    "goods received note": "goods receipt",
}


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """Split YAML front matter from the markdown body.

    Args:
        raw: Full file text.

    Returns:
        Metadata pairs and the body. Files without front matter return
        an empty mapping and the original text.
    """
    match = _FRONT_MATTER.match(raw.lstrip("\ufeff"))
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, match.group(2)


def _slug(text: str) -> str:
    """Turn a heading into a chunk-id fragment.

    Args:
        text: Section heading.

    Returns:
        Lowercase letters, digits, and hyphens.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _doc_type(status: str, tags: str) -> DocType:
    """Classify a document from its status and tags.

    Args:
        status: Front-matter status.
        tags: Front-matter tags.

    Returns:
        Policy, adversarial, or irrelevant.
    """
    lowered = tags.lower()
    if "adversarial" in lowered or "prompt-injection" in lowered:
        return DocType.ADVERSARIAL
    if "irrelevant" in lowered:
        return DocType.IRRELEVANT
    if status == "untrusted":
        return DocType.ADVERSARIAL
    return DocType.POLICY


def _trust(status: str, doc_type: DocType) -> Trust:
    """Mark policy as trusted and everything else as untrusted.

    Args:
        status: Front-matter status.
        doc_type: Classification from ``_doc_type``.

    Returns:
        Trust flag stored on the retrieved chunk.
    """
    if status == "untrusted" or doc_type is not DocType.POLICY:
        return Trust.UNTRUSTED
    return Trust.TRUSTED


class PolicyIndex:
    """SQLite FTS index of the finance corpus."""

    def __init__(self, path: Path) -> None:
        """Open or create the index database.

        Args:
            path: SQLite file. Parent directories are created.
        """
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._create()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._db.close()

    def _create(self) -> None:
        """Create the document, chunk, and FTS tables when they are missing."""
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                classification TEXT NOT NULL,
                tags TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                title TEXT NOT NULL,
                version TEXT NOT NULL,
                section TEXT NOT NULL,
                status TEXT NOT NULL,
                classification TEXT NOT NULL,
                tags TEXT NOT NULL,
                body TEXT NOT NULL,
                ordinal INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                title,
                section,
                body,
                tokenize = 'porter'
            );
            """
        )

    def ingest_dir(self, corpus: Path) -> int:
        """Ingest every markdown file under a corpus directory.

        Args:
            corpus: Directory of policy documents. macOS resource files
                are skipped.

        Returns:
            Number of files visited.
        """
        count = 0
        for path in sorted(corpus.rglob("*.md")):
            if "__MACOSX" in path.parts or path.name.startswith("._"):
                continue
            self.ingest_file(path)
            count += 1
        self._db.commit()
        return count

    def ingest_file(self, path: Path) -> None:
        """Replace one document's chunks when its content hash changed.

        Args:
            path: Markdown file with optional YAML front matter.
        """
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_front_matter(raw)
        document_id = meta.get("document_id", path.stem)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        existing = self._db.execute(
            "SELECT content_hash FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if existing and existing["content_hash"] == digest:
            return
        self._db.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        self._db.execute(
            "DELETE FROM chunks_fts WHERE chunk_id LIKE ?",
            (f"{document_id}#%",),
        )
        title = meta.get("title", document_id)
        version = meta.get("version", "0")
        status = meta.get("status", "current")
        classification = meta.get("classification", "internal")
        tags = meta.get("tags", "")
        sections = list(_HEADING.finditer(body))
        pieces: list[tuple[str, str]] = []
        if not sections:
            pieces.append(("Document", body.strip()))
        else:
            for index, heading in enumerate(sections):
                start = heading.end()
                end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
                pieces.append((heading.group(1).strip(), body[start:end].strip()))
        for ordinal, (section, text) in enumerate(pieces):
            chunk_id = f"{document_id}#{_slug(section)}#{ordinal}"
            self._db.execute(
                """
                INSERT INTO chunks (
                    chunk_id, document_id, title, version, section, status,
                    classification, tags, body, ordinal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    document_id,
                    title,
                    version,
                    section,
                    status,
                    classification,
                    tags,
                    text,
                    ordinal,
                ),
            )
            self._db.execute(
                "INSERT INTO chunks_fts (chunk_id, title, section, body) VALUES (?, ?, ?, ?)",
                (chunk_id, title, section, text),
            )
        self._db.execute(
            """
            INSERT INTO documents (
                document_id, title, version, status, classification, tags, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                title = excluded.title,
                version = excluded.version,
                status = excluded.status,
                classification = excluded.classification,
                tags = excluded.tags,
                content_hash = excluded.content_hash
            """,
            (document_id, title, version, status, classification, tags, digest),
        )

    def current_chunk(self, document_id: str) -> RetrievedChunk | None:
        """Return the first current chunk of a policy, for citations.

        Args:
            document_id: Policy id such as ``FIN-POL-003``.

        Returns:
            The first current chunk, or ``None`` when the document is
            missing or has no current text.
        """
        row = self._db.execute(
            """
            SELECT * FROM chunks
            WHERE document_id = ? AND status = 'current'
              AND classification IN ('internal', 'external-unverified')
            ORDER BY ordinal
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        return self._to_chunk(row, score=1.0)

    def retrieve_for_case(
        self,
        query: str,
        *,
        role: str = "ap_processor",
        topics: list[str] | None = None,
        intents: list[dict[str, object]] | None = None,
        top_k: int = 12,
    ) -> list[RetrievedChunk]:
        """Return the best chunk of every document this role may read.

        The document list comes from the index. Adding a markdown file to
        the corpus includes it on the next call. No document id is special.

        Args:
            query: Lexical query built from the case, not from supplier text.
            role: Reader role. ``ap_processor`` cannot see restricted text.
            topics: Tag bonuses applied after the lexical score.
            intents: Optional case-derived policy queries. When provided,
                only eligible current policy chunks are returned.
            top_k: Maximum number of chunks returned for intent retrieval.

        Returns:
            Eligible current policy chunks for supplied intents, or the
            legacy broad result when intents are omitted.
        """
        if intents is not None:
            ranked: dict[str, tuple[RetrievedChunk, float]] = {}
            for intent in intents:
                intent_query = str(intent.get("query", "")).strip()
                if not intent_query:
                    continue
                intent_topics = [str(value) for value in intent.get("topics", [])]
                candidates = self.search(
                    intent_query,
                    role=role,
                    top_k=6,
                    topics=intent_topics,
                    current_policy_only=True,
                )
                for rank, chunk in enumerate(candidates, start=1):
                    combined = 1.0 / (60 + rank)
                    prior = ranked.get(chunk.chunk_id)
                    if prior:
                        combined += prior[1]
                    chunk.score = round(combined, 6)
                    ranked[chunk.chunk_id] = (chunk, combined)
            ordered = sorted(ranked.values(), key=lambda pair: pair[1], reverse=True)
            return [chunk for chunk, _score in ordered[:top_k]]

        found: list[RetrievedChunk] = []
        for document_id in self._document_ids(role):
            chunk = self._best_for_document(document_id, query, role=role, topics=topics)
            if chunk is not None:
                found.append(chunk)
        found.sort(key=lambda item: item.score, reverse=True)
        return found

    def _document_ids(self, role: str) -> list[str]:
        """List document ids this role is allowed to read.

        Args:
            role: Reader role.

        Returns:
            Document ids in the index. Restricted documents are omitted
            for ``ap_processor``.
        """
        rows = self._db.execute("SELECT DISTINCT document_id, classification FROM chunks").fetchall()
        visible: list[str] = []
        seen: set[str] = set()
        for row in rows:
            document_id = row["document_id"]
            if document_id in seen:
                continue
            if role == "ap_processor" and row["classification"] not in _ALLOWED:
                continue
            seen.add(document_id)
            visible.append(document_id)
        return visible

    def _best_for_document(
        self,
        document_id: str,
        query: str,
        *,
        role: str,
        topics: list[str] | None,
    ) -> RetrievedChunk | None:
        """Return the highest scoring chunk of one document for a query.

        Args:
            document_id: Document to search inside.
            query: Lexical query.
            role: Reader role. Kept for the same classification check as
                a direct search.
            topics: Tag bonuses.

        Returns:
            The best chunk, or ``None`` when the document does not match.
        """
        expanded = query
        for source, target in _SYNONYMS.items():
            if source in query.lower() and target not in expanded.lower():
                expanded = f"{expanded} {target}"
        match = " OR ".join(re.findall(r"[A-Za-z0-9]{3,}", expanded)) or "policy"
        try:
            rows = self._db.execute(
                """
                SELECT chunks_fts.chunk_id AS chunk_id, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks ON chunks.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ? AND chunks.document_id = ?
                ORDER BY rank
                LIMIT 8
                """,
                (match, document_id),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        topic_set = {topic.lower() for topic in topics or []}
        best: RetrievedChunk | None = None
        for row in rows:
            stored = self._db.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?",
                (row["chunk_id"],),
            ).fetchone()
            if stored is None:
                continue
            if role == "ap_processor" and stored["classification"] not in _ALLOWED:
                continue
            lexical = -float(row["rank"])
            chunk = self._to_chunk(stored, score=lexical)
            bonus = 0.0
            if chunk.status == "current":
                bonus += 0.35
            if chunk.stale:
                bonus -= 0.50
            if chunk.trust is Trust.UNTRUSTED:
                bonus -= 0.40
            if any(topic in stored["tags"].lower() for topic in topic_set):
                bonus += 0.15
            chunk.score = round(lexical + bonus, 4)
            if best is None or chunk.score > best.score:
                best = chunk
        return best

    def search(
        self,
        query: str,
        *,
        role: str = "ap_processor",
        top_k: int = 6,
        topics: list[str] | None = None,
        current_policy_only: bool = False,
    ) -> list[RetrievedChunk]:
        """Rank chunks for a query, then penalise stale and untrusted text.

        Args:
            query: Lexical query. Short tokens are ignored.
            role: Reader role. ``ap_processor`` cannot see classifications
                outside the allowed set.
            top_k: Maximum chunks to return.
            topics: Tag bonuses applied after the lexical score.
            current_policy_only: Apply current/trusted/allowed metadata filters
                in the candidate query, before ranking.

        Returns:
            Chunks ordered by the adjusted score, highest first.
        """
        expanded = query
        for source, target in _SYNONYMS.items():
            if source in query.lower() and target not in expanded.lower():
                expanded = f"{expanded} {target}"
        match = " OR ".join(re.findall(r"[A-Za-z0-9]{3,}", expanded)[:12]) or "policy"
        try:
            rows = self._db.execute(
                """
                SELECT chunks_fts.chunk_id, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks AS eligible ON eligible.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ?
                  AND (? = 0 OR (
                    eligible.status = 'current'
                    AND eligible.classification IN ('internal', 'external-unverified')
                    AND lower(eligible.tags) NOT LIKE '%adversarial%'
                    AND lower(eligible.tags) NOT LIKE '%irrelevant%'
                  ))
                ORDER BY rank
                LIMIT ?
                """,
                (match, int(current_policy_only), 1000 if current_policy_only else 30),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        scored: list[RetrievedChunk] = []
        topic_set = {topic.lower() for topic in topics or []}
        for row in rows:
            stored = self._db.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?",
                (row["chunk_id"],),
            ).fetchone()
            if stored is None:
                continue
            if role == "ap_processor" and stored["classification"] not in _ALLOWED:
                continue
            doc_type = _doc_type(stored["status"], stored["tags"])
            if current_policy_only and (
                stored["status"] != "current"
                or doc_type is not DocType.POLICY
                or _trust(stored["status"], doc_type) is not Trust.TRUSTED
            ):
                continue
            lexical = -float(row["rank"])
            chunk = self._to_chunk(stored, score=lexical)
            bonus = 0.0
            if chunk.status == "current":
                bonus += 0.35
            if chunk.stale:
                bonus -= 0.50
            if chunk.trust is Trust.UNTRUSTED:
                bonus -= 0.40
            tags = stored["tags"].lower()
            if any(topic in tags for topic in topic_set):
                bonus += 0.15
            chunk.score = round(lexical + bonus, 4)
            scored.append(chunk)
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    def _to_chunk(self, row: sqlite3.Row, score: float) -> RetrievedChunk:
        """Map a stored chunk row to the retrieval contract.

        Args:
            row: Row from the ``chunks`` table.
            score: Score to store before any trust adjustment.

        Returns:
            A retrieved chunk, including trust and staleness flags.
        """
        doc_type = _doc_type(row["status"], row["tags"])
        return RetrievedChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            title=row["title"],
            doc_type=doc_type,
            version=row["version"],
            section=row["section"],
            status=row["status"],
            trust=_trust(row["status"], doc_type),
            stale=row["status"] == "superseded",
            classification=row["classification"],
            score=score,
            text=row["body"],
        )
