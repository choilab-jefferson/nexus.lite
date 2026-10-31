"""
nexus_lite.stm — File-backed Short-Term Memory (`stm/1`) for nexus-lite.

Follows the Nexus System STM principle:
- Files are the Source of Truth: one Markdown file per node at `<stm_root>/stm-<pid>/node/<id>.md`
  with `stm/1` YAML frontmatter (`schema: stm/1`, 21-char Crockford base32 IDs `p...`, `t...`, `s...`).
- SQLite (`lite.db`) is a rebuildable local index over the node files, never the primary store.
- Emits `<nexus_stm>` fenced briefs and JSON envelopes directly from the file-backed `stm/1` nodes.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

_STM_LOCK = threading.RLock()

SCHEMA = "stm/1"
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
BODY_LEN = 20
ID_LEN = 21
TYPES = {
    "p": "project",
    "f": "family",
    "s": "session",
    "a": "agent",
    "t": "task",
    "r": "run",
    "w": "workspace",
    "u": "user",
    "l": "lease",
    "m": "message",
    "g": "repo",
}
STATUSES = ("running", "needs-human", "done", "failed", "cancelled")
SENSITIVITIES = ("standard", "phi")
LEVELS = {"root": 0, "project": 0, "task": 1, "session": 1, "repo": 1}
MAX_SUMMARY = 160
_BODY_SET = frozenset(ALPHABET)


def _encode_bits(n: int) -> str:
    out: list[str] = []
    for _ in range(BODY_LEN):
        out.append(ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def is_valid_id(value: Any, kind: Optional[str] = None) -> bool:
    """Return True if value is a canonical 21-char `<type><20_crockford_base32>` ID."""
    if not isinstance(value, str) or len(value) != ID_LEN:
        return False
    t = value[0]
    if t not in TYPES or (kind is not None and t != kind):
        return False
    return all(c in _BODY_SET for c in value[1:])


def mint_id(kind: str = "t") -> str:
    """Mint a random 21-char Crockford base32 ID of type `kind`."""
    if kind not in TYPES:
        raise ValueError(f"Unsupported STM ID kind: {kind!r}")
    return kind + _encode_bits(secrets.randbits(100))


def derive_id(kind: str, *parts: str) -> str:
    """Deterministically derive a 21-char Crockford base32 ID of type `kind` from `parts`."""
    if kind not in TYPES:
        raise ValueError(f"Unsupported STM ID kind: {kind!r}")
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    bits = int.from_bytes(h.digest()[:13], "big") >> 4  # top 100 bits
    return kind + _encode_bits(bits)


def node_id_for_task(task_id: str) -> str:
    """Return `task_id` if already a valid `t<20>` ID, else deterministically derive one."""
    if is_valid_id(task_id, "t"):
        return task_id
    return derive_id("t", "task", task_id)


def node_id_for_session(session_name: str) -> str:
    """Return `session_name` if already a valid `s<20>` ID, else deterministically derive one."""
    if is_valid_id(session_name, "s"):
        return session_name
    return derive_id("s", "session", session_name)


def resolve_project_id(
    workspace: Union[str, Path],
    *,
    persist: bool = True,
) -> str:
    """
    Resolve the canonical 21-char `p<20>` project ID for `workspace`:
    1. `NEXUS_PROJECT_ID` environment variable
    2. `.git/nexus/project-id` (walking up 5 parents, including git worktree/submodule `gitdir:`)
    3. `<workspace>/.nexus_project_id`
    4. Deterministic `derive_id('p', resolved_workspace_path)` (persisted to `.git/nexus/project-id`
       when `.git` directory exists in `workspace`, and `<workspace>/.nexus_project_id`).
    """
    env_pid = os.environ.get("NEXUS_PROJECT_ID", "").strip()
    if is_valid_id(env_pid, "p"):
        return env_pid

    target_ws = Path(workspace).resolve()
    cur = target_ws
    for _ in range(6):
        dot_git = cur / ".git"
        if dot_git.is_dir():
            pid_file = dot_git / "nexus" / "project-id"
            if pid_file.is_file():
                cand = pid_file.read_text(encoding="utf-8", errors="replace").strip()
                if is_valid_id(cand, "p"):
                    return cand
        elif dot_git.is_file():
            raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if raw.startswith("gitdir:"):
                gd_raw = raw.split("gitdir:", 1)[1].strip()
                gd = Path(gd_raw) if Path(gd_raw).is_absolute() else (cur / gd_raw).resolve()
                for rel in (
                    gd / "nexus" / "project-id",
                    gd.parent / "nexus" / "project-id",
                    gd.parent.parent / "nexus" / "project-id",
                ):
                    if rel.is_file():
                        cand = rel.read_text(encoding="utf-8", errors="replace").strip()
                        if is_valid_id(cand, "p"):
                            return cand
        marker = cur / ".nexus_project_id"
        if marker.is_file():
            cand = marker.read_text(encoding="utf-8", errors="replace").strip()
            if is_valid_id(cand, "p"):
                return cand
        if cur.parent == cur:
            break
        cur = cur.parent

    pid = derive_id("p", "project", str(target_ws))
    if persist and target_ws.is_dir():
        try:
            dot_git = target_ws / ".git"
            if dot_git.is_dir():
                git_nexus = dot_git / "nexus"
                git_nexus.mkdir(parents=True, exist_ok=True)
                (git_nexus / "project-id").write_text(pid + "\n", encoding="utf-8")
            (target_ws / ".nexus_project_id").write_text(pid + "\n", encoding="utf-8")
        except OSError:
            pass
    return pid


def get_stm_root(workspace: Union[str, Path]) -> Path:
    """
    Return the STM root directory for `workspace`:
    - `$NEXUS_STM_DIR` if set
    - `~/.local/share/nexus/stm` if `stm-<pid>` already exists there
    - `<workspace>/stm` otherwise (self-contained workspace-local STM files)
    """
    env_stm = os.environ.get("NEXUS_STM_DIR", "").strip()
    if env_stm:
        return Path(env_stm).expanduser().resolve()

    target_ws = Path(workspace).resolve()
    pid = resolve_project_id(target_ws, persist=False)
    sys_stm = Path.home() / ".local" / "share" / "nexus" / "stm"
    if (sys_stm / f"stm-{pid}" / "node" / f"{pid}.md").is_file():
        return sys_stm
    return target_ws / "stm"


def get_project_stm_dir(workspace: Union[str, Path]) -> Path:
    """Return `<stm_root>/stm-<pid>` for `workspace`."""
    target_ws = Path(workspace).resolve()
    pid = resolve_project_id(target_ws, persist=True)
    return get_stm_root(target_ws) / f"stm-{pid}"


def _yaml_quote(val: str) -> str:
    cleaned = " ".join(str(val).splitlines()).strip()
    return "'" + cleaned.replace("'", "''") + "'"


def _safe_read_text(path: Path) -> str:
    """Read text from `path` with retry on transient FUSE/concurrency OSErrors."""
    for attempt in range(3):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if attempt < 2:
                time.sleep(0.02 * (attempt + 1))
    return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` safely across local POSIX and Google Drive FUSE mounts."""
    with _STM_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}"
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            try:
                path.write_text(content, encoding="utf-8")
            except OSError:
                pass


def dump_node_markdown(doc: dict[str, Any], body: str = "") -> str:
    """Serialize an `stm/1` node dictionary to YAML-frontmatter Markdown."""
    nid = str(doc["id"])
    kind = str(doc.get("kind", "task"))
    level = int(doc.get("level", LEVELS.get(kind, 1)))
    project = doc.get("project")
    parent = doc.get("parent")
    status = str(doc.get("status", "running"))
    if status == "completed":
        status = "done"
    elif status == "escalated":
        status = "needs-human"
    elif status not in STATUSES:
        status = "running"

    summary = " ".join(str(doc.get("summary") or "").splitlines()).strip()[:MAX_SUMMARY]
    summary_src = str(doc.get("summary_src") or "first-line")
    sensitivity = str(doc.get("sensitivity") or "standard")
    if sensitivity not in SENSITIVITIES:
        sensitivity = "standard"
    seq = int(doc.get("seq") or 1)
    updated = str(doc.get("updated") or _iso_now())
    valid_from = doc.get("valid_from") or updated
    invalid_at = doc.get("invalid_at")
    invalidated_by = doc.get("invalidated_by")
    agent = doc.get("agent")
    task_id_orig = doc.get("task_id")

    lines = [
        "---",
        f"schema: {SCHEMA}",
        f"id: {nid}",
        f"kind: {kind}",
        f"level: {level}",
        f"project: {project if project else 'null'}",
        f"parent: {parent if parent else 'null'}",
        f"status: {status}",
    ]
    if agent:
        lines.append(f"agent: {_yaml_quote(str(agent))}")
    if task_id_orig:
        lines.append(f"task_id: {_yaml_quote(str(task_id_orig))}")
    lines.extend(
        [
            f"summary: {_yaml_quote(summary)}",
            f"summary_src: {summary_src}",
            f"sensitivity: {sensitivity}",
            f"seq: {seq}",
            f"updated: {_yaml_quote(updated)}",
            f"valid_from: {_yaml_quote(str(valid_from))}",
            f"invalid_at: {_yaml_quote(str(invalid_at)) if invalid_at else 'null'}",
            f"invalidated_by: {_yaml_quote(str(invalidated_by)) if invalidated_by else 'null'}",
            "links: []",
            "pointers: []",
            "history: []",
            "prov: []",
            "---",
        ]
    )
    out = "\n".join(lines) + "\n"
    if body.strip():
        out += "\n" + body.strip() + "\n"
    return out


def parse_node_markdown(text: str) -> dict[str, Any]:
    """Parse an `stm/1` YAML-frontmatter Markdown file into a dictionary."""
    out: dict[str, Any] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    body_start = len(lines)
    for i in range(1, len(lines)):
        raw = lines[i]
        trimmed = raw.strip()
        if trimmed == "---":
            body_start = i + 1
            break
        if not trimmed or trimmed.startswith("#") or raw.startswith((" ", "\t")):
            continue
        if ":" in raw:
            k, v = raw.split(":", 1)
            key = k.strip()
            val = v.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                inner = val[1:-1]
                if val[0] == "'":
                    inner = inner.replace("''", "'")
                out[key] = inner
            elif val in ("null", "~", ""):
                out[key] = None
            elif val == "[]":
                out[key] = []
            elif val.isdigit():
                out[key] = int(val)
            else:
                out[key] = val
    if body_start < len(lines):
        body = "\n".join(lines[body_start:]).strip()
        if body:
            out["body"] = body
    return out


def _iso_now(ts: Optional[float] = None) -> str:
    t = time.time() if ts is None else ts
    return _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_seq(proj_stm_dir: Path) -> int:
    """Issue the next monotonic sequence number for `proj_stm_dir` (`max(seq_file, disk_hwm) + 1`)."""
    with _STM_LOCK:
        proj_stm_dir.mkdir(parents=True, exist_ok=True)
        seq_file = proj_stm_dir / "seq"
        hwm = 0
        if seq_file.is_file():
            try:
                hwm = int(_safe_read_text(seq_file).strip() or "0")
            except (ValueError, OSError):
                hwm = 0
        node_dir = proj_stm_dir / "node"
        if node_dir.is_dir():
            for p in list(node_dir.glob("*.md")):
                try:
                    raw = _safe_read_text(p)
                    if not raw:
                        continue
                    meta = parse_node_markdown(raw)
                    s = meta.get("seq")
                    if isinstance(s, int) and s > hwm:
                        hwm = s
                except OSError:
                    pass
        nxt = hwm + 1
        _atomic_write_text(seq_file, f"{nxt}\n")
        return nxt


def ensure_project_node(
    workspace: Union[str, Path],
    *,
    sensitivity: Optional[str] = None,
) -> dict[str, Any]:
    """Ensure `<stm_root>/stm-<pid>/node/<pid>.md` exists and return its metadata."""
    with _STM_LOCK:
        target_ws = Path(workspace).resolve()
        pid = resolve_project_id(target_ws, persist=True)
        proj_dir = get_project_stm_dir(target_ws)
        node_dir = proj_dir / "node"
        node_dir.mkdir(parents=True, exist_ok=True)
        root_path = node_dir / f"{pid}.md"

        sens = sensitivity or ("phi" if os.environ.get("NEXUS_SENSITIVITY", "").lower() == "phi" else "standard")
        if root_path.is_file():
            raw = _safe_read_text(root_path)
            if raw:
                existing = parse_node_markdown(raw)
                if existing and (sensitivity is None or existing.get("sensitivity") == sens):
                    return existing

        seq = next_seq(proj_dir)
        doc = {
            "id": pid,
            "kind": "project",
            "level": 0,
            "project": None,
            "status": "running",
            "summary": f"{target_ws.name}: active workspace",
            "summary_src": "template",
            "sensitivity": sens,
            "seq": seq,
            "updated": _iso_now(),
        }
        _atomic_write_text(root_path, dump_node_markdown(doc))
        return doc


def write_node(
    workspace: Union[str, Path],
    *,
    node_id: str,
    kind: str = "task",
    status: str = "running",
    summary: str = "",
    agent: Optional[str] = None,
    task_id: Optional[str] = None,
    sensitivity: Optional[str] = None,
    body: str = "",
    updated_ts: Optional[float] = None,
) -> dict[str, Any]:
    """
    Write or update a file-backed `stm/1` node at `<stm_root>/stm-<pid>/node/<node_id>.md`
    and refresh the L0 project root node headline `<stm_root>/stm-<pid>/node/<pid>.md`.
    """
    with _STM_LOCK:
        target_ws = Path(workspace).resolve()
        pid = resolve_project_id(target_ws, persist=True)
        proj_dir = get_project_stm_dir(target_ws)
        node_dir = proj_dir / "node"
        node_dir.mkdir(parents=True, exist_ok=True)

        ensure_project_node(target_ws, sensitivity=sensitivity)
        seq = next_seq(proj_dir)
        sens = sensitivity or ("phi" if os.environ.get("NEXUS_SENSITIVITY", "").lower() == "phi" else "standard")
        iso_ts = _iso_now(updated_ts)

        node_path = node_dir / f"{node_id}.md"
        valid_from = iso_ts
        if node_path.is_file():
            raw_prev = _safe_read_text(node_path)
            if raw_prev:
                prev = parse_node_markdown(raw_prev)
                if prev.get("valid_from"):
                    valid_from = str(prev["valid_from"])

        doc = {
            "id": node_id,
            "kind": kind,
            "level": LEVELS.get(kind, 1),
            "project": pid,
            "parent": pid,
            "status": status,
            "agent": agent,
            "task_id": task_id,
            "summary": summary,
            "summary_src": "first-line",
            "sensitivity": sens,
            "seq": seq,
            "updated": iso_ts,
            "valid_from": valid_from,
            "invalid_at": None,
        }
        _atomic_write_text(node_path, dump_node_markdown(doc, body=body))

        # Update project L0 summary line
        active_nodes = list_nodes(target_ws)
        n_tasks = sum(1 for n in active_nodes if n.get("kind") == "task")
        n_sess = sum(1 for n in active_nodes if n.get("kind") == "session")
        headline = f"{target_ws.name}: {len(active_nodes)} active ({n_tasks} tasks, {n_sess} sessions), last={node_id}"
        root_path = node_dir / f"{pid}.md"
        root_doc = {
            "id": pid,
            "kind": "project",
            "level": 0,
            "project": None,
            "status": "running",
            "summary": headline[:MAX_SUMMARY],
            "summary_src": "template",
            "sensitivity": sens,
            "seq": seq,
            "updated": iso_ts,
        }
        _atomic_write_text(root_path, dump_node_markdown(root_doc))
        return doc


def read_node(workspace: Union[str, Path], node_id: str) -> Optional[dict[str, Any]]:
    """Read a single `stm/1` node file from `<stm_root>/stm-<pid>/node/<node_id>.md`."""
    with _STM_LOCK:
        proj_dir = get_project_stm_dir(workspace)
        node_path = proj_dir / "node" / f"{node_id}.md"
        if not node_path.is_file():
            return None
        raw = _safe_read_text(node_path)
        if not raw:
            return None
        return parse_node_markdown(raw)


def list_nodes(
    workspace: Union[str, Path],
    *,
    kind: Optional[str] = None,
    include_invalid: bool = False,
) -> list[dict[str, Any]]:
    """
    Scan `<stm_root>/stm-<pid>/node/*.md` directly from disk (Files = Source of Truth).
    Excludes the project L0 root node `<pid>.md` unless `kind == 'project'`.
    Returns nodes sorted by `(seq, id)` descending.
    """
    with _STM_LOCK:
        target_ws = Path(workspace).resolve()
        pid = resolve_project_id(target_ws, persist=False)
        proj_dir = get_project_stm_dir(target_ws)
        node_dir = proj_dir / "node"
        if not node_dir.is_dir():
            return []

        out: list[dict[str, Any]] = []
        for p in list(node_dir.glob("*.md")):
            if p.name == f"{pid}.md" and kind != "project":
                continue
            raw = _safe_read_text(p)
            if not raw:
                continue
            meta = parse_node_markdown(raw)
            if not meta.get("id"):
                continue
            if not include_invalid and meta.get("invalid_at") not in (None, "", "null", "~"):
                continue
            if kind and meta.get("kind") != kind:
                continue
            out.append(meta)

        out.sort(key=lambda d: (int(d.get("seq") or 0), str(d.get("id") or "")), reverse=True)
        return out


def pack_brief(
    workspace: Union[str, Path],
    *,
    phi: bool = False,
    limit: int = 8,
) -> dict[str, Any]:
    """
    Build the structured `stm/1` brief dictionary natively in Python (no external binary required).
    Includes project L0 root summary and up to `limit` most recent active nodes.
    Redacts node summaries when `phi=True` or node sensitivity is `'phi'`.
    """
    target_ws = Path(workspace).resolve()
    pid = resolve_project_id(target_ws, persist=True)
    root_doc = ensure_project_node(target_ws, sensitivity="phi" if phi else None)
    nodes = list_nodes(target_ws, include_invalid=False)[: max(1, int(limit))]
    clamp_phi = bool(phi or root_doc.get("sensitivity") == "phi")

    packed_nodes: list[dict[str, Any]] = []
    for n in nodes:
        node_phi = clamp_phi or (n.get("sensitivity") == "phi")
        summary_val = "" if node_phi else str(n.get("summary") or "")
        packed_nodes.append(
            {
                "id": n.get("id"),
                "kind": n.get("kind", "task"),
                "status": n.get("status", "running"),
                "agent": n.get("agent"),
                "task_id": n.get("task_id"),
                "seq": n.get("seq", 1),
                "updated": n.get("updated"),
                "summary": summary_val,
            }
        )

    agents_md = target_ws / "AGENTS.md"
    agents_md_path = str(agents_md.resolve()) if agents_md.is_file() else ""
    headline = "[REDACTED:PHI]" if clamp_phi else str(root_doc.get("summary") or f"{target_ws.name}: active workspace")
    return {
        "schema": SCHEMA,
        "stm_available": True,
        "project_id": pid,
        "stm_dir": str(get_stm_root(target_ws)),
        "agents_md_path": agents_md_path,
        "sensitivity": "phi" if clamp_phi else str(root_doc.get("sensitivity") or "standard"),
        "headline": headline,
        "node_count": len(packed_nodes),
        "nodes": packed_nodes,
        "recent_nodes": packed_nodes,
    }


def format_fenced_brief(
    workspace: Union[str, Path],
    *,
    phi: bool = False,
    limit: int = 8,
) -> str:
    """Format the `<nexus_stm>` fenced prompt envelope natively in Python."""
    brief = pack_brief(workspace, phi=phi, limit=limit)
    lines = [
        f"<nexus_stm project=\"{brief['project_id']}\" sensitivity=\"{brief['sensitivity']}\">",
        f"project: {brief['project_id']} ({brief['sensitivity']})",
        f"headline: {brief['headline']}",
    ]
    for n in brief["nodes"]:
        ag = f" agent={n['agent']}" if n.get("agent") else ""
        lines.append(f"- [{n['id']}] ({n['kind']}/{n['status']}{ag}): {n['summary']}")
    lines.append("</nexus_stm>")
    return "\n".join(lines)

