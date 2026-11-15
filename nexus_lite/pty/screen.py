"""
nexus_lite.pty.screen — a stdlib VT text-screen reader for CONTROL (reading what an agent shows), not for display.

The screen is a character grid: no colours, no attributes. Feed it the raw bytes a program wrote to its terminal and
read the result back as text::

    >>> s = Screen(80, 24)
    >>> s.feed(b"\\x1b[2J\\x1b[Hhello \\x1b[1mworld\\x1b[0m")
    >>> s.text()
    'hello world'

Supported: C0 CR LF VT FF BS HT BEL; cursor motion (CUU CUD CUF CUB CNL CPL CHA HPA HPR VPA VPR CUP HVP, DECSC/DECRC,
``CSI s``/``CSI u``); erase (ED, EL, ECH; ``ESC[3J`` clears the scrollback only); edit (ICH DCH IL DL); scrolling
(DECSTBM, IND, RI, NEL, SU, SD); tab stops (HTS, TBC); modes IRM, LNM, DECOM, DECAWM and the alternate screen
(47 / 1047 / 1049); UTF-8 text with wide (two-cell), combining and zero-width characters. SGR, OSC, DCS/APC/PM/SOS,
charset designations, device queries and every unknown sequence are parsed and dropped. Input may be split anywhere
between :meth:`Screen.feed` calls. No input raises.

The screen keeps its own bounded scrollback (:attr:`Screen.scrolled`, with the monotonic counter
:attr:`Screen.pushed`) of the text of rows scrolled off the top of the main screen.

EXTENSION POINT. nexus.code builds a real terminal on top of this class; lite itself implements no display features.
Every control function is a separate public method that the parser dispatches to by name, with pyte-compatible names
and signatures (``draw``, ``index``, ``erase_in_display``, ``cursor_position``, ``select_graphic_rendition`` ...):
subclass, override what you need, call ``super()``. Cells are opaque to every movement operation: a cell is whatever
:meth:`Screen.make_cell` returns (default: the one-character ``str``), a blank is :meth:`Screen.blank_cell`, text
rendering goes through :meth:`Screen.cell_text` (a stub cell, the right half of a wide character, renders as ``""``).
A subclass that tracks colours overrides ``make_cell`` / ``blank_cell`` / ``cell_text`` / ``combine_cell`` and
``select_graphic_rendition``; scrolling, inserting and deleting need no change. See docs/specs/pty-package.md.
"""

from __future__ import annotations

import codecs
import collections
import functools
import re
import unicodedata
from typing import Deque, List, Optional, Union

DEFAULT_HISTORY = 2000

# ANSI modes (CSI Pm h/l) and DEC private modes (CSI ? Pm h/l) the screen acts on.
IRM = 4          # insert mode
LNM = 20         # linefeed also returns the carriage
DECOM = 6        # origin mode: CUP/VPA relative to the scroll region
DECAWM = 7       # auto-wrap
DECTCEM = 25     # cursor visible
ALT_47 = 47
ALT_1047 = 1047
ALT_1049 = 1049

_MAX_PARAM = 9999
_STRING_LIMIT = 1 << 20   # an unterminated OSC/DCS/APC string is dropped after this many characters
_OSC_LIMIT = 4096         # OSC payload kept for :meth:`Screen.osc`


@functools.lru_cache(maxsize=8192)
def char_width(ch: str) -> int:
    """Terminal cell width of one character: 2 (East Asian wide / fullwidth), 0 (combining marks, zero-width format
    characters, Hangul medial/final jamo), 1 otherwise; -1 for controls."""
    o = ord(ch)
    if o < 0x20 or 0x7F <= o < 0xA0:
        return -1
    if o < 0x300:
        return 1
    if (unicodedata.category(ch) in ("Mn", "Me", "Mc", "Cf") or 0x1160 <= o <= 0x11FF or 0xD7B0 <= o <= 0xD7FF
            or 0x1F3FB <= o <= 0x1F3FF):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Cursor:
    """Cursor position (0-based) and visibility."""

    __slots__ = ("x", "y", "hidden")

    def __init__(self, x: int = 0, y: int = 0, hidden: bool = False) -> None:
        self.x = x
        self.y = y
        self.hidden = hidden

    def copy(self) -> "Cursor":
        return Cursor(self.x, self.y, self.hidden)

    def __repr__(self) -> str:
        return f"Cursor(x={self.x}, y={self.y})"


# parser states
_GROUND, _ESC, _ESC_INTER, _CSI, _OSC, _OSC_ESC, _STR, _STR_ESC = range(8)

_PLAIN = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
_STR_END = re.compile("[\x07\x18\x1a\x1b\x9c]")

_C0 = {0x07: "bell", 0x08: "backspace", 0x09: "tab", 0x0A: "linefeed", 0x0B: "linefeed", 0x0C: "linefeed",
       0x0D: "carriage_return", 0x0E: "shift_out", 0x0F: "shift_in"}
_CSI_C0 = frozenset("\x07\x08\x09\x0a\x0b\x0c\x0d")

_ESC_FINAL = {"D": "index", "E": "next_line", "M": "reverse_index", "H": "set_tab_stop", "7": "save_cursor",
              "8": "restore_cursor", "c": "reset"}

# CSI final -> method taking one optional count
_CSI_COUNT = {"A": "cursor_up", "B": "cursor_down", "C": "cursor_forward", "D": "cursor_back",
              "E": "cursor_down1", "F": "cursor_up1", "G": "cursor_to_column", "`": "cursor_to_column",
              "a": "cursor_forward", "d": "cursor_to_line", "e": "cursor_down", "@": "insert_characters",
              "P": "delete_characters", "X": "erase_characters", "L": "insert_lines", "M": "delete_lines",
              "S": "scroll_up", "T": "scroll_down"}


class Screen:
    """A ``columns`` x ``lines`` character grid driven by a VT byte stream (see the module docstring)."""

    def __init__(self, columns: int = 80, lines: int = 24, history: int = DEFAULT_HISTORY) -> None:
        self.columns = max(1, int(columns))
        self.lines = max(1, int(lines))
        self.scrolled: Deque[str] = collections.deque(maxlen=max(0, int(history)))
        self.pushed = 0  # rows ever scrolled off the top (the deque wraps)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._st = _GROUND
        self._reset_csi()
        self._str_buf: List[str] = []
        self._str_len = 0
        self.title = ""
        self.icon_name = ""
        self._fast = type(self).make_cell is Screen.make_cell
        self.reset()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.columns}, {self.lines})"

    # --- cells (override to track more than the character) -------------------------------------------------
    def make_cell(self, char: str):
        """Cell for ``char`` drawn at the cursor. Default: the ``str`` itself."""
        return char

    def blank_cell(self):
        """Cell of an erased position. Default: ``" "``."""
        return " "

    def stub_cell(self):
        """Cell that fills the right half of a wide character. Default: ``""``."""
        return ""

    def cell_text(self, cell) -> str:
        """Text a cell renders as (``""`` for a stub)."""
        return cell

    def combine_cell(self, cell, mark: str):
        """Cell ``cell`` with the combining character ``mark`` attached. Default: NFC of the concatenation."""
        return unicodedata.normalize("NFC", cell + mark)

    def _blank_row(self) -> list:
        return [self.blank_cell()] * self.columns

    def _row_text(self, row: list) -> str:
        if self._fast:
            return "".join(row)
        return "".join(self.cell_text(c) for c in row)

    # --- state ---------------------------------------------------------------------------------------------
    def reset(self) -> None:
        """RIS: clear the screen, home the cursor, reset modes, margins and tab stops. Scrollback is kept."""
        self.buffer: List[list] = [self._blank_row() for _ in range(self.lines)]
        self.cursor = Cursor()
        self.margins: Optional[tuple] = None  # (top, bottom), 0-based inclusive; None = whole screen
        self.mode = set()
        self.private_mode = {DECAWM, DECTCEM}
        self.tabstops = set(range(8, self.columns, 8))
        self._saved: Optional[tuple] = None
        self._alt = False
        self._main_buffer: Optional[List[list]] = None
        self._main_margins: Optional[tuple] = None
        self._alt_saved: Optional[Cursor] = None

    @property
    def in_alternate_screen(self) -> bool:
        return self._alt

    @property
    def display(self) -> List[str]:
        """The screen as ``lines`` strings (not right-stripped). Never raises."""
        return [self._row_text(r) for r in self.buffer]

    def text(self) -> str:
        """The screen as text: rows right-stripped, trailing blank rows dropped, joined with ``"\\n"``."""
        rows = [r.rstrip() for r in self.display]
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows)

    def resize(self, lines: Optional[int] = None, columns: Optional[int] = None) -> None:
        """Resize the grid. Fewer lines drop rows from the top (they are not added to the scrollback), more lines add
        blank rows at the bottom; columns are cut or padded on the right. The cursor stays on the same text, margins
        are reset."""
        lines = max(1, int(lines or self.lines))
        columns = max(1, int(columns or self.columns))
        if lines == self.lines and columns == self.columns:
            return
        drop = max(0, self.lines - lines)
        old_columns = self.columns
        self.lines, self.columns = lines, columns
        for grid_name in ("buffer", "_main_buffer"):
            grid = getattr(self, grid_name)
            if grid is None:
                continue
            if drop:
                del grid[:drop]
            while len(grid) < lines:
                grid.append(self._blank_row())
            for row in grid:
                if len(row) > columns:
                    del row[columns:]
                elif len(row) < columns:
                    row.extend([self.blank_cell()] * (columns - len(row)))
        self.cursor.y = max(0, self.cursor.y - drop)
        self.margins = None
        self._main_margins = None
        stops = {t for t in self.tabstops if t < columns}
        if columns > old_columns:
            stops |= set(range((old_columns + 7) // 8 * 8, columns, 8))
        self.tabstops = stops
        self.ensure_hbounds()
        self.ensure_vbounds()

    # --- feeding -------------------------------------------------------------------------------------------
    def _reset_csi(self) -> None:
        self._params: List[int] = []
        self._cur = -1
        self._priv = ""
        self._inter = ""

    def feed(self, data: Union[bytes, bytearray, memoryview, str]) -> None:
        """Consume output. Bytes are decoded as UTF-8 incrementally (a character or escape sequence may be split
        across calls); ``str`` is taken as already decoded."""
        if isinstance(data, str):
            s = data
        else:
            s = self._decoder.decode(bytes(data))
        n = len(s)
        i = 0
        st = self._st
        plain = _PLAIN.match
        while i < n:
            if st == _GROUND:
                m = plain(s, i)
                if m is not None:
                    j = m.end()
                    self.draw(s[i:j])
                    i = j
                    continue
                c = s[i]
                i += 1
                o = ord(c)
                if o == 0x1B:
                    st = _ESC
                elif o in _C0:
                    getattr(self, _C0[o])()
                elif o == 0x9B:
                    self._reset_csi()
                    st = _CSI
                elif o == 0x9D:
                    self._begin_string(True)
                    st = _OSC
                elif o in (0x90, 0x98, 0x9E, 0x9F):
                    self._begin_string(False)
                    st = _STR
            elif st == _ESC:
                c = s[i]
                i += 1
                if c == "[":
                    self._reset_csi()
                    st = _CSI
                elif c == "]":
                    self._begin_string(True)
                    st = _OSC
                elif c in "PX^_":
                    self._begin_string(False)
                    st = _STR
                elif "\x20" <= c <= "\x2f":
                    st = _ESC_INTER
                elif c == "\x1b":
                    pass
                elif c in "\x18\x1a":
                    st = _GROUND
                elif c in _ESC_FINAL:
                    getattr(self, _ESC_FINAL[c])()
                    st = _GROUND
                else:
                    st = _GROUND
            elif st == _ESC_INTER:
                c = s[i]
                i += 1
                if c == "\x1b":
                    st = _ESC
                elif c in "\x18\x1a" or c >= "\x30":
                    st = _GROUND  # final byte (``ESC ( B``, ``ESC # 8``, ...): all dropped
            elif st == _CSI:
                c = s[i]
                i += 1
                if "0" <= c <= "9":
                    self._cur = min(max(self._cur, 0) * 10 + (ord(c) - 48), _MAX_PARAM)
                elif c == ";" or c == ":":
                    self._params.append(max(self._cur, 0))
                    self._cur = -1
                elif c in "?<=>":
                    if not self._priv:
                        self._priv = c
                elif "\x20" <= c <= "\x2f":
                    self._inter += c
                elif "\x40" <= c <= "\x7e":
                    self._params.append(max(self._cur, 0))
                    st = _GROUND
                    self._csi_dispatch(c)
                elif c == "\x1b":
                    st = _ESC
                elif c in "\x18\x1a":
                    st = _GROUND
                elif c in _CSI_C0:
                    getattr(self, _C0[ord(c)])()
            else:  # _OSC, _OSC_ESC, _STR, _STR_ESC
                if st == _OSC_ESC or st == _STR_ESC:
                    c = s[i]
                    i += 1
                    osc = st == _OSC_ESC
                    if c == "\\":
                        if osc:
                            self._end_osc()
                        st = _GROUND
                    else:  # ESC aborts the string and starts a new sequence with c
                        if osc:
                            self._end_osc()
                        st = _ESC
                        i -= 1
                    continue
                osc = st == _OSC
                m = _STR_END.search(s, i)
                if m is None:
                    self._str_chunk(s[i:], osc)
                    i = n
                    if self._str_len > _STRING_LIMIT:
                        st = _GROUND
                    continue
                self._str_chunk(s[i:m.start()], osc)
                i = m.end()
                c = m.group()
                if c == "\x07":
                    if osc:
                        self._end_osc()
                        st = _GROUND
                elif c == "\x9c":
                    if osc:
                        self._end_osc()
                    st = _GROUND
                elif c == "\x1b":
                    st = _OSC_ESC if osc else _STR_ESC
                else:  # CAN / SUB abort the string
                    st = _GROUND
                if self._str_len > _STRING_LIMIT:
                    st = _GROUND
        self._st = st

    def _begin_string(self, osc: bool) -> None:
        self._str_buf = []
        self._str_len = 0

    def _str_chunk(self, chunk: str, osc: bool) -> None:
        self._str_len += len(chunk)
        if osc and self._str_len <= _OSC_LIMIT:
            self._str_buf.append(chunk)

    def _end_osc(self) -> None:
        payload = "".join(self._str_buf)
        self._str_buf = []
        code, _, text = payload.partition(";")
        self.osc(code, text)

    def osc(self, code: str, text: str) -> None:
        """OSC ``code;text`` received (payload capped at 4096 characters). Default: window title / icon name for
        codes 0, 1 and 2, everything else ignored."""
        if code in ("0", "2"):
            self.set_title(text)
        if code in ("0", "1"):
            self.set_icon_name(text)

    def set_title(self, text: str) -> None:
        self.title = text

    def set_icon_name(self, text: str) -> None:
        self.icon_name = text

    def _csi_dispatch(self, final: str) -> None:
        params, priv, inter = self._params, self._priv, self._inter
        self._reset_csi()
        if inter or priv in ("<", "=", ">"):
            return  # CSI ! p, CSI SP q, CSI $ p, kitty/xterm ``>`` ``<`` ``=`` extensions: no grid effect
        p0 = params[0]
        if priv == "?":
            if final == "h":
                self.set_mode(*params, private=True)
            elif final == "l":
                self.reset_mode(*params, private=True)
            elif final == "J":
                self.erase_in_display(p0, private=True)
            elif final == "K":
                self.erase_in_line(p0, private=True)
            return
        name = _CSI_COUNT.get(final)
        if name is not None:
            if final == "T" and len(params) > 1:
                return  # mouse tracking initiation, not scroll down
            getattr(self, name)(p0 or None)
        elif final == "H" or final == "f":
            self.cursor_position(p0 or None, (params[1] if len(params) > 1 else 0) or None)
        elif final == "J":
            self.erase_in_display(p0)
        elif final == "K":
            self.erase_in_line(p0)
        elif final == "m":
            self.select_graphic_rendition(*params)
        elif final == "r":
            self.set_margins(p0 or None, (params[1] if len(params) > 1 else 0) or None)
        elif final == "h":
            self.set_mode(*params)
        elif final == "l":
            self.reset_mode(*params)
        elif final == "g":
            self.clear_tab_stop(p0)
        elif final == "s":
            self.save_cursor()
        elif final == "u":
            self.restore_cursor()
        # everything else (DA, DSR, XTWINOPS, ...): no grid effect

    # --- text ----------------------------------------------------------------------------------------------
    def draw(self, data: str) -> None:
        """Write ``data`` at the cursor and advance it, wrapping at the right edge when DECAWM is set."""
        cols = self.columns
        cur = self.cursor
        wrap = DECAWM in self.private_mode
        if self._fast and data.isascii() and IRM not in self.mode:
            # printable ASCII is one cell per character: write whole slices
            while data:
                if cur.x >= cols:
                    if wrap:
                        self.carriage_return()
                        self.linefeed()
                    else:
                        cur.x = cols - 1
                row = self.buffer[cur.y]
                x = cur.x
                k = min(len(data), cols - x)
                if row[x] == "" and x > 0:
                    row[x - 1] = self.blank_cell()  # overwrote the right half of a wide character
                if x + k < cols and row[x + k] == "":
                    row[x + k] = self.blank_cell()  # overwrote the left half of one
                row[x:x + k] = data[:k]
                cur.x = x + k
                data = data[k:]
            return
        for ch in data:
            w = char_width(ch)
            if w < 0:
                continue
            if cur.x >= cols:
                if wrap:
                    self.carriage_return()
                    self.linefeed()
                elif w > 0:
                    cur.x = cols - w
            if w == 0:
                if unicodedata.combining(ch):
                    self._attach_mark(ch)
                continue
            if IRM in self.mode:
                self.insert_characters(w)
            row = self.buffer[cur.y]
            x = cur.x
            if x > 0 and self._is_stub(row[x]):
                row[x - 1] = self.blank_cell()
            if w == 2:
                if x + 1 < cols:
                    if x + 2 < cols and self._is_stub(row[x + 2]):
                        row[x + 2] = self.blank_cell()
                    row[x + 1] = self.stub_cell()
                row[x] = self.make_cell(ch)
            else:
                if x + 1 < cols and self._is_stub(row[x + 1]):
                    row[x + 1] = self.blank_cell()
                row[x] = self.make_cell(ch)
            cur.x = min(x + w, cols)

    def _is_stub(self, cell) -> bool:
        return self.cell_text(cell) == ""

    def _attach_mark(self, mark: str) -> None:
        cur = self.cursor
        if cur.x:
            row, x = self.buffer[cur.y], cur.x - 1
        elif cur.y:
            row, x = self.buffer[cur.y - 1], self.columns - 1
        else:
            return
        if x and self.cell_text(row[x]) == "":
            x -= 1  # the mark follows a wide character: attach to its left half
        row[x] = self.combine_cell(row[x], mark)

    # --- C0 and cursor -------------------------------------------------------------------------------------
    def bell(self) -> None:
        """BEL. Ignored."""

    def shift_in(self) -> None:
        """SI. Ignored (UTF-8 mode: no charset switching)."""

    def shift_out(self) -> None:
        """SO. Ignored."""

    def backspace(self) -> None:
        self.cursor_back()

    def tab(self) -> None:
        x = self.cursor.x
        for stop in sorted(self.tabstops):
            if x < stop:
                self.cursor.x = stop
                return
        self.cursor.x = self.columns - 1

    def carriage_return(self) -> None:
        self.cursor.x = 0

    def linefeed(self) -> None:
        self.index()
        if LNM in self.mode:
            self.carriage_return()

    def next_line(self) -> None:
        """NEL: carriage return, then index."""
        self.carriage_return()
        self.index()

    def ensure_hbounds(self) -> None:
        self.cursor.x = min(max(0, self.cursor.x), self.columns - 1)

    def ensure_vbounds(self, use_margins: Optional[bool] = None) -> None:
        if (use_margins or DECOM in self.private_mode) and self.margins is not None:
            top, bottom = self.margins
        else:
            top, bottom = 0, self.lines - 1
        self.cursor.y = min(max(top, self.cursor.y), bottom)

    def _region(self) -> tuple:
        return self.margins or (0, self.lines - 1)

    def cursor_up(self, count: Optional[int] = None) -> None:
        top = self._region()[0]
        if self.cursor.y < top:
            top = 0
        self.cursor.y = max(self.cursor.y - (count or 1), top)

    def cursor_down(self, count: Optional[int] = None) -> None:
        bottom = self._region()[1]
        if self.cursor.y > bottom:
            bottom = self.lines - 1
        self.cursor.y = min(self.cursor.y + (count or 1), bottom)

    def cursor_up1(self, count: Optional[int] = None) -> None:
        self.cursor_up(count)
        self.carriage_return()

    def cursor_down1(self, count: Optional[int] = None) -> None:
        self.cursor_down(count)
        self.carriage_return()

    def cursor_back(self, count: Optional[int] = None) -> None:
        if self.cursor.x == self.columns:  # a pending wrap does not count as a column
            self.cursor.x -= 1
        self.cursor.x -= count or 1
        self.ensure_hbounds()

    def cursor_forward(self, count: Optional[int] = None) -> None:
        self.cursor.x += count or 1
        self.ensure_hbounds()

    def cursor_position(self, line: Optional[int] = None, column: Optional[int] = None) -> None:
        """CUP: 1-based ``line``/``column`` (None = 1). With DECOM the line is relative to the scroll region."""
        column = (column or 1) - 1
        line = (line or 1) - 1
        if self.margins is not None and DECOM in self.private_mode:
            line += self.margins[0]
            if not self.margins[0] <= line <= self.margins[1]:
                return
        self.cursor.x = column
        self.cursor.y = line
        self.ensure_hbounds()
        self.ensure_vbounds()

    def cursor_to_column(self, column: Optional[int] = None) -> None:
        self.cursor.x = (column or 1) - 1
        self.ensure_hbounds()

    def cursor_to_line(self, line: Optional[int] = None) -> None:
        self.cursor.y = (line or 1) - 1
        if DECOM in self.private_mode and self.margins is not None:
            self.cursor.y += self.margins[0]
        self.ensure_vbounds()

    def save_cursor(self) -> None:
        """DECSC: remember position, origin mode and auto-wrap."""
        self._saved = (self.cursor.x, self.cursor.y, DECOM in self.private_mode, DECAWM in self.private_mode)

    def restore_cursor(self) -> None:
        """DECRC: restore what :meth:`save_cursor` remembered; with nothing saved, home and reset origin mode."""
        if self._saved is None:
            self.private_mode.discard(DECOM)
            self.cursor.x = self.cursor.y = 0
            return
        x, y, origin, wrap = self._saved
        (self.private_mode.add if origin else self.private_mode.discard)(DECOM)
        (self.private_mode.add if wrap else self.private_mode.discard)(DECAWM)
        self.cursor.x, self.cursor.y = x, y
        self.ensure_hbounds()
        self.ensure_vbounds(use_margins=True)

    def set_tab_stop(self) -> None:
        self.tabstops.add(self.cursor.x)

    def clear_tab_stop(self, how: int = 0) -> None:
        if how == 0:
            self.tabstops.discard(self.cursor.x)
        elif how == 3:
            self.tabstops.clear()

    # --- scrolling -----------------------------------------------------------------------------------------
    def set_margins(self, top: Optional[int] = None, bottom: Optional[int] = None) -> None:
        """DECSTBM: scroll region from 1-based ``top`` to ``bottom`` (None = screen edge). Homes the cursor."""
        if not top and not bottom:
            self.margins = None
            return
        t = max(0, min((top or 1) - 1, self.lines - 1))
        b = max(0, min((bottom or self.lines) - 1, self.lines - 1))
        if b - t >= 1:
            self.margins = (t, b)
            self.cursor_position()

    def row_scrolled_off(self, row: list) -> None:
        """A row left the top of the main screen: remember its text in :attr:`scrolled`."""
        self.scrolled.append(self._row_text(row).rstrip())
        self.pushed += 1

    def clear_scrollback(self) -> None:
        """``ESC[3J``: forget the scrolled-off rows (:attr:`pushed` keeps counting). Override to keep history."""
        self.scrolled.clear()

    def _scroll_up(self, top: int, bottom: int, n: int) -> None:
        n = min(max(n, 1), bottom - top + 1)
        buf = self.buffer
        if top == 0 and not self._alt:
            for row in buf[:n]:
                self.row_scrolled_off(row)
        buf[top:bottom + 1] = buf[top + n:bottom + 1] + [self._blank_row() for _ in range(n)]

    def _scroll_down(self, top: int, bottom: int, n: int) -> None:
        n = min(max(n, 1), bottom - top + 1)
        buf = self.buffer
        buf[top:bottom + 1] = [self._blank_row() for _ in range(n)] + buf[top:bottom + 1 - n]

    def index(self) -> None:
        """IND: move down one line, scrolling the region up when on its bottom line."""
        top, bottom = self._region()
        if self.cursor.y == bottom:
            self._scroll_up(top, bottom, 1)
        elif self.cursor.y < self.lines - 1:
            self.cursor.y += 1

    def reverse_index(self) -> None:
        """RI: move up one line, scrolling the region down when on its top line."""
        top, bottom = self._region()
        if self.cursor.y == top:
            self._scroll_down(top, bottom, 1)
        elif self.cursor.y > 0:
            self.cursor.y -= 1

    def scroll_up(self, count: Optional[int] = None) -> None:
        """SU: scroll the region up ``count`` lines (cursor does not move)."""
        top, bottom = self._region()
        self._scroll_up(top, bottom, count or 1)

    def scroll_down(self, count: Optional[int] = None) -> None:
        """SD: scroll the region down ``count`` lines (cursor does not move)."""
        top, bottom = self._region()
        self._scroll_down(top, bottom, count or 1)

    # --- editing -------------------------------------------------------------------------------------------
    def insert_lines(self, count: Optional[int] = None) -> None:
        top, bottom = self._region()
        if top <= self.cursor.y <= bottom:
            self._scroll_down(self.cursor.y, bottom, count or 1)
            self.carriage_return()

    def delete_lines(self, count: Optional[int] = None) -> None:
        top, bottom = self._region()
        if top <= self.cursor.y <= bottom:
            y = self.cursor.y
            n = min(count or 1, bottom - y + 1)
            buf = self.buffer
            buf[y:bottom + 1] = buf[y + n:bottom + 1] + [self._blank_row() for _ in range(n)]
            self.carriage_return()

    def insert_characters(self, count: Optional[int] = None) -> None:
        x, row = self.cursor.x, self.buffer[self.cursor.y]
        n = min(count or 1, self.columns - x)
        if n > 0:
            row[x:] = [self.blank_cell()] * n + row[x:self.columns - n]

    def delete_characters(self, count: Optional[int] = None) -> None:
        x, row = self.cursor.x, self.buffer[self.cursor.y]
        n = min(count or 1, self.columns - x)
        if n > 0:
            row[x:] = row[x + n:] + [self.blank_cell()] * n

    def erase_characters(self, count: Optional[int] = None) -> None:
        x, row = self.cursor.x, self.buffer[self.cursor.y]
        n = min(count or 1, self.columns - x)
        if n > 0:
            row[x:x + n] = [self.blank_cell()] * n

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        """EL: 0 = cursor to end of line, 1 = start to cursor, 2 = whole line."""
        row, x = self.buffer[self.cursor.y], min(self.cursor.x, self.columns)
        blank = self.blank_cell()
        if how == 0:
            row[x:] = [blank] * (self.columns - x)
        elif how == 1:
            row[:x + 1] = [blank] * min(x + 1, self.columns)
        elif how == 2:
            row[:] = [blank] * self.columns

    def erase_in_display(self, how: int = 0, private: bool = False) -> None:
        """ED: 0 = cursor to end of screen, 1 = start to cursor, 2 = whole screen, 3 = the scrollback only."""
        y = self.cursor.y
        if how == 0:
            for r in range(y + 1, self.lines):
                self.buffer[r] = self._blank_row()
            self.erase_in_line(0)
        elif how == 1:
            for r in range(y):
                self.buffer[r] = self._blank_row()
            self.erase_in_line(1)
        elif how == 2:
            for r in range(self.lines):
                self.buffer[r] = self._blank_row()
        elif how == 3:
            self.clear_scrollback()

    # --- modes ---------------------------------------------------------------------------------------------
    def set_mode(self, *modes: int, private: bool = False) -> None:
        for m in modes:
            if not private:
                self.mode.add(m)
                continue
            self.private_mode.add(m)
            if m == DECOM:
                self.cursor_position()
            elif m == DECTCEM:
                self.cursor.hidden = False
            elif m in (ALT_47, ALT_1047, ALT_1049):
                self._enter_alt(m)

    def reset_mode(self, *modes: int, private: bool = False) -> None:
        for m in modes:
            if not private:
                self.mode.discard(m)
                continue
            self.private_mode.discard(m)
            if m == DECOM:
                self.cursor_position()
            elif m == DECTCEM:
                self.cursor.hidden = True
            elif m in (ALT_47, ALT_1047, ALT_1049):
                self._leave_alt(m)

    def _enter_alt(self, mode: int) -> None:
        if self._alt:
            return
        if mode == ALT_1049:
            self._alt_saved = self.cursor.copy()
        self._main_buffer, self._main_margins = self.buffer, self.margins
        self.buffer = [self._blank_row() for _ in range(self.lines)]
        self.margins = None
        self._alt = True

    def _leave_alt(self, mode: int) -> None:
        if not self._alt:
            return
        self.buffer, self.margins = self._main_buffer, self._main_margins
        self._main_buffer = self._main_margins = None
        self._alt = False
        if mode == ALT_1049 and self._alt_saved is not None:
            self.cursor.x, self.cursor.y = self._alt_saved.x, self._alt_saved.y
            self._alt_saved = None
            self.ensure_hbounds()
            self.ensure_vbounds()

    def select_graphic_rendition(self, *attrs: int) -> None:
        """SGR. Ignored: lite reads characters only. Override together with ``make_cell`` / ``blank_cell`` to track
        attributes."""


def render(data: Union[bytes, str], cols: int = 240, rows: int = 45) -> str:
    """Render raw terminal output to trimmed screen text: ``Screen(cols, rows)``, feed, :meth:`Screen.text`."""
    screen = Screen(cols, rows)
    screen.feed(data)
    return screen.text()
