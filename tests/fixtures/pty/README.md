# Golden fixtures for `nexus_lite.pty.screen`

Raw terminal output of agent CLIs (`<name>.bin`) and what pyte renders for it (`<name>.expected.json`).
`tests/test_pty_screen_golden.py` feeds each `.bin` to `nexus_lite.pty.screen.Screen` at three sizes
(200x60, 240x45, 100x24), whole and in random chunks, and compares display rows, cursor, the scrolled-row counter and
the scrolled rows with the expected output.

| fixture | CLI (version) | what it is |
|---|---|---|
| `claude_2196_*`, `claude_2206_*`, `claude_2216_*` | Claude Code 2.1.196 / 2.1.206 / 2.1.216 | three 14 KB windows (stream start, middle, end) of a recorded worker session each: trust/permission dialogs, spinner, tool output, Korean text, box drawing |
| `agy_111_*` | Antigravity CLI 1.1.1 | three windows of a worker session |
| `agy_147` | Antigravity CLI 1.4.7 | a whole turn (prompt, working, answer) |
| `codex` | OpenAI Codex 0.163 | startup (alternate screen, logo animation) |
| `copilot` | GitHub Copilot CLI 1.0.107 | startup (alternate screen, tabs) |
| `gemini` | Gemini CLI | startup, input box, repeated redraws (`ESC[2K ESC[1A`) |
| `opencode` | opencode 2.2.5 | startup and typed input in its alternate-screen TUI (sync-output frames, Korean input) |
| `shell` | bash | echo with CJK, `ls --color`, SGR, 120 lines of output, a long wrapped line, `less` (alternate screen) twice, `clear` |

The Claude Code and Antigravity streams are `script -f` copies of nexus worker sessions of the `nexus` project;
codex, copilot, gemini, opencode and bash were recorded fresh in an empty directory (no model call was made).
`scripts/make_pty_goldens.py cut` cut the windows out (on a newline, never inside an escape sequence or a UTF-8
character) and scrubbed them: e-mail addresses, token-shaped strings and the account name are replaced by same-length
text, so wrapping is unchanged.

## How the expected output is made

`python scripts/make_pty_goldens.py expect tests/fixtures/pty` (needs `pyte` and `wcwidth`, i.e. `pip install -e '.[dev]'`).
The reference is the screen of nexus.code's turn detector: `pyte.Screen` + `ByteStream`, a counter and a deque of the
rows that leave the top of a full-height scroll region (`ESC[3J` ignored, hence the test's `KeepScrollback`), with
three adaptations that keep pyte from mis-rendering what real terminals render correctly:

- pyte has no alternate screen; the reference adds the swap (`?47`/`?1047`/`?1049`: enter blank, leave restores the main
  screen, no scrollback while alternate).
- pyte draws the final byte of `CSI < ... u`, `CSI = ... u` (kitty keyboard protocol) and of CSI sequences with
  `! " # % & ( ) * + , - . /` intermediates as text; the reference input has those sequences removed.
- pyte raises `TypeError` (dropping the rest of the chunk) for `CSI ? ... m`/`n` and other handlers without a `private`
  parameter; the reference drops the flag instead.

## Known, intended differences from pyte

- Overwriting one half of a wide character blanks the other half (every terminal does); pyte leaves an orphan stub cell,
  which shows up as a missing space in the *scrolled* text of such a row (seen in 1 of 28 whole-stream comparisons, at
  100x24, never in the fixtures).
- `ESC[3J` clears the scrollback (the issue's semantics); the reference ignores it, the test overrides
  `Screen.clear_scrollback` to match.
- `char_width` uses `unicodedata` instead of `wcwidth`: 8 of 1.1 million code points differ (five unassigned, U+00AD, U+2028, U+2029).
