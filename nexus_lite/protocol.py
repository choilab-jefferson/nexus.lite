"""
nexus_lite.protocol — Shared wire/bus pointer protocol primitives for the Open-Core Kernel.

Format of a PTY pointer line:
    [nexus] <kind> <id> <path> - <summary>
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Mapping, Optional

POINTER_MAX_CHARS = 300

_FORBIDDEN_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def sanitize_pointer_token(val: str) -> str:
    """Strip forbidden control characters and replace whitespace runs with '_', returning '-' if empty."""
    cleaned = _FORBIDDEN_RE.sub("", str(val if val is not None else "")).strip()
    return _WS_RE.sub("_", cleaned) or "-"


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_bus_field(name: str, val: object) -> str:
    """Typing guard + control-char/path-separator rejection for bus ids/targets/senders."""
    if not isinstance(val, str):
        raise TypeError(f"{name} must be str, got {type(val).__name__}")
    if not val or _CTRL_RE.search(val) or "/" in val or "\\" in val or val in (".", ".."):
        raise ValueError(f"invalid {name}: empty, control char or path separator")
    return val


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


def parse_handoffs(text: str) -> list[tuple[str, str]]:
    """Extract `[nexus] handoff <target> - <summary>` lines from task output as (target, summary)."""
    out: list[tuple[str, str]] = []
    if not isinstance(text, str):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("[nexus] handoff "):
            continue
        rest = line[len("[nexus] handoff ") :]
        if " - " in rest:
            target, summary = rest.split(" - ", 1)
        elif rest.endswith(" -"):
            target, summary = rest[:-2], ""
        else:
            continue
        out.append((target.strip(), summary.strip()))
    return out


class EdgeKind(str, Enum):
    """Closed set of agent-to-agent link kinds for orchestration (who relates to whom, and how).

    A general agent-link primitive: any consumer that queries agent-to-agent relations can use it
    (Office lines, a research viewer, handoff lineage). It carries no presentation.
    """

    DM = "dm"
    HANDOFF = "handoff"
    BROADCAST = "broadcast"
    TASK = "task"
    RECEIPT = "receipt"
    SPAWN = "spawn"


# Research-only message kinds: not agent links. Any kind outside EdgeKind is also not a link.
NON_LINK_KINDS = frozenset({"gp_spawn", "stage_handoff"})

_RECEIPT_PREFIXES = ("TASK_COMPLETE", "TASK_FAIL", "TASK_ESCALATE")


def _row(row: Any) -> Mapping[str, Any]:
    """View a dict or sqlite3.Row as a mapping; anything else is empty."""
    if isinstance(row, Mapping):
        return row
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return {}


def _edge(src: Any, dst: Any, kind: EdgeKind, ts: Any) -> Optional[dict[str, Any]]:
    if not (isinstance(src, str) and src and isinstance(dst, str) and dst):
        return None
    return {"from": src, "to": dst, "kind": kind.value, "ts": ts}


def normalize_edge(msg_row: Any) -> Optional[dict[str, Any]]:
    """Project a `messages` row to an agent link ``{from, to, kind, ts}``, or None if it is not one.

    Not a link (returns None): kinds in NON_LINK_KINDS, any kind outside EdgeKind, a literal
    `spawn` message kind (spawn links come only from tasks), or a missing sender/target.
    Only those four keys are ever emitted, so summary/content/path cannot leak.
    """
    r = _row(msg_row)
    kind = r.get("kind")
    if not isinstance(kind, str) or kind in NON_LINK_KINDS or kind == EdgeKind.SPAWN.value:
        return None
    try:
        ek = EdgeKind(kind)
    except ValueError:
        return None
    if ek is EdgeKind.TASK and str(r.get("summary") or "").startswith(_RECEIPT_PREFIXES):
        ek = EdgeKind.RECEIPT
    elif ek is EdgeKind.DM and r.get("target") == "ALL":
        ek = EdgeKind.BROADCAST
    return _edge(r.get("sender"), r.get("target"), ek, r.get("timestamp"))


def normalize_spawn(task_row: Any) -> Optional[dict[str, Any]]:
    """Project a `tasks` row to a spawn link ``{from: caller, to: task id, kind: "spawn", ts}`` or None."""
    r = _row(task_row)
    return _edge(r.get("caller"), r.get("id"), EdgeKind.SPAWN, r.get("created_at"))
