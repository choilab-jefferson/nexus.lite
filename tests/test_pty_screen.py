"""nexus_lite.pty.screen: the stdlib VT text-screen reader, one test group per control sequence."""

import random

import pytest

from nexus_lite.pty.screen import Screen, char_width, render

ESC = "\x1b"


def feed(s: Screen, text) -> Screen:
    s.feed(text)
    return s


def screen(text="", cols=20, lines=6, **kw) -> Screen:
    return feed(Screen(cols, lines, **kw), text) if text else Screen(cols, lines, **kw)


def rows(s: Screen):
    return [r.rstrip() for r in s.display]


# --- text, C0 -----------------------------------------------------------------------------------------------------

def test_plain_text_cr_lf_and_trimmed_text():
    s = screen("hello\r\nworld")
    assert rows(s)[:2] == ["hello", "world"]
    assert s.text() == "hello\nworld"
    assert (s.cursor.x, s.cursor.y) == (5, 1)


def test_lf_alone_keeps_the_column_unless_lnm():
    assert rows(screen("ab\ncd"))[:2] == ["ab", "  cd"]
    assert rows(screen(f"{ESC}[20hab\ncd"))[:2] == ["ab", "cd"]


def test_backspace_and_overwrite():
    assert rows(screen("abc\b\bX"))[0] == "aXc"
    assert screen("\b\b").cursor.x == 0


def test_tab_stops_default_hts_tbc():
    s = screen("a\tb", cols=30)
    assert s.cursor.x == 9 and rows(s)[0] == "a       b"
    s = screen(f"{ESC}[3g\tb", cols=30)  # all stops cleared: tab runs to the last column
    assert rows(s)[0] == " " * 29 + "b"
    s = screen(f"{ESC}[3g\x1b[1;5H{ESC}H\r\tb", cols=30)  # HTS at column 4
    assert rows(s)[0] == "    b"
    s = screen(f"\x1b[1;9H{ESC}[g\r\tb", cols=30)  # TBC 0 clears the stop at column 8: next is 16
    assert s.cursor.x == 17


def test_autowrap_defers_wrap_to_the_next_character():
    s = screen("abcdefghij", cols=10, lines=3)
    assert (s.cursor.x, s.cursor.y) == (10, 0) and rows(s)[1] == ""
    feed(s, "k")
    assert rows(s)[:2] == ["abcdefghij", "k"]
    feed(s, "\b\bZ")  # cursor_back from a pending wrap counts the last column
    assert rows(s)[1] == "Z"


def test_no_autowrap_overwrites_last_column():
    s = screen(f"{ESC}[?7labcdefghijkl", cols=10, lines=3)
    assert rows(s)[0] == "abcdefghil" and rows(s)[1] == ""
    s = screen(f"{ESC}[?7l{ESC}[?7habcdefghijkl", cols=10, lines=3)
    assert rows(s)[:2] == ["abcdefghij", "kl"]


def test_vt_ff_are_linefeeds_and_other_c0_ignored():
    assert rows(screen("a\x0bb\x0cc\x00\x07\x0e\x0f\x7fd"))[:3] == ["a", " b", "  cd"]


def test_insert_mode():
    s = screen("abcdef\r")
    feed(s, f"{ESC}[4hXY")
    assert rows(s)[0] == "XYabcdef"


# --- cursor movement ----------------------------------------------------------------------------------------------

def test_cup_hvp_defaults_and_clamping():
    s = screen(f"{ESC}[3;5HX{ESC}[HY{ESC}[;3HZ{ESC}[99;99fQ", cols=10, lines=4)
    assert rows(s) == ["Y Z", "", "    X", "         Q"]
    s = screen(f"{ESC}[0;0HA")
    assert rows(s)[0] == "A"


def test_relative_moves_and_defaults():
    s = screen(f"{ESC}[3;3H{ESC}[AU{ESC}[2BD{ESC}[2CR{ESC}[3DL", cols=10, lines=5)
    assert rows(s) == ["", "  U", "", "   DL R", ""]
    s = screen(f"{ESC}[0A{ESC}[0B{ESC}[0C{ESC}[0D")  # a zero count means one
    assert (s.cursor.x, s.cursor.y) == (0, 1)
    s = screen(f"{ESC}[1;1H{ESC}[B{ESC}[B{ESC}[C")
    assert (s.cursor.x, s.cursor.y) == (1, 2)


def test_cnl_cpl_cha_hpa_vpa_vpr_hpr():
    s = screen(f"{ESC}[3;5H{ESC}[2E", cols=10, lines=8)
    assert (s.cursor.x, s.cursor.y) == (0, 4)
    feed(s, f"{ESC}[1F")
    assert (s.cursor.x, s.cursor.y) == (0, 3)
    feed(s, f"{ESC}[7G")
    assert s.cursor.x == 6
    feed(s, f"{ESC}[2`")
    assert s.cursor.x == 1
    feed(s, f"{ESC}[5d")
    assert s.cursor.y == 4
    feed(s, f"{ESC}[2e{ESC}[3a")
    assert (s.cursor.x, s.cursor.y) == (4, 6)
    feed(s, f"{ESC}[99d")
    assert s.cursor.y == 7


def test_decsc_decrc_and_csi_s_u():
    s = screen(f"{ESC}[2;3H{ESC}7{ESC}[5;5Hxx{ESC}8Y", cols=10, lines=6)
    assert rows(s)[1] == "  Y"
    s = screen(f"{ESC}[2;3H{ESC}[s{ESC}[5;5H{ESC}[uZ", cols=10, lines=6)
    assert rows(s)[1] == "  Z"
    s = screen(f"{ESC}[4;4H{ESC}8A")  # nothing saved: home
    assert rows(s)[0] == "A"


# --- erase --------------------------------------------------------------------------------------------------------

def _filled(cols=6, lines=3):
    return screen("".join(c * cols for c in "abc"), cols=cols, lines=lines)


def test_ed_variants():
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[J")
    assert rows(s) == ["aaaaaa", "bb", ""]
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[1J")
    assert rows(s) == ["", "   bbb", "cccccc"]
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[2J")
    assert rows(s) == ["", "", ""] and (s.cursor.x, s.cursor.y) == (2, 1)


def test_el_variants():
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[K")
    assert rows(s)[1] == "bb"
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[1K")
    assert rows(s)[1] == "   bbb"
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[2K")
    assert rows(s)[1] == ""


def test_ech_clamps_and_keeps_cursor():
    s = _filled()
    feed(s, f"{ESC}[2;2H{ESC}[3X")
    assert rows(s)[1] == "b   bb" and s.cursor.x == 1
    feed(s, f"{ESC}[99X")
    assert rows(s)[1] == "b"


def test_decsed_decsel_are_erases():
    s = _filled()
    feed(s, f"{ESC}[2;3H{ESC}[?K")
    assert rows(s)[1] == "bb"
    feed(s, f"{ESC}[?2J")
    assert rows(s) == ["", "", ""]


# --- insert / delete ----------------------------------------------------------------------------------------------

def test_ich_dch():
    s = screen("abcdef", cols=8, lines=2)
    feed(s, f"{ESC}[1;3H{ESC}[2@")
    assert rows(s)[0] == "ab  cdef"
    feed(s, f"{ESC}[1;3H{ESC}[3P")
    assert rows(s)[0] == "abdef"
    feed(s, f"{ESC}[1;1H{ESC}[99@")
    assert rows(s)[0] == ""
    s = screen("abcdef", cols=8, lines=2)
    feed(s, f"{ESC}[1;2H{ESC}[99P")
    assert rows(s)[0] == "a"


def test_il_dl_inside_and_outside_region():
    s = screen("1\r\n2\r\n3\r\n4\r\n5", cols=4, lines=5)
    feed(s, f"{ESC}[2;1H{ESC}[2L")
    assert rows(s) == ["1", "", "", "2", "3"]
    feed(s, f"{ESC}[1;1H{ESC}[2M")
    assert rows(s) == ["", "2", "3", "", ""]
    feed(s, f"{ESC}[2;4r{ESC}[1;1H{ESC}[L")  # cursor outside the region: nothing happens
    assert rows(s) == ["", "2", "3", "", ""]
    feed(s, f"{ESC}[2;2H{ESC}[L")
    assert rows(s) == ["", "", "2", "3", ""]
    assert s.cursor.x == 0  # IL/DL return the carriage


# --- scrolling ----------------------------------------------------------------------------------------------------

def test_linefeed_at_bottom_scrolls_and_records_scrollback():
    s = screen("1\r\n2\r\n3\r\n4", cols=4, lines=3)
    assert rows(s) == ["2", "3", "4"]
    assert list(s.scrolled) == ["1"] and s.pushed == 1


def test_decstbm_region_ind_ri_nel_su_sd():
    s = screen(f"1\r\n2\r\n3\r\n4\r\n5{ESC}[2;4r", cols=4, lines=5)
    assert (s.cursor.x, s.cursor.y) == (0, 0)  # DECSTBM homes the cursor
    feed(s, f"{ESC}[4;1H{ESC}D")  # IND on the region's bottom line
    assert rows(s) == ["1", "3", "4", "", "5"]
    assert s.pushed == 0  # the region does not start at row 0: nothing enters the scrollback
    feed(s, f"{ESC}[2;1H{ESC}M")  # RI on the top line
    assert rows(s) == ["1", "", "3", "4", "5"]
    feed(s, f"{ESC}[4;1H{ESC}E")  # NEL = CR + IND
    assert rows(s) == ["1", "3", "4", "", "5"] and s.cursor.x == 0
    feed(s, f"{ESC}[S")
    assert rows(s) == ["1", "4", "", "", "5"]
    feed(s, f"{ESC}[2T")
    assert rows(s) == ["1", "", "", "4", "5"]
    feed(s, f"{ESC}[r{ESC}[5;1H\n")  # reset margins: scrolls the whole screen again
    assert s.pushed == 1 and rows(s)[0] == ""


def test_su_whole_screen_feeds_scrollback_and_resets_with_bare_decstbm():
    s = screen("a\r\nb\r\nc", cols=4, lines=3)
    feed(s, f"{ESC}[2S")
    assert rows(s) == ["c", "", ""] and list(s.scrolled) == ["a", "b"]
    feed(s, f"{ESC}[1;2r{ESC}[r")
    assert s.margins is None


def test_decom_origin_mode():
    s = screen(f"{ESC}[2;4r{ESC}[?6h{ESC}[1;1HX{ESC}[?6l{ESC}[1;1HY", cols=6, lines=5)
    assert rows(s)[1] == "X" and rows(s)[0] == "Y"
    s = screen(f"{ESC}[2;4r{ESC}[?6h{ESC}[9;1HX", cols=6, lines=5)  # outside the region: the move is ignored
    assert rows(s) == ["", "X", "", "", ""]
    s = screen(f"{ESC}[2;4r{ESC}[?6h{ESC}[2dX", cols=6, lines=5)  # VPA is region-relative too
    assert rows(s)[2] == "X"


def test_3j_clears_scrollback_only():
    s = screen("1\r\n2\r\n3\r\n4\r\nvisible", cols=8, lines=3)
    before = s.pushed
    assert before == 2 and list(s.scrolled) == ["1", "2"]
    feed(s, f"{ESC}[3J")
    assert list(s.scrolled) == [] and s.pushed == before
    assert rows(s) == ["3", "4", "visible"]  # the visible screen is untouched
    feed(s, "\r\nx")
    assert s.pushed == before + 1 and list(s.scrolled) == ["3"]


def test_scrollback_is_bounded():
    s = Screen(5, 2, history=3)
    feed(s, "".join(f"{i}\r\n" for i in range(10)))
    assert len(s.scrolled) == 3 and s.pushed > 3
    assert list(s.scrolled)[-1] == str(s.pushed - 1)


# --- alternate screen ---------------------------------------------------------------------------------------------

def test_alt_screen_1049_restores_main_screen_and_cursor():
    s = screen("main text", cols=12, lines=4)
    feed(s, f"{ESC}[?1049h{ESC}[2J{ESC}[1;1Halt screen")
    assert s.in_alternate_screen and rows(s)[0] == "alt screen"
    feed(s, f"{ESC}[?1049l")
    assert not s.in_alternate_screen and rows(s)[0] == "main text"
    assert (s.cursor.x, s.cursor.y) == (9, 0)


def test_alt_screen_starts_blank_and_does_not_feed_scrollback():
    s = screen("keep", cols=8, lines=2)
    feed(s, f"{ESC}[?1049h")
    assert rows(s) == ["", ""]
    feed(s, "a\r\nb\r\nc\r\nd")
    assert s.pushed == 0 and list(s.scrolled) == []
    feed(s, f"{ESC}[?1049l")
    assert rows(s)[0] == "keep"


@pytest.mark.parametrize("mode", [47, 1047])
def test_alt_screen_47_1047(mode):
    s = screen("m", cols=6, lines=2)
    feed(s, f"{ESC}[?{mode}hx{ESC}[?{mode}l")
    assert rows(s)[0] == "m"


def test_resize_in_alt_screen_resizes_both_grids():
    s = screen("main", cols=8, lines=3)
    feed(s, f"{ESC}[?1049h")
    s.resize(2, 5)
    feed(s, f"{ESC}[?1049l")
    assert (s.lines, s.columns, len(s.buffer), len(s.buffer[0])) == (2, 5, 2, 5)


def test_cursor_visibility_mode():
    s = screen(f"{ESC}[?25l")
    assert s.cursor.hidden
    feed(s, f"{ESC}[?25h")
    assert not s.cursor.hidden


# --- characters ---------------------------------------------------------------------------------------------------

def test_wide_characters_take_two_cells_and_render_once():
    s = screen("a안녕b", cols=10)
    assert s.cursor.x == 6
    assert rows(s)[0] == "a안녕b"
    assert s.buffer[0][2] == "" and s.buffer[0][4] == ""  # stubs
    assert char_width("안") == 2 and char_width("a") == 1 and char_width("́") == 0 and char_width("─") == 1


def test_wide_character_that_does_not_fit_stays_in_the_last_column():
    s = screen("abc안녕", cols=6, lines=3)  # like pyte: no wrap-before for a wide character
    assert rows(s)[0] == "abc안녕" and rows(s)[1] == ""


def test_overwriting_half_of_a_wide_character_blanks_the_other_half():
    s = screen("안녕\r", cols=6)
    feed(s, "x")  # overwrites the left half of 안
    assert rows(s)[0] == "x 녕"
    s = screen("안녕\x1b[1;2H", cols=6)
    feed(s, "x")  # overwrites the right half (the stub)
    assert rows(s)[0] == " x녕"
    s = screen("ab안\x1b[1;2Hxyz", cols=8)  # a wide character overwritten by narrow ones
    assert rows(s)[0] == "axyz"


def test_combining_and_zero_width_characters():
    s = screen("éx")
    assert rows(s)[0] == "éx" and s.cursor.x == 2
    s = screen("a​b️")  # zero width space / variation selector: no cells, nothing lost
    assert rows(s)[0] == "ab" and s.cursor.x == 2
    s = screen("́")  # combining mark with nothing to attach to
    assert rows(s)[0] == "" and s.cursor.x == 0
    s = screen("ab\r\ń", cols=2, lines=3)
    assert s.cursor.x == 0


def test_utf8_split_across_feeds():
    data = "x안y".encode()
    s = Screen(10, 2)
    for b in data:
        s.feed(bytes([b]))
    assert rows(s)[0] == "x안y"
    s = Screen(10, 2)
    s.feed(b"a\xff\xfeb")  # invalid bytes become U+FFFD, never an exception
    assert rows(s)[0].startswith("a") and rows(s)[0].endswith("b")


# --- ignored sequences --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seq", [
    f"{ESC}[1;31m", f"{ESC}[38;2;1;2;3m", f"{ESC}[0m", f"{ESC}]0;title\x07", f"{ESC}]8;;http://x{ESC}\\",
    f"{ESC}P+q4d73{ESC}\\", f"{ESC}_Gi=1,a=q;AAAA{ESC}\\", f"{ESC}^priv{ESC}\\", f"{ESC}Xsos{ESC}\\",
    f"{ESC}(B", f"{ESC})0", f"{ESC}#8", f"{ESC}%G", f"{ESC}=", f"{ESC}>", f"{ESC}[c", f"{ESC}[>c", f"{ESC}[6n",
    f"{ESC}[?6n", f"{ESC}[?996n", f"{ESC}[14t", f"{ESC}[22;0t", f"{ESC}[>4;2m", f"{ESC}[>1u", f"{ESC}[<u",
    f"{ESC}[=1;1u", f"{ESC}[?u", f"{ESC}[ q", f"{ESC}[2 q", f"{ESC}[!p", f"{ESC}[?2026$p", f"{ESC}[>q",
    f"{ESC}[?1004h", f"{ESC}[?2004l", f"{ESC}[?1000h{ESC}[?1006h", f"{ESC} F", f"{ESC}Z", f"{ESC}\\", "\x9b0m",
    f"{ESC}[?25;1h", f"{ESC}[4;0T", f"{ESC}[1;2;3;4;5T",
])
def test_sequences_without_grid_effect_leave_no_text(seq):
    s = screen(f"A{seq}B")
    assert rows(s)[0] == "AB", seq


def test_osc_title_is_delivered_to_set_title():
    s = screen(f"{ESC}]0;my title\x07{ESC}]2;second{ESC}\\{ESC}]1;icon\x07")
    assert s.title == "second" and s.icon_name == "icon"


def test_unterminated_string_is_dropped_after_the_limit():
    s = Screen(10, 2)
    s.feed(f"{ESC}P".encode() + b"x" * ((1 << 20) + 10))
    s.feed(b"visible")
    assert rows(s)[0] == "visible"


def test_esc_inside_a_string_starts_a_new_sequence():
    s = screen(f"{ESC}]0;t{ESC}[2;3Hx")
    assert rows(s)[1] == "  x"


def test_cancel_aborts_sequences():
    assert rows(screen(f"a{ESC}[12\x18b"))[0] == "ab"
    assert rows(screen(f"a{ESC}]0;x\x1ab"))[0] == "ab"
    assert rows(screen(f"a{ESC}\x18b"))[0] == "ab"


def test_c0_inside_csi_executes_and_the_sequence_continues():
    s = screen(f"{ESC}[2\n;4Hx", cols=8, lines=4)  # LF inside CSI, then CUP 2;4
    assert rows(s)[1] == "   x"


def test_8bit_csi_and_osc():
    s = screen("\x9b2;3Hx\x9d0;t\x07y", cols=8, lines=3)
    assert rows(s)[1] == "  xy"


def test_huge_parameters_are_clamped():
    s = screen(f"{ESC}[99999999999;99999999999HX", cols=5, lines=3)
    assert rows(s)[2] == "    X"


def test_unknown_final_bytes_are_ignored():
    assert rows(screen(f"a{ESC}[1;2;3~b{ESC}[?9999zc"))[0] == "abc"


# --- robustness ---------------------------------------------------------------------------------------------------

def test_random_bytes_never_raise_and_keep_invariants():
    rnd = random.Random(1234)
    alphabet = [b"\x1b", b"[", b"]", b"P", b";", b"?", b"<", b"=", b">", b"0", b"1", b"9", b"$", b" ", b"!", b"m",
                b"H", b"J", b"K", b"r", b"h", b"l", b"@", b"L", b"M", b"X", b"S", b"T", b"\r", b"\n", b"\b", b"\t",
                b"a", "안".encode(), "é".encode(), b"\x07", b"\x18", b"\x9b", b"\xff", b"\x00", b"\\", b"7", b"8"]
    for _ in range(60):
        s = Screen(rnd.randint(1, 30), rnd.randint(1, 12), history=50)
        for _ in range(40):
            s.feed(b"".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 60))))
            assert 0 <= s.cursor.x <= s.columns and 0 <= s.cursor.y < s.lines
            assert len(s.buffer) == s.lines and all(len(r) == s.columns for r in s.buffer)
            if rnd.random() < 0.1:
                s.resize(rnd.randint(1, 12), rnd.randint(1, 30))
        assert len(s.display) == s.lines


MIXED = (f"{ESC}[2J{ESC}[H{ESC}[1;31mhello{ESC}[0m 안녕 {ESC}]0;t\x07\r\n{ESC}[3;4Hmid{ESC}7{ESC}[K{ESC}[2Aup"
         f"{ESC}8\r\nline\r\n{ESC}[2;5r{ESC}[5;1H\n\n\nz{ESC}[?1049hALT{ESC}[?1049l{ESC}[3J{ESC}[X{ESC}[2P"
         "é​끝\r\n").encode()


def test_every_split_of_the_input_gives_the_same_screen():
    whole = Screen(30, 8)
    whole.feed(MIXED)
    for cut in range(len(MIXED) + 1):
        s = Screen(30, 8)
        s.feed(MIXED[:cut])
        s.feed(MIXED[cut:])
        assert s.display == whole.display and (s.cursor.x, s.cursor.y) == (whole.cursor.x, whole.cursor.y)
        assert s.pushed == whole.pushed


def test_feed_accepts_str_bytearray_memoryview():
    for chunk in ("abc", b"abc", bytearray(b"abc"), memoryview(b"abc")):
        assert rows(screen(chunk if isinstance(chunk, str) else chunk))[0] == "abc"
    s = Screen(5, 2)
    s.feed(bytearray(b"x"))
    s.feed(memoryview(b"y"))
    assert rows(s)[0] == "xy"


def test_resize_semantics():
    s = screen("1\r\n2\r\n3\r\n4", cols=6, lines=4)
    s.resize(2, 6)  # fewer lines: rows drop from the top, the cursor stays on its text
    assert rows(s) == ["3", "4"] and s.cursor.y == 1
    s.resize(4, 3)
    assert rows(s) == ["3", "4", "", ""] and s.columns == 3
    s.resize(None, 20)
    assert s.columns == 20 and s.tabstops >= {8, 16}
    feed(s, f"{ESC}[2;4r")
    s.resize(5, 20)
    assert s.margins is None


def test_reset_clears_screen_and_modes_but_keeps_scrollback():
    s = screen("1\r\n2\r\n3\r\nx", cols=4, lines=2)
    feed(s, f"{ESC}[?7l{ESC}[2;3r{ESC}c")
    assert rows(s) == ["", ""] and s.margins is None and 7 in s.private_mode
    assert s.pushed >= 1 and list(s.scrolled)


def test_render_helper():
    assert render(f"{ESC}[2J{ESC}[1;1Hhi{ESC}[3;1Hthere".encode(), 10, 5) == "hi\n\nthere"
    assert render(b"", 10, 5) == ""


# --- extension point ----------------------------------------------------------------------------------------------

def test_subclass_overrides_a_handler_and_calls_super():
    class Counting(Screen):
        def __init__(self, *a, **k):
            self.indexes = 0
            super().__init__(*a, **k)

        def index(self):
            self.indexes += 1
            super().index()

    s = Counting(5, 2)
    feed(s, "a\r\nb\r\nc")
    assert s.indexes == 2 and rows(s) == ["b", "c"]
    feed(s, f"{ESC}D")  # ESC D dispatches to the same method
    assert s.indexes == 3


def test_erase_in_display_and_clear_scrollback_hooks():
    seen = []

    class Keep(Screen):
        def erase_in_display(self, how=0, private=False):
            seen.append(how)
            super().erase_in_display(how, private)

        def clear_scrollback(self):
            seen.append("scrollback")

    s = Keep(4, 2)
    feed(s, f"a\r\nb\r\nc{ESC}[3J{ESC}[2J")
    assert seen == [3, "scrollback", 2] and list(s.scrolled) == ["a"]


def test_attribute_tracking_subclass_needs_only_cell_hooks():
    """A colour-tracking terminal overrides make_cell / blank_cell / cell_text / select_graphic_rendition;
    scrolling, inserting and deleting move its opaque cells without any further change."""

    class Coloured(Screen):
        def __init__(self, *a, **k):
            self.fg = 0
            super().__init__(*a, **k)

        def select_graphic_rendition(self, *attrs):
            self.fg = 0 if attrs in ((), (0,)) else attrs[-1]

        def make_cell(self, char):
            return (char, self.fg)

        def blank_cell(self):
            return (" ", 0)

        def stub_cell(self):
            return ("", self.fg)

        def cell_text(self, cell):
            return cell[0]

        def combine_cell(self, cell, mark):
            return (cell[0] + mark, cell[1])

    s = Coloured(6, 3)
    assert not s._fast
    feed(s, f"{ESC}[31mred{ESC}[0m ok\r\n{ESC}[32m안g\r\nz\r\n")  # the last LF scrolls
    assert rows(s)[0] == "안g" and s.buffer[0][0] == ("안", 32) and s.buffer[0][2] == ("g", 32)
    assert s.buffer[0][1] == ("", 32)
    assert list(s.scrolled) == ["red ok"] and s.pushed == 1
    feed(s, f"{ESC}[1;1H{ESC}[2P")  # delete characters moves cells, colours included
    assert s.buffer[0][0] == ("g", 32)
    feed(s, f"{ESC}[1;1He\u0301")  # a combining mark joins the cell and keeps its colour
    assert s.buffer[0][0] == ("e\u0301", 32)


def test_alt_screen_property_and_mode_sets_are_public():
    s = Screen(5, 2)
    assert 7 in s.private_mode and 25 in s.private_mode and s.mode == set()
    feed(s, f"{ESC}[4h{ESC}[?1004h")
    assert 4 in s.mode and 1004 in s.private_mode
