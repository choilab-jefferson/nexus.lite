"""
nexus_lite.pty.agents — agent command normalization for PTY sessions (stdlib only).

Turns a bare agent invocation (``claude``, ``agy``, ``copilot``, ``gemini``, ``opencode`` or ``opencode/<model>``)
into the command line a coordinator should run: auto-approve flags plus the screen-reader / declutter flags that keep
TUI animation noise off the screen the orchestrator reads; opencode is pointed at its zero-signup free models.
Kept out of the PTY core: the session server and client know nothing about agents.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Optional, Union

DEFAULT_OPENCODE_MODEL = "opencode/big-pickle"
OPENCODE_FREE_MODELS = (
    "opencode/big-pickle",
    "opencode/mimo-v2.6-flash-free",
    "opencode/glm-5.3-flash-free",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/beam-free",
    "opencode/flint-40b-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/nemotron-3.5-super-vl-free",
)


def _is_exec_file(p: Union[str, Path]) -> bool:
    """Return True if p is an existing executable file, ignoring PermissionError on restricted paths."""
    try:
        return os.path.isfile(str(p)) and os.access(str(p), os.X_OK)
    except OSError:
        return False


def find_opencode_bin() -> Optional[str]:
    """Locate the opencode CLI binary, preferring ~/.opencode/bin (v2 installer) over global PATH."""
    for candidate in (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path("/root/.opencode/bin/opencode"),
    ):
        if _is_exec_file(candidate):
            return str(candidate)
    found = shutil.which("opencode")
    if found:
        return found
    if _is_exec_file(Path("/usr/local/bin/opencode")):
        return "/usr/local/bin/opencode"
    return None


def _parse_opencode_model(cmd: Union[str, list[str]]) -> Optional[str]:
    """
    If cmd specifies an opencode agent invocation, return the target model ID
    (defaulting to DEFAULT_OPENCODE_MODEL = 'opencode/big-pickle').
    Otherwise return None.
    """
    if isinstance(cmd, list):
        if not cmd:
            return None
        first = os.path.basename(cmd[0].strip())
        if first != "opencode":
            return None
        for i, tok in enumerate(cmd[1:], start=1):
            if tok in ("-m", "--model") and i + 1 < len(cmd):
                return cmd[i + 1]
            if tok.startswith("--model="):
                return tok.split("=", 1)[1]
        return DEFAULT_OPENCODE_MODEL

    if isinstance(cmd, str):
        stripped = cmd.strip()
        if stripped == "opencode":
            return DEFAULT_OPENCODE_MODEL
        if stripped.startswith("opencode/"):
            # Shorthand like "opencode/big-pickle" or "opencode/mimo-v2.6-flash-free"
            if stripped == "opencode/minimax-m2.5-free":
                return "opencode/mimo-v2.6-flash-free"
            return stripped
        if stripped.startswith("opencode:"):
            model_part = stripped.split(":", 1)[1].strip()
            return model_part if "/" in model_part else f"opencode/{model_part}"
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        if parts and os.path.basename(parts[0]) == "opencode":
            # Only normalize if not already a raw 'opencode run ...' command with a message
            if len(parts) >= 2 and parts[1] == "run":
                return None
            for i, tok in enumerate(parts[1:], start=1):
                if tok in ("-m", "--model") and i + 1 < len(parts):
                    return parts[i + 1]
                if tok.startswith("--model="):
                    return tok.split("=", 1)[1]
            return DEFAULT_OPENCODE_MODEL
    return None


CLAUDE_DECLUTTER_SETTINGS: dict[str, object] = {
    "prefersReducedMotion": True,
    "spinnerTipsEnabled": False,
    "terminalProgressBarEnabled": False,
}
AGY_DECLUTTER_SETTINGS: dict[str, object] = {
    "altScreenMode": False,
    "showTips": False,
    "showFeedbackSurvey": False,
    "artifactReviewPolicy": "always-proceed",
}
COPILOT_SCREEN_READER_ARGS: tuple[str, ...] = (
    "--screen-reader",
    "--plain-diff",
    "--no-mouse",
    "--no-auto-update",
)
GEMINI_SCREEN_READER_ARGS: tuple[str, ...] = ("--screen-reader",)

_AGENT_BINS = ("claude", "agy", "copilot", "gemini")


def _settings_arg(settings: dict[str, object]) -> str:
    return shlex.quote(json.dumps(settings, separators=(",", ":")))


def _normalize_known_agent(
    cmd: Union[str, list[str]], mode: str, prompt: Optional[str], declutter: bool
) -> Optional[str]:
    """Build the command for claude/agy/copilot/gemini, or None if `cmd` is not one of them."""
    try:
        toks = list(cmd) if isinstance(cmd, list) else shlex.split(cmd)
    except ValueError:
        return None
    if not toks or toks[0] not in _AGENT_BINS:
        return None
    bin_ = toks[0]
    has = set(toks)
    out = list(toks)

    def add(*flags: str) -> None:
        for f in flags:
            if f not in has:
                out.append(f)
                has.add(f)

    extra: list[str] = []
    if bin_ in ("claude", "agy"):
        if bin_ == "agy" or "--permission-mode" not in has:
            add("--dangerously-skip-permissions")
        if declutter:
            if bin_ == "claude":
                if os.environ.get("NEXUS_CLAUDE_SCREEN_READER") != "0":
                    add("--ax-screen-reader")
                if "--settings" not in has:
                    extra += ["--settings", _settings_arg(CLAUDE_DECLUTTER_SETTINGS)]
            elif "--settings" not in has:
                extra += ["--settings", _settings_arg(AGY_DECLUTTER_SETTINGS)]
    elif bin_ == "copilot":
        add("--allow-all-tools")
        if declutter and os.environ.get("NEXUS_COPILOT_SCREEN_READER") != "0":
            add(*COPILOT_SCREEN_READER_ARGS)
    else:  # gemini
        if "--yolo" not in has:
            add("-y")
        if (declutter and mode == "pty" and os.environ.get("NEXUS_GEMINI_SCREEN_READER") != "0"):
            add(*GEMINI_SCREEN_READER_ARGS)
    line = " ".join([shlex.quote(t) for t in out] + extra)
    if mode != "pty" and prompt is not None:
        line += " -p " + shlex.quote(prompt)
    return line


def normalize_agent_cmd(
    cmd: Union[str, list[str]],
    mode: str = "pty",
    prompt: Optional[str] = None,
    declutter: Optional[bool] = None,
) -> Union[str, list[str]]:
    """
    Normalize agent command for pty or oneshot execution.
    - claude/agy/copilot/gemini get their auto-approve flag; in pty mode (declutter default on,
      opt out with NEXUS_LITE_PTY_DECLUTTER=0) also screen-reader / declutter flags so TUI
      animations, borders and alt-screen noise do not reach the orchestrator.
      Oneshot mode appends "-p <prompt>" and declutters only if declutter=True.
    - Expands "opencode" (or "opencode -m <model>" / "opencode/<model>") to use
      OpenCode's built-in zero-signup free models (default: opencode/big-pickle) via
      "opencode run --standalone --auto -m <model> <prompt>" in oneshot mode.
    """
    if declutter is None:
        declutter = mode == "pty" and os.environ.get("NEXUS_LITE_PTY_DECLUTTER", "1") != "0"
    known = _normalize_known_agent(cmd, mode, prompt, declutter)
    if known is not None:
        return known

    opencode_model = _parse_opencode_model(cmd)
    if opencode_model is not None:
        oc_bin = shlex.quote(find_opencode_bin() or "opencode")
        q_model = shlex.quote(opencode_model)
        if mode == "pty":
            return f"{oc_bin} -m {q_model}"
        if prompt is not None:
            return f"{oc_bin} run --standalone --auto -m {q_model} {shlex.quote(prompt)}"
        return f"{oc_bin} run --standalone --auto -m {q_model}"

    return cmd




KNOWN_PROVIDERS = ("claude", "agy", "copilot", "gemini", "codex", "opencode", "hermes", "bash", "zsh", "sh")


def provider_of(cmd: Union[str, list]) -> str:
    """Short provider label of a command line: the basename of its program (``claude``, ``agy``, ``opencode``,
    ``bash``, ...), skipping leading ``VAR=value`` words. ``""`` when there is none."""
    try:
        toks = list(cmd) if isinstance(cmd, list) else shlex.split(str(cmd))
    except ValueError:
        return ""
    for t in toks:
        if "=" in t and not t.startswith("-") and "/" not in t.split("=", 1)[0]:
            continue
        base = os.path.basename(t)
        return base if base in KNOWN_PROVIDERS else base[:24]
    return ""
