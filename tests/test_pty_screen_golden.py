"""Golden tests for nexus_lite.pty.screen: recorded raw terminal output of agent CLIs rendered identically to pyte.

Fixtures (tests/fixtures/pty/<name>.bin + <name>.expected.json, see tests/fixtures/pty/README.md) are real raw
terminal streams of claude, agy, codex, copilot, gemini, opencode and a plain bash session. The expected output was
generated ONCE with pyte (scripts/make_pty_goldens.py) at three sizes: display rows, cursor, the scrolled-row counter
and the scrolled rows of nexus.code's turn-detector screen. pyte is not needed to run these tests; when it is
installed, ``test_live_parity_with_pyte`` re-renders every fixture with it as well.
"""

import json
import random
import sys
from pathlib import Path

import pytest

from nexus_lite.pty.screen import Screen

FIXTURES = Path(__file__).parent / "fixtures" / "pty"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.bin"))
PROVIDERS = ("claude", "agy", "codex", "copilot", "gemini", "opencode", "shell")


class KeepScrollback(Screen):
    """nexus.code's turn detector keeps its history across ``ESC[3J`` (ink CLIs send it on every clear)."""

    def clear_scrollback(self) -> None:
        pass


def _cases():
    for name in NAMES:
        exp = json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))
        for size in exp:
            yield pytest.param(name, size, exp[size], id=f"{name}-{size}")


def _rows(screen):
    rows = [r.rstrip() for r in screen.display]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def test_fixture_set_covers_every_provider():
    assert NAMES, "no fixtures"
    for prov in PROVIDERS:
        assert any(n.startswith(prov) for n in NAMES), prov
    assert len(NAMES) >= 18


@pytest.mark.parametrize("name,size,exp", list(_cases()))
@pytest.mark.parametrize("chunked", [False, True], ids=["whole", "chunked"])
def test_golden_matches_pyte(name, size, exp, chunked):
    data = (FIXTURES / f"{name}.bin").read_bytes()
    cols, lines = map(int, size.split("x"))
    screen = KeepScrollback(cols, lines)
    if chunked:  # sequences and UTF-8 characters split at arbitrary byte offsets
        rnd, i = random.Random(7), 0
        while i < len(data):
            k = rnd.randint(1, 600)
            screen.feed(data[i:i + k])
            i += k
    else:
        screen.feed(data)
    assert _rows(screen) == exp["display"]
    assert [screen.cursor.x, screen.cursor.y] == exp["cursor"]
    assert screen.pushed == exp["pushed"]
    assert list(screen.scrolled) == exp["scrolled"]


def test_golden_screens_are_not_trivially_empty():
    """Guard against a vacuous pass: the rendered fixtures carry real UI text and scrolled rows."""
    total_rows = 0
    pushed = 0
    for name in NAMES:
        exp = json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))
        total_rows += sum(len(v["display"]) for v in exp.values())
        pushed += sum(v["pushed"] for v in exp.values())
    assert total_rows > 1500 and pushed > 300


@pytest.mark.parametrize("name", NAMES)
def test_live_parity_with_pyte(name):
    pytest.importorskip("pyte")
    pytest.importorskip("wcwidth")
    scripts = Path(__file__).parent.parent / "scripts"
    if not (scripts / "make_pty_goldens.py").exists():
        pytest.skip("scripts/make_pty_goldens.py is not in this tree (public snapshot)")
    sys.path.insert(0, str(scripts))
    try:
        import make_pty_goldens as gen
    finally:
        sys.path.pop(0)
    data = (FIXTURES / f"{name}.bin").read_bytes()
    display, cursor, pushed, scrolled = gen.reference(data, 120, 40)
    screen = KeepScrollback(120, 40)
    screen.feed(data)
    rows = [r.rstrip() for r in display]
    while rows and not rows[-1]:
        rows.pop()
    assert _rows(screen) == rows
    assert (screen.cursor.x, screen.cursor.y) == cursor
    assert screen.pushed == pushed and list(screen.scrolled) == scrolled
