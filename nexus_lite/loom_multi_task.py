"""Task data for the loom multi-agent example: spec, mock payloads, HIDDEN acceptance tests.

The hidden tests are only ever written to a directory OUTSIDE the workers' sandbox and are
never put in a prompt. Workers/baseline see only failing test names + failure kinds.
Stdlib only.
"""
from __future__ import annotations

NAME = "basic"
SPEC = (
    "Build four small, independent python modules, all flat in one directory (no packages, stdlib only).\n"
    "1) tokenizer.py: tokenize(text) -> list of (kind, value) tuples. Kinds: 'num' (an integer or decimal such as "
    "12, 3.5 or .5; a second '.' inside one number is an error), 'id' (a Unicode letter or underscore followed by "
    "letters, digits or underscores) and 'op' (one of + - * / ** ( ) , matched greedily). Whitespace is skipped. "
    "Identifiers are case-insensitive, so the value of an 'id' token is the identifier in its canonical caseless form. "
    "Any other character raises ValueError whose message contains that character's index.\n"
    "2) intervals.py: an interval is a (start, end) tuple of ints covering start up to end, end excluded. "
    "merge(intervals) returns a sorted list of disjoint tuples; intervals that overlap or merely touch are combined and "
    "empty ones (start == end) are dropped; start > end raises ValueError. total_length(intervals) is the size of the "
    "union. gaps(intervals, lo, hi) returns the uncovered parts of [lo, hi) as a sorted list of tuples "
    "(lo > hi raises ValueError).\n"
    "3) ttlcache.py: class TTLCache(capacity, ttl, clock=time.monotonic), an LRU cache whose entries also expire. "
    "get(key, default=None) returns the value and marks the key most recently used (it does not extend its life). "
    "put(key, value) stores it; putting an existing key replaces the value and restarts its life. An entry lives ttl time "
    "units after its put, so at age exactly ttl it is already gone. Expired entries are invisible to get, `in` and "
    "len(), and never cause a live entry to be evicted: when full, drop expired entries first, then the least recently "
    "used live one. capacity < 1 raises ValueError.\n"
    "4) money.py: amounts are integer cents. to_cents(text) parses a decimal string such as '12.345', '-0.5' or "
    "'1,234.5' (well-formed thousands commas allowed) into cents; more than two decimals are rounded to the nearest "
    "cent and exact halves go to the even cent. Invalid text raises ValueError. split_evenly(total_cents, parts) "
    "returns `parts` ints that sum to the total and differ by at most 1, larger shares first (parts < 1 raises "
    "ValueError). format_cents(cents) renders '$1,234.50', negatives as '-$0.05'."
)

FILES = ["tokenizer.py", "intervals.py", "ttlcache.py", "money.py"]
GOALS = {
    "tokenizer.py": "tokenize(text) per the spec (num/id/op tokens, greedy **, caseless identifiers, ValueError with index)",
    "intervals.py": "merge, total_length, gaps over half-open integer intervals",
    "ttlcache.py": "TTLCache: LRU + TTL with injectable clock",
    "money.py": "to_cents, split_evenly, format_cents on integer cents",
}
DEFAULT_SPLIT = [{"file": f, "goal": GOALS[f]} for f in FILES]

_TOKENIZER = '''_DIGITS = "0123456789"


def tokenize(text):
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c in _DIGITS or (c == "." and i + 1 < n and text[i + 1] in _DIGITS):
            j, dots = i, 0
            while j < n and (text[j] in _DIGITS or text[j] == "."):
                if text[j] == ".":
                    dots += 1
                    if dots > 1:
                        raise ValueError("bad number at index %d" % j)
                j += 1
            out.append(("num", text[i:j]))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("id", text[i:j].CASE()))
            i = j
        elif text.startswith("**", i):
            out.append(("op", "**"))
            i += 2
        elif c in "+-*/(),":
            out.append(("op", c))
            i += 1
        else:
            raise ValueError("unexpected %r at index %d" % (c, i))
    return out
'''
_INTERVALS = '''def merge(intervals):
    ivs = []
    for s, e in intervals:
        if s > e:
            raise ValueError("start > end")
        if s < e:
            ivs.append((s, e))
    ivs.sort()
    out = []
    for s, e in ivs:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def total_length(intervals):
    return sum(e - s for s, e in merge(intervals))


def gaps(intervals, lo, hi):
    if lo > hi:
        raise ValueError("lo > hi")
    cur, out = lo, []
    for s, e in merge(intervals):
        if e <= cur:
            continue
        if s >= hi:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
        if cur >= hi:
            break
    if cur < hi:
        out.append((cur, hi))
    return out
'''
_TTLCACHE = '''import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, capacity, ttl, clock=time.monotonic):
        if capacity < 1:
            raise ValueError("capacity < 1")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._d = OrderedDict()  # key -> (value, born)

    def _alive(self, born):
        return self.clock() - born AGE_OP self.ttl

    def _purge(self):
        for k in [k for k, (_v, b) in self._d.items() if not self._alive(b)]:
            del self._d[k]

    def get(self, key, default=None):
        item = self._d.get(key)
        if item is None:
            return default
        if not self._alive(item[1]):
            del self._d[key]
            return default
        self._d.move_to_end(key)
        return item[0]

    def put(self, key, value):
        self._d.pop(key, None)
        self._purge()
        while len(self._d) >= self.capacity:
            self._d.popitem(last=False)
        self._d[key] = (value, self.clock())

    def __contains__(self, key):
        item = self._d.get(key)
        return item is not None and self._alive(item[1])

    def __len__(self):
        self._purge()
        return len(self._d)
'''
_MONEY = '''import re
from decimal import Decimal, ROUND_MODE

_NUM = re.compile(r"([+-]?)((?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d+)?|\\.\\d+)")


def to_cents(text):
    m = _NUM.fullmatch(text.strip())
    if not m:
        raise ValueError("bad amount")
    cents = int((Decimal(m.group(2).replace(",", "")) * 100).quantize(Decimal(1), rounding=ROUND_MODE))
    return -cents if m.group(1) == "-" else cents


def split_evenly(total_cents, parts):
    if parts < 1:
        raise ValueError("parts < 1")
    base = total_cents // parts
    extra = total_cents - base * parts
    return [base + 1 if i < extra else base for i in range(parts)]


def format_cents(cents):
    a = abs(cents)
    return "%s$%s.%02d" % ("-" if cents < 0 else "", format(a // 100, ","), a % 100)
'''

GOOD = {
    "tokenizer.py": _TOKENIZER.replace("CASE", "casefold"),
    "intervals.py": _INTERVALS,
    "ttlcache.py": _TTLCACHE.replace("AGE_OP", "<"),
    "money.py": _MONEY.replace("ROUND_MODE", "ROUND_HALF_EVEN"),
}
# Scripted first attempt (mock path only): two modules fall into the spec's traps.
FLAWED = dict(GOOD,
              **{"tokenizer.py": _TOKENIZER.replace("CASE", "lower"),
                 "money.py": _MONEY.replace("ROUND_MODE", "ROUND_HALF_UP")})
# file -> (attempt-0 payload, reworked payload)
MOCK_FILES = {f: (FLAWED[f], GOOD[f]) for f in FILES}

_M = "# hidden-suite-marker-7f3a91\n"
HIDDEN_TESTS = {
    "test_h_tokenizer.py": _M + '''import pytest
from tokenizer import tokenize


def test_basic_expression():
    assert tokenize("1 + 2") == [("num", "1"), ("op", "+"), ("num", "2")]


def test_decimals_and_leading_dot():
    assert tokenize("3.5*.5") == [("num", "3.5"), ("op", "*"), ("num", ".5")]


def test_double_star_is_one_token():
    assert tokenize("2**3") == [("num", "2"), ("op", "**"), ("num", "3")]
    assert tokenize("a***b") == [("id", "a"), ("op", "**"), ("op", "*"), ("id", "b")]


def test_two_dots_in_number():
    with pytest.raises(ValueError):
        tokenize("1.2.3")


def test_bad_char_reports_index():
    with pytest.raises(ValueError) as e:
        tokenize("1 + $")
    assert "4" in str(e.value)


def test_identifier_is_caseless_ascii():
    assert tokenize("Foo_1 + BAR") == [("id", "foo_1"), ("op", "+"), ("id", "bar")]


def test_identifier_caseless_unicode():
    assert tokenize("Stra\\u00dfe") == tokenize("STRASSE") == [("id", "strasse")]


def test_final_sigma_is_caseless():
    assert tokenize("\\u03a3\\u0391\\u03a3") == [("id", "\\u03c3\\u03b1\\u03c3")]


def test_empty_and_whitespace_only():
    assert tokenize("") == [] and tokenize(" \\t\\n") == []
''',
    "test_h_intervals.py": _M + '''import pytest
from intervals import merge, total_length, gaps


def test_merge_overlap():
    assert merge([(5, 8), (1, 3), (2, 4)]) == [(1, 4), (5, 8)]


def test_merge_touching_combines():
    assert merge([(1, 3), (3, 5)]) == [(1, 5)]


def test_merge_drops_empty():
    assert merge([(2, 2), (4, 6)]) == [(4, 6)]


def test_merge_contained_and_empty_input():
    assert merge([(1, 10), (2, 3)]) == [(1, 10)] and merge([]) == []


def test_merge_rejects_reversed_even_late():
    with pytest.raises(ValueError):
        merge([(1, 2), (5, 3)])


def test_total_length_is_union():
    assert total_length([(0, 5), (3, 8), (10, 11)]) == 9


def test_gaps_basic_and_clipped():
    assert gaps([(2, 4), (6, 8)], 0, 10) == [(0, 2), (4, 6), (8, 10)]
    assert gaps([(0, 20)], 5, 10) == [] and gaps([(-5, 2)], 0, 4) == [(2, 4)]


def test_gaps_touching_leaves_no_gap_and_bad_range():
    assert gaps([(0, 3), (3, 6)], 0, 6) == []
    assert gaps([], 3, 3) == []
    with pytest.raises(ValueError):
        gaps([], 5, 1)
''',
    "test_h_ttlcache.py": _M + '''import pytest
from ttlcache import TTLCache


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_basic_get_put_default():
    c = TTLCache(2, 10, Clock())
    c.put("a", 1)
    assert c.get("a") == 1 and c.get("zz", "d") == "d"


def test_expires_at_exactly_ttl():
    clk = Clock()
    c = TTLCache(2, 10, clk)
    c.put("a", 1)
    clk.t = 9.99
    assert c.get("a") == 1
    clk.t = 10
    assert c.get("a") is None and "a" not in c


def test_get_does_not_extend_life():
    clk = Clock()
    c = TTLCache(2, 10, clk)
    c.put("a", 1)
    clk.t = 9
    assert c.get("a") == 1
    clk.t = 10
    assert c.get("a") is None


def test_put_existing_restarts_life():
    clk = Clock()
    c = TTLCache(2, 10, clk)
    c.put("a", 1)
    clk.t = 9
    c.put("a", 2)
    clk.t = 15
    assert c.get("a") == 2


def test_lru_eviction_respects_get_recency():
    c = TTLCache(2, 100, Clock())
    c.put("a", 1)
    c.put("b", 2)
    c.get("a")
    c.put("c", 3)
    assert "b" not in c and "a" in c and "c" in c


def test_expired_never_evicts_live():
    clk = Clock()
    c = TTLCache(2, 10, clk)
    c.put("old", 1)
    clk.t = 5
    c.put("live", 2)
    clk.t = 11
    c.put("new", 3)
    assert "live" in c and "new" in c and "old" not in c


def test_len_ignores_expired_and_contains_keeps_order():
    clk = Clock()
    c = TTLCache(3, 10, clk)
    c.put("a", 1)
    c.put("b", 2)
    assert "a" in c          # must not refresh recency
    c.put("c", 3)
    c.put("d", 4)
    assert "a" not in c and len(c) == 3
    clk.t = 10
    assert len(c) == 0


def test_bad_capacity():
    with pytest.raises(ValueError):
        TTLCache(0, 1)
''',
    "test_h_money.py": _M + '''import pytest
from money import to_cents, split_evenly, format_cents


def test_plain_and_signed():
    assert to_cents("12.34") == 1234 and to_cents("-0.5") == -50 and to_cents(".5") == 50 and to_cents("7") == 700


def test_thousands_commas():
    assert to_cents("1,234.5") == 123450
    for bad in ("1,23", "12,", ",5", "1,2345"):
        with pytest.raises(ValueError):
            to_cents(bad)


def test_ties_go_to_even_cent():
    assert to_cents("12.345") == 1234 and to_cents("12.355") == 1236
    assert to_cents("0.125") == 12 and to_cents("0.135") == 14


def test_ties_even_for_negatives():
    assert to_cents("-0.125") == -12 and to_cents("-0.135") == -14


def test_non_ties_round_to_nearest():
    assert to_cents("1.006") == 101 and to_cents("1.004") == 100 and to_cents("2.675001") == 268


def test_invalid_text():
    for bad in ("", "abc", "1.2.3", "$5", "--1"):
        with pytest.raises(ValueError):
            to_cents(bad)


def test_split_evenly():
    assert split_evenly(10, 3) == [4, 3, 3] and split_evenly(9, 3) == [3, 3, 3]
    assert split_evenly(-5, 3) == [-1, -2, -2] and split_evenly(0, 2) == [0, 0]
    assert split_evenly(2, 5) == [1, 1, 0, 0, 0]
    with pytest.raises(ValueError):
        split_evenly(5, 0)


def test_format_cents():
    assert format_cents(123450) == "$1,234.50" and format_cents(5) == "$0.05"
    assert format_cents(-5) == "-$0.05" and format_cents(0) == "$0.00" and format_cents(100000000) == "$1,000,000.00"
''',
}
HIDDEN_TOTAL = sum(src.count("\ndef test_") for src in HIDDEN_TESTS.values())
