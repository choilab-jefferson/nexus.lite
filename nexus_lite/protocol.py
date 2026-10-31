"""
nexus_lite.protocol — Shared wire/bus pointer protocol primitives for the Open-Core Kernel.

Format of a PTY pointer line:
    [nexus] <kind> <id> <path> - <summary>
"""

from __future__ import annotations

import re
from typing import Optional

POINTER_MAX_CHARS = 300

_FORBIDDEN_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def sanitize_pointer_token(val: str) -> str:
    """Strip forbidden control characters and replace whitespace runs with '_', returning '-' if empty."""
    cleaned = _FORBIDDEN_RE.sub("", str(val if val is not None else "")).strip()
    return _WS_RE.sub("_", cleaned) or "-"


def truncate_words(text: str, limit: int) -> str:
    """Truncate `text` to `limit` chars at a word boundary if possible, appending '…'."""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    cut = text[: limit - 1]
    if not text[limit - 1].isspace() and " " in cut:
        word_cut = cut.rsplit(" ", 1)[0].rstrip()
        if word_cut:
            cut = word_cut
    else:
        cut = cut.rstrip()
    return cut + "…"


def format_pointer_line(
    kind: str,
    msg_id: str,
    path: str,
    summary: str,
    limit: int = POINTER_MAX_CHARS,
) -> str:
    """Build a single-line `[nexus] <kind> <id> <path> - <summary>` PTY pointer string."""
    p = _FORBIDDEN_RE.sub("", str(path if path is not None else "")).strip() or "-"
    head = f"[nexus] {sanitize_pointer_token(kind)} {sanitize_pointer_token(msg_id)} {p} - "
    one = _WS_RE.sub(" ", _FORBIDDEN_RE.sub(" ", str(summary or ""))).strip()
    room = max(0, limit - len(head))
    if len(one) > room:
        one = truncate_words(one, room)
    return head + one


def parse_pointer_line(text: str) -> Optional[dict[str, str]]:
    """Parse `[nexus] <kind> <id> <path> - <summary>` into a dict or return None if malformed."""
    if not isinstance(text, str):
        return None
    line = text.strip()
    if not line.startswith("[nexus] "):
        return None
    rest = line[len("[nexus] ") :]
    if " - " in rest:
        left, summary = rest.split(" - ", 1)
    elif rest.endswith(" -"):
        left, summary = rest[:-2], ""
    else:
        return None
    parts = left.split(" ", 2)
    if len(parts) != 3:
        return None
    kind, msg_id, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not kind or not msg_id or not path:
        return None
    return {
        "kind": kind,
        "id": msg_id,
        "path": path,
        "summary": summary.strip(),
    }


def pointer_parts(text: str) -> tuple[str, str]:
    """Parse `(msg_id, path)` from `[nexus] <kind> <id> <path> - <summary>`, or `('', '')` if malformed."""
    parsed = parse_pointer_line(text)
    if not parsed:
        return ("", "")
    return (parsed["id"], parsed["path"])
