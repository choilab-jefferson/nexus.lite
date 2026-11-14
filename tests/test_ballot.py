"""nexus-lite ballot: agreement among agents done by code (open, cast, status; result announced by code)."""

import hashlib
import json

import pytest

from nexus_lite import _db
from nexus_lite import ballot as B
from nexus_lite.memory import mtm

TEAM = ["lead", "analyst", "writer", "stats"]


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    _db.reset_init()
    return w


def inbox(name):
    return [m for m in mtm.bus_read(target=name, since_seq=0, limit=1000) if m["target"] == name]


def results(name):
    return [json.loads(m["content"]) for m in inbox(name) if m["kind"] == "ballot-result"]


# --- open ---------------------------------------------------------------------------------

def test_open_notifies_every_voter_and_hashes_attachments(tmp_path):
    (tmp_path / "report.md").write_text("final report\n")
    B.open_ballot("b1", "adopt report.md?", TEAM, opener="lead", attach=["report.md"])
    for v in TEAM:
        assert [m["kind"] for m in inbox(v)] == ["ballot"]
        assert "nexus-lite ballot cast b1 yes" in inbox(v)[0]["content"]
    st = B.status("b1")
    assert st["question"] == "adopt report.md?" and st["voters"] == TEAM and st["decided"] is None
    assert st["attach"] == [{"path": "report.md", "sha256": hashlib.sha256(b"final report\n").hexdigest()}]


def test_open_refuses_a_duplicate_id_unknown_rule_or_bad_tie_break():
    B.open_ballot("b1", "q", TEAM, opener="lead")
    with pytest.raises(ValueError):
        B.open_ballot("b1", "q again", TEAM, opener="lead")
    with pytest.raises(ValueError):
        B.open_ballot("b2", "q", TEAM, opener="lead", rule="unanimous")
    with pytest.raises(ValueError):
        B.open_ballot("b3", "q", TEAM, opener="lead", tie_break="nobody")


def test_open_refuses_a_missing_attachment():
    with pytest.raises(FileNotFoundError):
        B.open_ballot("b1", "q", TEAM, opener="lead", attach=["missing.md"])


# --- cast and the majority rule -----------------------------------------------------------

def test_majority_yes_decides_and_announces_once():
    B.open_ballot("b1", "q", TEAM, opener="lead", notify=["harness"])
    B.cast("b1", "lead", "yes")
    B.cast("b1", "analyst", "yes")
    assert B.status("b1")["decided"] is None
    st = B.cast("b1", "writer", "yes")
    assert st["decided"] == "yes" and st["yes"] == 3 and st["missing"] == ["stats"]
    for target in ("lead", "harness"):
        assert [r["decided"] for r in results(target)] == ["yes"]
    with pytest.raises(ValueError):            # decided: no more votes
        B.cast("b1", "stats", "no")
    assert len(results("harness")) == 1


def test_no_decides_as_soon_as_yes_cannot_reach_a_majority():
    B.open_ballot("b1", "q", TEAM, opener="lead")
    B.cast("b1", "analyst", "no")
    st = B.cast("b1", "writer", "no")          # 4 voters: yes can reach at most 2, not > 2
    assert st["decided"] == "no"


def test_tie_break_waits_for_everyone_then_follows_the_tie_breaker():
    B.open_ballot("b1", "q", TEAM, opener="lead", tie_break="lead")
    B.cast("b1", "analyst", "no")
    B.cast("b1", "writer", "no")
    assert B.status("b1")["decided"] is None   # a 2-2 tie is still possible
    B.cast("b1", "stats", "yes")
    st = B.cast("b1", "lead", "yes")
    assert st["decided"] == "yes" and st["tie_broken_by"] == "lead"


def test_a_later_vote_replaces_an_earlier_one():
    B.open_ballot("b1", "q", TEAM, opener="lead")
    B.cast("b1", "analyst", "no")
    B.cast("b1", "analyst", "yes")
    assert B.status("b1")["votes"] == {"analyst": "yes"}


@pytest.mark.parametrize("bad", [("nobody", "yes"), ("analyst", "maybe")])
def test_cast_refuses_a_non_voter_or_a_choice_other_than_yes_no(bad):
    B.open_ballot("b1", "q", TEAM, opener="lead")
    with pytest.raises(ValueError):
        B.cast("b1", *bad)


def test_cast_refuses_an_unknown_ballot():
    with pytest.raises(KeyError):
        B.cast("nope", "lead", "yes")


def test_a_quoted_or_capitalised_choice_is_read_as_yes_no():
    B.open_ballot("b1", "q", TEAM, opener="lead")
    B.cast("b1", "analyst", '"Yes"')
    assert B.status("b1")["votes"] == {"analyst": "yes"}


# --- the record is the bus; status is recomputed and validated --------------------------

def test_status_ignores_hand_written_records_from_non_voters_or_with_bad_choices():
    B.open_ballot("b1", "q", TEAM, opener="lead")
    mtm.bus_send("ballot:b1", json.dumps({"vote": "yes"}), sender="intruder", kind="ballot-cast")
    mtm.bus_send("ballot:b1", json.dumps({"vote": "maybe"}), sender="analyst", kind="ballot-cast")
    mtm.bus_send("ballot:b1", "not json", sender="writer", kind="ballot-cast")
    assert B.status("b1")["votes"] == {}


def test_status_of_an_unknown_ballot_is_a_key_error():
    with pytest.raises(KeyError):
        B.status("nope")


# --- CLI: what a member runs (voter = NEXUS_AGENT) ------------------------------------------

def _cli(ws, tmp_path, agent, *args):
    import subprocess
    import sys
    from pathlib import Path
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "NEXUS_LITE_DIR": str(ws),
           "NEXUS_LITE_SOCK_DIR": str(tmp_path / "s"), "NEXUS_AGENT": agent,
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    return subprocess.run([sys.executable, "-m", "nexus_lite", "ballot", *args], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)


def test_cli_open_cast_status_and_result(ws, tmp_path):
    (tmp_path / "r.md").write_text("r\n")
    p = _cli(ws, tmp_path, "lead", "open", "b1", "--question", "adopt r.md?", "--voters", "lead,analyst,writer",
             "--attach", "r.md", "--notify", "harness")
    assert p.returncode == 0, p.stderr
    assert _cli(ws, tmp_path, "analyst", "cast", "b1", "yes").returncode == 0
    p = _cli(ws, tmp_path, "writer", "status", "b1")
    assert p.returncode == 0 and "undecided" in p.stdout and "yes=1" in p.stdout
    p = _cli(ws, tmp_path, "writer", "cast", "b1", "yes")
    assert p.returncode == 0 and "decided: yes" in p.stdout
    _db.reset_init()
    assert [r["decided"] for r in results("harness")] == ["yes"]
    p = _cli(ws, tmp_path, "lead", "status", "b1", "--json")
    assert json.loads(p.stdout)["decided"] == "yes"


def test_cli_refusals_exit_2_with_a_reason(ws, tmp_path):
    _cli(ws, tmp_path, "lead", "open", "b1", "--question", "q", "--voters", "lead,analyst")
    p = _cli(ws, tmp_path, "intruder", "cast", "b1", "yes")
    assert p.returncode == 2 and "not a voter" in p.stderr
    p = _cli(ws, tmp_path, "analyst", "cast", "nope", "yes")
    assert p.returncode == 2 and "no ballot" in p.stderr


def test_cli_a_member_cannot_vote_or_open_under_another_name(ws, tmp_path):
    """Inside a member NEXUS_AGENT is its identity: --as may only repeat it (run 11: nothing stopped one member from
    casting as another, and the count cannot tell)."""
    _cli(ws, tmp_path, "lead", "open", "b1", "--question", "q", "--voters", "lead,analyst,writer")
    p = _cli(ws, tmp_path, "analyst", "cast", "b1", "yes", "--as", "writer")
    assert p.returncode == 2 and "NEXUS_AGENT" in p.stderr
    assert _cli(ws, tmp_path, "analyst", "status", "b1", "--json").stdout.count('"writer": "yes"') == 0
    p = _cli(ws, tmp_path, "analyst", "open", "b2", "--question", "q", "--voters", "lead,analyst", "--as", "lead")
    assert p.returncode == 2 and "NEXUS_AGENT" in p.stderr
    assert _cli(ws, tmp_path, "analyst", "cast", "b1", "yes", "--as", "analyst").returncode == 0   # same name: fine


def test_cli_as_still_names_the_voter_outside_a_member(ws, tmp_path):
    _cli(ws, tmp_path, "lead", "open", "b1", "--question", "q", "--voters", "lead,analyst")
    p = _cli(ws, tmp_path, "", "cast", "b1", "yes", "--as", "analyst")     # a person or script, no NEXUS_AGENT
    assert p.returncode == 0, p.stderr
