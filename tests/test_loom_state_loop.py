import os

from nexus_lite import _db
from nexus_lite.loom import LoomResult, LoomStateLoop


class FakeAdapter:
    def __init__(self, ok_at):
        self.ok_at, self.calls, self.ctx = ok_at, 0, []

    def run_prompt(self, goal, extra_args=None, **kw):
        self.calls += 1
        a = extra_args
        with open(a[a.index("--context-in") + 1]) as f:
            self.ctx.append(f.read())
        if self.calls not in getattr(self, "skip_out", ()):
            with open(a[a.index("--context-out") + 1], "w") as f:
                f.write(f"out{self.calls}")
        ok = self.calls >= self.ok_at
        return LoomResult(0 if ok else 1, "ok" if ok else "task_failure", ok=ok, retryable=not ok, stdout="{}")


def test_loop_stops_on_verify_and_context_is_append_only():
    ad = FakeAdapter(ok_at=3)
    loop = LoomStateLoop(ad, run_id="r", system="SYS", pack="PACK", checkpoint=False)
    recs = loop.run("g", verify=lambda r: True)
    assert [r.verified for r in recs] == [False, False, True]
    # earlier ledger content is a stable prefix of later contexts
    pre = ad.ctx[1].split("[Current Step Goal")[0]
    assert ad.ctx[2].startswith(pre.rstrip("\n").rsplit("\n", 1)[0])
    assert ad.ctx[2].index("[Frozen Role") < ad.ctx[2].index("[Append-Only") < ad.ctx[2].index("[Current Step")
    assert "step 1:" in ad.ctx[1] and "out1" in ad.ctx[1]


def test_max_steps_enforced(monkeypatch):
    monkeypatch.setenv("NEXUS_MAX_STEPS", "2")
    ad = FakeAdapter(ok_at=99)
    assert len(LoomStateLoop(ad, run_id="r", checkpoint=False).run("g", verify=lambda r: True)) == 2


def test_context_bounded():
    loop = LoomStateLoop(FakeAdapter(1), run_id="r", max_context_chars=300, checkpoint=False)
    loop.ledger = ["x" * 100] * 20
    assert len(loop.render_context("g")) <= 400


def test_checkpoint_to_db(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(_db, "get_db_path", lambda: tmp_path / "lite.db")
    LoomStateLoop(FakeAdapter(1), run_id="rid").run("g", verify=lambda r: True)
    with _db.connect() as con:
        rows = con.execute("SELECT run_id, step, verified FROM loom_state_steps").fetchall()
    assert [tuple(r) for r in rows] == [("rid", 1, 1)]


def test_missing_context_out_is_empty_not_stale():
    ad = FakeAdapter(ok_at=2)
    ad.skip_out = {2}
    recs = LoomStateLoop(ad, run_id="r", checkpoint=False).run("g", verify=lambda r: True)
    assert recs[0].output == "out1"
    assert recs[1].output == ""
