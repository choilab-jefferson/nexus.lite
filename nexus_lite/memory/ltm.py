"""
nexus_lite.memory.ltm — LTM: markdown wiki under <ws>/wiki with SQLite FTS5 search.

LTM = authoritative durable record (nexus wording).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .._db import connect, get_workspace, init


def _wiki_path(slug: str) -> tuple[str, Path]:
    """Resolve a wiki slug to its file, refusing absolute paths and anything escaping <workspace>/wiki."""
    raw = str(slug).strip()
    if not raw or raw.startswith(("/", "\\")) or "\x00" in raw:
        raise ValueError(f"Invalid wiki slug: {slug!r}")
    clean_slug = raw.strip("/")
    wiki_root = (get_workspace() / "wiki").resolve()
    p = (wiki_root / f"{clean_slug}.md").resolve()
    if wiki_root != p and wiki_root not in p.parents:
        raise ValueError(f"Wiki slug escapes the wiki directory: {slug!r}")
    return clean_slug, p


def wiki_write(slug: str, content: str) -> Path:
    """Store markdown document in .nexus/wiki/<slug>.md and index in SQLite FTS5."""
    init()
    clean_slug, p = _wiki_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    with connect() as con:
        con.execute("DELETE FROM wiki_fts WHERE slug = ?", (clean_slug,))
        con.execute("INSERT INTO wiki_fts (slug, content) VALUES (?, ?)", (clean_slug, content))

    return p


def wiki_read(slug: str) -> str:
    """Read markdown document from .nexus/wiki/<slug>.md."""
    init()
    clean_slug, p = _wiki_path(slug)
    if not p.exists():
        raise FileNotFoundError(f"Wiki article '{clean_slug}' not found at {p}")
    return p.read_text(encoding="utf-8")


def wiki_search(query: str) -> list[dict]:
    """Search wiki documents using SQLite FTS5 full-text search with snippets."""
    init()
    with connect() as con:
        try:
            cur = con.execute(
                """
                SELECT slug, snippet(wiki_fts, 1, '<b>', '</b>', '...', 10) as snippet
                FROM wiki_fts
                WHERE wiki_fts MATCH ?
                ORDER BY rank;
                """,
                (query,),
            )
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            cur = con.execute(
                """
                SELECT slug, content as snippet
                FROM wiki_fts
                WHERE content LIKE ? OR slug LIKE ?;
                """,
                (f"%{query}%", f"%{query}%"),
            )
            return [dict(r) for r in cur.fetchall()]


def wiki_list() -> list[dict]:
    """List all wiki documents."""
    init()
    ws = get_workspace()
    wiki_dir = ws / "wiki"
    docs: list[dict] = []
    if wiki_dir.exists():
        for p in wiki_dir.rglob("*.md"):
            rel = p.relative_to(wiki_dir)
            slug = str(rel)[:-3]
            stat = p.stat()
            docs.append({
                "slug": slug,
                "path": str(p),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    docs.sort(key=lambda d: d["slug"])
    return docs
