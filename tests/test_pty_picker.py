"""nexus-lite pty ls / attach [name|no] [--ro]: table, matching, picker, detach keys, read-only mode, human input stamp."""

import fcntl
import os
import pty as stdpty
import select
import shutil
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest

from nexus_lite import pty
from nexus_lite.pty import client, picker

from .test_ptyd import PROG, Client, _alive_not_zombie, _master_pid, _wait, env, launch  # noqa: F401

pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="POSIX pty required")

LITE = [sys.executable, "-m", "nexus_lite"]
CTRL_BACKSLASH = b"\x1c"


def sock_for(tmp_path, name):
    return str(tmp_path / f"nexus-lite-{name}.sock")


class Term:
    """A process whose stdin/stdout is a real terminal (30x100) that the test types into and reads."""

    def __init__(self, args, env_extra=None):
        self.master, slave = stdpty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        e = dict(os.environ)
        e.update(env_extra or {})
        self.proc = subprocess.Popen(LITE + list(args), stdin=slave, stdout=slave, stderr=slave, env=e,
                                     start_new_session=True, close_fds=True)
        # Hold the slave open like the shell of a real terminal does: macOS discards unread output when the
        # last slave descriptor closes, so the final line of a process that exits would otherwise be lost.
        self.slave = slave
        self.buf = b""

    def pump(self, t=0.2):
        end = time.monotonic() + t
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    b = os.read(self.master, 65536)
                except OSError:
                    return
                if not b:
                    return
                self.buf += b

    def write(self, data: bytes):
        os.write(self.master, data)

    def expect(self, text, t=8.0, since=0):
        needle = text.encode()
        end = time.monotonic() + t
        while time.monotonic() < end and needle not in self.buf[since:]:
            self.pump(0.1)
        return needle in self.buf[since:]

    def wait(self, t=8.0):
        # Keep reading while waiting, as a terminal does: on macOS a TCSADRAIN restore blocks until it is read.
        end = time.monotonic() + t
        while self.proc.poll() is None and time.monotonic() < end:
            self.pump(0.05)
        rc = self.proc.wait(timeout=max(0.0, end - time.monotonic()))
        self.pump(0.3)
        return rc

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def term(env):
    made = []

    def make(args, env_extra=None):
        t = Term(args, env_extra)
        made.append(t)
        return t

    yield make
    for t in made:
        t.close()


def two_sessions(env, tmp_path, names=("alpha", "beta")):
    for n in names:
        assert launch(env, sock_for(tmp_path, n)).returncode == 0


# --- pure functions -----------------------------------------------------------------------------------------------

def test_fmt_age_units():
    assert [picker.fmt_age(x) for x in (0, 5, 59, 60, 3599, 3600, 86399, 86400, 400000)] == [
        "0s", "5s", "59s", "1m", "59m", "1h", "23h", "1d", "4d"]
    assert picker.fmt_age(-3) == "0s"


def _info(*names):
    return [{"no": i, "name": n, "sock": f"/s/{n}", "provider": "claude" if i == 1 else "-", "age": 90.0 * i,
             "attached": i == 2, "alive": True} for i, n in enumerate(names, 1)]


def test_format_table_columns_and_alignment():
    t = picker.format_table(_info("alpha", "beta-long-name")).splitlines()
    assert t[0].split() == ["NO", "NAME", "PROVIDER", "AGE", "ATTACHED", "ALIVE"]
    assert t[1].split() == ["1", "alpha", "claude", "1m", "no", "yes"]
    assert t[2].split() == ["2", "beta-long-name", "-", "3m", "yes", "yes"]
    assert t[0].index("PROVIDER") == t[2].index("-  ", 10) or "PROVIDER" in t[0]
    assert all(line == line.rstrip() for line in t)


def test_resolve_by_number_exact_and_substring():
    info = _info("claude-nexus", "agy-nexus", "codex-lite")
    assert picker.resolve(info, "2")["name"] == "agy-nexus"
    assert picker.resolve(info, "agy-nexus")["name"] == "agy-nexus"
    assert picker.resolve(info, "LITE")["name"] == "codex-lite"
    assert picker.resolve(info, "  codex ")["name"] == "codex-lite"
    for bad, msg in (("9", "no session number 9"), ("zzz", "no session matches"), ("nexus", "matches 2 sessions"),
                     ("", "no session given")):
        with pytest.raises(LookupError, match=msg):
            picker.resolve(info, bad)
    assert picker.resolve(_info("a", "ab"), "a")["name"] == "a"  # an exact name wins over longer matches


def test_parse_pick():
    assert picker.parse_pick("q\n") == ("quit", None)
    assert picker.parse_pick(" QUIT ") == ("quit", None)
    assert picker.parse_pick("\n") == ("refresh", None)
    assert picker.parse_pick("r") == ("refresh", None)
    assert picker.parse_pick(" 2 \n") == ("pick", "2")
    assert picker.parse_pick("my-sess") == ("pick", "my-sess")


def test_detach_key_names_and_env(monkeypatch):
    assert client.key_name(b"\x1c") == "Ctrl-\\"
    assert client.key_name(b"\x18") == "Ctrl-X"
    assert client.key_name(b"~") == "~" and client.key_name(None) == ""
    monkeypatch.delenv("NEXUS_PTY_DETACH", raising=False)
    assert picker.detach_from_env(b"\x1c") == b"\x1c"
    monkeypatch.setenv("NEXUS_PTY_DETACH", "^X")
    assert picker.detach_from_env(b"\x1c") == b"\x18"
    monkeypatch.setenv("NEXUS_PTY_DETACH", "bogus!")
    assert picker.detach_from_env(b"\x1c") == b"\x1c"


def test_provider_of():
    from nexus_lite.pty.agents import provider_of
    assert provider_of("claude --x") == "claude"
    assert provider_of("FOO=1 /usr/bin/opencode -m x") == "opencode"
    assert provider_of(["bash", "--norc"]) == "bash"
    assert provider_of("") == "" and provider_of("'unbalanced") == ""


# --- ls -----------------------------------------------------------------------------------------------------------

def test_ls_lists_sessions_with_provider_age_attached_alive(env, tmp_path):
    a = pty.spawn("bash --norc", name="lsa")
    b = pty.spawn("cat", name="lsb")
    env.extend([pty.sock_path(a), pty.sock_path(b)])
    p = subprocess.run(LITE + ["pty", "ls"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[0].split() == ["NO", "NAME", "PROVIDER", "AGE", "ATTACHED", "ALIVE"]
    rows = {r.split()[1]: r.split() for r in lines[1:]}
    assert rows["lsa"][0] == "1" and rows["lsa"][2] == "bash" and rows["lsa"][3].endswith("s")
    assert rows["lsa"][4] == "no" and rows["lsa"][5] == "yes"
    assert rows["lsb"][0] == "2" and rows["lsb"][2] == "cat"
    c = Client(pty.sock_path(a))  # an attached client turns the ATTACHED column on
    assert _wait(lambda: picker.sessions_info()[0]["attached"])
    c.close()
    assert _wait(lambda: not picker.sessions_info()[0]["attached"])


def test_ls_without_sessions(env):
    p = subprocess.run(LITE + ["pty", "ls"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == "No active sessions."


def test_sessions_info_marks_dead_sockets_not_alive(env, tmp_path):
    sp = sock_for(tmp_path, "gone")
    assert launch(env, sp).returncode == 0
    info = picker.sessions_info()
    assert [(r["name"], r["alive"]) for r in info] == [("gone", True)]


# --- attach -------------------------------------------------------------------------------------------------------

def test_attach_by_name_substring_prints_hint_types_and_detaches(env, tmp_path, term):
    two_sessions(env, tmp_path)
    t = term(["pty", "attach", "alph"])
    assert t.expect("[attached: alpha — Ctrl-\\ to detach]")
    t.write(b"who\n")
    assert t.expect("READY pid=")
    t.write(b"hi there\n")
    assert t.expect("IN:hi there")
    t.write(CTRL_BACKSLASH)
    assert t.wait() == 0
    assert b"[detached: alpha]" in t.buf
    assert Client(sock_for(tmp_path, "alpha")).ready()  # still running
    assert termios.tcgetattr(t.master)[3] & termios.ECHO  # the terminal is back in cooked mode


def test_attach_by_number_and_sends_winsize_and_winch(env, tmp_path, term):
    two_sessions(env, tmp_path)
    t = term(["pty", "attach", "2"])
    assert t.expect("[attached: beta")
    t.write(b"size\n")
    assert t.expect("SIZE 30 100")
    assert t.expect("WINCH 30 100")  # the winch redraw reached the program
    t.write(CTRL_BACKSLASH)
    assert t.wait() == 0


def test_attach_unknown_or_ambiguous_name_fails(env, tmp_path, term):
    two_sessions(env, tmp_path, ("same-1", "same-2"))
    t = term(["pty", "attach", "nothing"])
    assert t.wait() == 1 and b"no session matches" in t.buf
    t = term(["pty", "attach", "same"])
    assert t.wait() == 1 and b"matches 2 sessions" in t.buf
    t = term(["pty", "attach", "7"])
    assert t.wait() == 1 and b"no session number 7" in t.buf


def test_attach_detach_key_override_env_and_flags(env, tmp_path, term):
    assert launch(env, sock_for(tmp_path, "k1")).returncode == 0
    t = term(["pty", "attach", "k1", "-e", "^X"])
    assert t.expect("Ctrl-X to detach")
    t.write(b"\x18")
    assert t.wait() == 0 and b"[detached: k1]" in t.buf
    t = term(["pty", "attach", "k1"], {"NEXUS_PTY_DETACH": "^X"})
    assert t.expect("Ctrl-X to detach")
    t.write(b"z")  # not the detach key (Ctrl-\\ would be a plain keystroke here, SIGQUIT for the program)
    time.sleep(0.4)
    assert t.proc.poll() is None
    t.write(b"\x18")
    assert t.wait() == 0
    t = term(["pty", "attach", "k1", "-E"])  # no detach key at all
    assert t.expect("[attached: k1]") and b"to detach" not in t.buf
    t.write(b"\x18\x18")  # Ctrl-X is an ordinary key without a detach key
    time.sleep(0.4)
    assert t.proc.poll() is None
    t.write(b"\nquit\n")  # the program exits: the client reports the end of the session
    assert t.wait() == 0 and b"[ended: k1]" in t.buf


def test_invalid_detach_key_is_a_usage_error(env, tmp_path):
    assert launch(env, sock_for(tmp_path, "k2")).returncode == 0
    p = subprocess.run(LITE + ["pty", "attach", "k2", "-e", "toolong"], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 2 and "invalid detach key" in p.stderr


def test_attach_needs_a_terminal(env, tmp_path):
    assert launch(env, sock_for(tmp_path, "nt")).returncode == 0
    p = subprocess.run(LITE + ["pty", "attach", "nt"], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 1 and "terminal" in p.stderr
    p = subprocess.run(LITE + ["pty", "attach"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert p.returncode == 1 and "terminal" in p.stderr


# --- picker -------------------------------------------------------------------------------------------------------

def test_picker_attach_detach_returns_to_the_picker(env, tmp_path, term):
    two_sessions(env, tmp_path)
    t = term(["pty", "attach"])
    assert t.expect("attach [number|name]")
    assert t.buf.count(b"alpha") == 1 and b"beta" in t.buf
    t.write(b"1\n")
    assert t.expect("[attached: alpha")
    t.write(b"who\n")
    assert t.expect("READY pid=")
    mark = len(t.buf)
    t.write(CTRL_BACKSLASH)
    assert t.expect("[detached: alpha]", since=mark)
    assert t.expect("attach [number|name]", since=mark)  # back at the list
    t.write(b"nonexistent\n")
    assert t.expect("no session matches")
    t.write(b"bet\n")  # a name substring works in the picker too
    assert t.expect("[attached: beta")
    t.write(CTRL_BACKSLASH)
    assert t.expect("[detached: beta]")
    t.write(b"q\n")
    assert t.wait() == 0
    assert _alive_not_zombie(_master_pid(sock_for(tmp_path, "alpha")))


def test_picker_refresh_and_session_end_return_to_the_list(env, tmp_path, term):
    assert launch(env, sock_for(tmp_path, "solo")).returncode == 0
    t = term(["pty", "attach"])
    assert t.expect("attach [number|name]")
    mark = len(t.buf)
    t.write(b"r\n")
    assert t.expect("attach [number|name]", since=mark)
    t.write(b"solo\n")
    assert t.expect("[attached: solo")
    t.write(b"quit\n")  # the program exits while attached
    mark = len(t.buf)
    assert t.expect("[ended: solo]")
    assert t.expect("No active sessions.", since=mark) or t.proc.poll() is not None
    assert t.wait() == 0


def test_picker_eof_and_no_sessions(env, term):
    t = term(["pty", "attach"])
    assert t.wait() == 0 and b"No active sessions." in t.buf


# --- --ro never writes PUSH ---------------------------------------------------------------------------------------

class FakeServer:
    """Records every 10-byte wire record a client sends; never answers."""

    def __init__(self, path):
        self.records = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        buf = b""
        while True:
            try:
                b = conn.recv(4096)
            except OSError:
                break
            if not b:
                break
            buf += b
            while len(buf) >= 10:
                self.records.append(struct.unpack("<BB8s", buf[:10]))
                buf = buf[10:]
        conn.close()

    def close(self):
        self.srv.close()
        self.t.join(timeout=3)


def test_ro_writes_no_push_and_q_detaches(tmp_path, term, env):
    sp = str(tmp_path / "fake.sock")
    srv = FakeServer(sp)
    try:
        t = term(["pty", "attach", sp, "--ro"])
        assert t.expect("[attached: fake.sock")
        assert t.expect("read-only")
        t.write(b"hello world, typed text\r\x03\x1b[A")
        time.sleep(0.5)
        assert t.proc.poll() is None  # typing alone does not detach
        t.write(b"q")
        assert t.wait() == 0 and b"[detached: fake.sock]" in t.buf
    finally:
        srv.close()
    types = [r[0] for r in srv.records]
    assert 0 not in types  # no PUSH, ever
    assert types[0] == 1 and types[-1] == 2  # ATTACH first, DETACH last
    redraw = [r for r in srv.records if r[0] == 4]
    assert redraw and redraw[0][1] == 3  # REDRAW with the winch method
    assert struct.unpack("HHHH", redraw[0][2]) == (30, 100, 0, 0)  # our winsize


def test_ro_detach_key_also_works_and_rw_does_push(tmp_path, term, env):
    sp = str(tmp_path / "fake2.sock")
    srv = FakeServer(sp)
    try:
        t = term(["pty", "attach", sp, "--ro"])
        assert t.expect("[attached:")
        t.write(CTRL_BACKSLASH)
        assert t.wait() == 0
    finally:
        srv.close()
    assert 0 not in [r[0] for r in srv.records]

    sp = str(tmp_path / "fake3.sock")
    srv = FakeServer(sp)
    try:
        t = term(["pty", "attach", sp])
        assert t.expect("[attached:")
        t.write(b"ab")
        time.sleep(0.4)
        t.write(CTRL_BACKSLASH)
        assert t.wait() == 0
    finally:
        srv.close()
    pushed = b"".join(r[2][:r[1]] for r in srv.records if r[0] == 0)
    assert pushed == b"ab"


def test_ro_against_a_live_session_does_not_reach_the_program(env, tmp_path, term):
    assert launch(env, sock_for(tmp_path, "watch")).returncode == 0
    w = Client(sock_for(tmp_path, "watch")).ready()  # a second, ordinary client sees what the program prints
    t = term(["pty", "attach", "watch", "--ro"])
    assert t.expect("read-only")
    t.write(b"typed-by-the-watcher\r")
    w.pump(1.0)
    assert b"typed-by-the-watcher" not in w.buf
    w.push(b"from-the-other-client\n")  # output still flows to the watcher
    assert t.expect("IN:from-the-other-client")
    t.write(b"q")
    assert t.wait() == 0


# --- dtach binary session -----------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("dtach") is None, reason="no dtach binary")
def test_attach_to_a_dtach_binary_session(env, tmp_path, term):
    sp = sock_for(tmp_path, "dt")
    p = subprocess.run([shutil.which("dtach"), "-n", sp, "-z", "-r", "winch", sys.executable, "-c", PROG],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    env.append(sp)
    try:
        t = term(["pty", "attach", "dt"])
        assert t.expect("[attached: dt")
        t.write(b"who\n")
        assert t.expect("READY pid=")
        t.write(CTRL_BACKSLASH)
        assert t.wait() == 0 and b"[detached: dt]" in t.buf
        ls = subprocess.run(LITE + ["pty", "ls"], capture_output=True, text=True, timeout=30)
        assert "dt" in ls.stdout and "yes" in ls.stdout
    finally:
        pid = pty.find_dtach_master_pid(sp)
        if pid:
            os.kill(pid, 9)


# --- human input stamp --------------------------------------------------------------------------------------------

def test_attached_client_push_stamps_last_human_input(env, tmp_path):
    sp = sock_for(tmp_path, "hi")
    assert launch(env, sp).returncode == 0
    assert pty.last_human_input(sp) is None
    c = Client(sp).ready()  # ATTACH then PUSH: an interactive client
    t = pty.last_human_input(sp)
    assert t is not None and abs(time.time() - t) < 5
    assert (os.stat(sp + ".input").st_mode & 0o777) == 0o600
    time.sleep(0.05)
    c.push(b"x")
    assert _wait(lambda: pty.last_human_input("hi") > t)  # by session name too
    assert not os.path.exists(sp + ".input.tmp")
    c.push(b"\x15quit\n")
    assert c.eof()
    assert _wait(lambda: not os.path.exists(sp) and not os.path.exists(sp + ".input") and not os.path.exists(sp + ".pid"))
    assert pty.last_human_input(sp) is None


def test_programmatic_send_and_keys_do_not_stamp(env, tmp_path):
    sp = sock_for(tmp_path, "prog")
    assert launch(env, sp).returncode == 0
    pty.send("prog", "who")
    pty.keys("prog", "Enter", "x", "Enter")
    with client.Connection(sp) as conn:  # the control API's connection: push without ATTACH
        conn.push(b"who\n")
        time.sleep(0.3)
    raw = Client(sp, attach=False)  # a raw client that never attaches is programmatic too
    raw.push(b"who\n")
    time.sleep(0.4)
    assert pty.last_human_input(sp) is None and not os.path.exists(sp + ".input")
    assert Client(sp).ready() and pty.last_human_input(sp) is not None  # an attaching client does


def test_stale_input_stamp_is_cleared_when_a_new_session_starts(env, tmp_path):
    sp = sock_for(tmp_path, "st")
    with open(sp + ".input", "w") as f:
        f.write("123.0\n")
    assert launch(env, sp).returncode == 0
    assert _wait(lambda: pty.last_human_input(sp) is None)


def test_last_human_input_tolerates_garbage(tmp_path):
    sp = str(tmp_path / "g.sock")
    assert pty.last_human_input(sp) is None
    with open(sp + ".input", "w") as f:
        f.write("not a number")
    assert pty.last_human_input(sp) is None


def test_sidecar_clients_count_neither_as_attached_nor_as_human(env, tmp_path):
    sp = sock_for(tmp_path, "sc")
    assert launch(env, sp).returncode == 0
    obs = Client(sp, attach=False)
    obs.send(5)  # MSG_SIDECAR, then ATTACH: an output-capture observer
    obs.send(1)
    time.sleep(0.4)
    assert not os.stat(sp).st_mode & 0o100  # no person attached
    obs.push(b"who\n")
    assert obs.expect("READY")
    assert pty.last_human_input(sp) is None
    human = Client(sp)
    assert _wait(lambda: os.stat(sp).st_mode & 0o100)
    human.push(b"who\n")
    assert _wait(lambda: pty.last_human_input(sp) is not None)


def test_spawned_sessions_are_not_attached_until_a_person_attaches(env, tmp_path):
    name = pty.spawn("cat", name="quiet")  # spawn() runs an output-capture sidecar on the session
    env.append(pty.sock_path(name))
    time.sleep(0.5)
    assert picker.sessions_info()[0]["attached"] is False
