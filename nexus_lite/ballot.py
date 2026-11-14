"""
nexus_lite.ballot — agreement among agents done by code.

Agents decide (open a ballot, what it asks, how they vote); code does the procedure: recording, counting, the rule,
hashing attached files and announcing the result. Records live on the bus under target `ballot:<ID>`:
one `ballot-open` (the definition), `ballot-cast` per vote, and a `ballot-result` once decided. `status()` recomputes
everything from those records and validates them, so a hand-written record from a non-voter does not count.

Rule `majority` (the only rule): decided yes when yes > n/2; decided no when yes cannot reach a majority even if every
remaining voter says yes. With a tie-breaker, a possible tie waits for everyone; a full tie follows the tie-breaker.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from ._db import get_workspace
from .artifacts import mkdir_private
from .memory import mtm
from .protocol import validate_bus_field

RULES = ("majority",)
CHOICES = ("yes", "no")
_LIMIT = 100_000


def _target(ballot_id: str) -> str:
    validate_bus_field("ballot id", ballot_id)
    return f"ballot:{ballot_id}"


def normalize_choice(choice: str) -> str:
    """'"Yes"', "no." -> 'yes', 'no'; anything else is returned as is (and refused by the caller)."""
    return str(choice).strip().strip("\"'`").rstrip(".!").strip("\"'`").strip().lower()


def _records(ballot_id: str) -> List[dict]:
    t = _target(ballot_id)
    return [m for m in mtm.bus_read(target=t, since_seq=0, limit=_LIMIT) if m["target"] == t]


def _decide(votes: dict, voters: Sequence[str], tie_break: Optional[str]):
    n, yes = len(voters), sum(v == "yes" for v in votes.values())
    remaining = n - len(votes)
    if 2 * yes > n:
        return "yes", None
    if 2 * (yes + remaining) > n:
        return None, None
    if tie_break and 2 * (yes + remaining) == n:  # a tie is still possible: wait for everyone
        return (votes.get(tie_break), tie_break) if remaining == 0 else (None, None)
    return "no", None


def status(ballot_id: str) -> dict:
    """The ballot as the bus records it: definition, valid votes up to the decision, tally and decision."""
    recs = _records(ballot_id)
    opened = next((m for m in recs if m["kind"] == "ballot-open"), None)
    if opened is None:
        raise KeyError(f"no ballot {ballot_id!r}")
    d = json.loads(opened["content"])
    voters, tie_break = list(d["voters"]), d.get("tie_break")
    votes: dict = {}
    decided, tie_by = None, None
    for m in recs:
        if decided is not None:
            break
        if m["kind"] != "ballot-cast" or m["seq"] < opened["seq"] or m["sender"] not in voters:
            continue
        try:
            v = normalize_choice(json.loads(m["content"]).get("vote", ""))
        except (ValueError, AttributeError):
            continue
        if v in CHOICES:
            votes[m["sender"]] = v
            decided, tie_by = _decide(votes, voters, tie_break)
    return {"id": ballot_id, "question": d["question"], "voters": voters, "rule": d["rule"], "tie_break": tie_break,
            "attach": d.get("attach", []), "opener": opened["sender"], "notify": d.get("notify", []),
            "votes": votes, "yes": sum(v == "yes" for v in votes.values()),
            "no": sum(v == "no" for v in votes.values()), "missing": [v for v in voters if v not in votes],
            "decided": decided, "tie_broken_by": tie_by}


@contextlib.contextmanager
def _lock(ballot_id: str) -> Iterator[None]:
    d = mkdir_private(get_workspace() / "ballots")
    fd = os.open(str(d / f"{_target(ballot_id).replace(':', '_')}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def open_ballot(ballot_id: str, question: str, voters: Sequence[str], *, opener: str, rule: str = "majority",
                tie_break: Optional[str] = None, attach: Sequence[str] = (), notify: Sequence[str] = ()) -> dict:
    """Open a ballot and notify every voter. Attached files are hashed now (sha256, paths as given)."""
    voters = list(dict.fromkeys(voters))
    for name in (opener, *voters, *notify):
        validate_bus_field("name", name)
    if not voters:
        raise ValueError("a ballot needs voters")
    if rule not in RULES:
        raise ValueError(f"rule must be one of {RULES}")
    if tie_break is not None and tie_break not in voters:
        raise ValueError("the tie-breaker must be a voter")
    if not str(question).strip():
        raise ValueError("a ballot needs a question")
    files = [{"path": p, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in attach]
    with _lock(ballot_id):
        if any(m["kind"] == "ballot-open" for m in _records(ballot_id)):
            raise ValueError(f"ballot {ballot_id!r} already exists")
        definition = {"question": question, "voters": voters, "rule": rule, "tie_break": tie_break,
                      "attach": files, "notify": list(notify)}
        mtm.bus_send(_target(ballot_id), json.dumps(definition), sender=opener, kind="ballot-open")
    lines = [f"Ballot {ballot_id} from {opener}: {question}",
             *(f"attached: {f['path']} sha256={f['sha256']}" for f in files),
             f"Vote with: nexus-lite ballot cast {ballot_id} yes   (or: no)",
             f"See the count with: nexus-lite ballot status {ballot_id}"]
    for v in voters:
        mtm.bus_send(v, "\n".join(lines), sender=opener, kind="ballot")
    return status(ballot_id)


def cast(ballot_id: str, voter: str, choice: str) -> dict:
    """Record `voter`'s yes/no. When this vote decides the ballot, the result is announced by code (once)."""
    v = normalize_choice(choice)
    with _lock(ballot_id):
        st = status(ballot_id)
        if st["decided"] is not None:
            raise ValueError(f"ballot {ballot_id!r} is already decided: {st['decided']}")
        if voter not in st["voters"]:
            raise ValueError(f"{voter!r} is not a voter of ballot {ballot_id!r}")
        if v not in CHOICES:
            raise ValueError(f"a vote is yes or no, got {choice!r}")
        mtm.bus_send(_target(ballot_id), json.dumps({"vote": v}), sender=voter, kind="ballot-cast")
        st = status(ballot_id)
        if st["decided"] is not None:
            result = json.dumps({"ballot": ballot_id, **{k: st[k] for k in (
                "question", "decided", "votes", "yes", "no", "voters", "rule", "tie_break", "tie_broken_by",
                "attach", "opener")}})
            mtm.bus_send(_target(ballot_id), result, sender=voter, kind="ballot-result")
            for t in dict.fromkeys([st["opener"], *st["notify"]]):
                mtm.bus_send(t, result, sender=voter, kind="ballot-result")
    return st


def _summary(st: dict) -> str:
    state = f"decided: {st['decided']}" if st["decided"] else "undecided"
    lines = [f"ballot {st['id']} ({state}) yes={st['yes']} no={st['no']} missing={','.join(st['missing']) or '-'}",
             f"question: {st['question']}",
             f"votes: {', '.join(f'{k}={v}' for k, v in st['votes'].items()) or '-'}"]
    if st["tie_broken_by"]:
        lines.append(f"tie broken by {st['tie_broken_by']}")
    lines += [f"attached: {a['path']} sha256={a['sha256']}" for a in st["attach"]]
    return "\n".join(lines)


def cli_main(argv: Sequence[str]) -> int:
    """`nexus-lite ballot open|cast|status`; argv excludes the leading 'ballot'. Refusals exit 2 with a reason."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="nexus-lite ballot", description=(
        "Agreement among agents done by code: open a ballot, cast yes/no, read the count. Recording, counting, the "
        "rule, file hashes and the result announcement are code. Voter/opener = $NEXUS_AGENT; --as NAME only where "
        "NEXUS_AGENT is unset (a person or script), or equal to it."))
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open", help="open a ballot and notify every voter")
    o.add_argument("id")
    o.add_argument("--question", required=True)
    o.add_argument("--voters", required=True, help="comma-separated names")
    o.add_argument("--rule", default="majority", choices=RULES)
    o.add_argument("--tie-break", default=None, metavar="NAME", help="a voter who breaks a full tie")
    o.add_argument("--attach", action="append", default=[], metavar="PATH", help="file hashed now (repeatable)")
    o.add_argument("--notify", action="append", default=[], metavar="TARGET", help="also told the result (repeatable)")
    c = sub.add_parser("cast", help="vote yes or no")
    c.add_argument("id")
    c.add_argument("choice")
    s = sub.add_parser("status", help="the count, recomputed from the bus")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    for sp in (o, c):
        sp.add_argument("--as", dest="who", default=None, metavar="NAME")
    a = p.parse_args(list(argv))
    agent = os.environ.get("NEXUS_AGENT") or None
    who = getattr(a, "who", None) or agent
    try:
        if agent and who != agent:  # inside a member its identity is NEXUS_AGENT: no voting under another name
            raise ValueError(f"--as {who} differs from NEXUS_AGENT={agent}; a member acts only as itself")
        if a.cmd in ("open", "cast") and not who:
            raise ValueError("who is voting? set NEXUS_AGENT or pass --as NAME")
        if a.cmd == "open":
            st = open_ballot(a.id, a.question, [v.strip() for v in a.voters.split(",") if v.strip()], opener=who,
                             rule=a.rule, tie_break=a.tie_break, attach=a.attach, notify=a.notify)
        elif a.cmd == "cast":
            st = cast(a.id, who, a.choice)
        else:
            st = status(a.id)
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"nexus-lite ballot: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    print(json.dumps(st) if getattr(a, "json", False) else _summary(st))
    return 0
