"""Group-chat coordinator checks against a controllable fake Herdr; no model is invoked."""
import fcntl
import importlib.util
import itertools
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata
import unittest

# The installed module (src/ is not on sys.path), run as a script like the agents' generated commands.
SCRIPT = Path(importlib.util.find_spec("herdr_group_chat").origin).resolve()
PANES = {"codex": "w9:p1", "claude": "w9:p2"}
AGENT_ORDER = ("codex", "claude")

# Fake Herdr: identities in agents.json, status per pane in status-*; `agent prompt --wait`
# records the prompt, marks the pane working, and settles only when the test writes finish-*.
# `agent prompt` without --wait (an update) records and returns at once; on a settled pane it
# starts a new turn that `agent get`/`wait` still report as settled for 0.4 s (lag-*), as
# measured natively. stall-* holds every wait's settled answer until the test removes it.
# A native resume (`codex resume ID`, `claude --resume ID`) keeps that id; Codex reports it (its `reveal`) only when
# a prompt starts its first turn, as observed natively; resume-missing-* makes a resume fail.
# `agent send-keys` records each key in keys.jsonl; Esc stops a working pane unless esc-ignored-* exists.
# slow-update delays every update write (prompt without --wait) by 1 s.
FAKE_HERDR = r'''
import fcntl, json, os, sys, time
from pathlib import Path
D = Path(os.environ["FAKE_HERDR_DIR"])
def key(p): return p.replace(":", "_")
def info(p):
    a = dict(json.loads((D / "agents.json").read_text())[p])
    status, lag = (D / f"status-{key(p)}").read_text(), D / f"lag-{key(p)}"
    if lag.exists():
        until, before = json.loads(lag.read_text())
        if time.time() < until:
            status = before
        else:
            lag.unlink(missing_ok=True)
    a.update(pane_id=p, agent_status=status)
    return a
def settle(p, timeout_ms, until=None):
    end = time.time() + timeout_ms / 1000
    f, seen = D / f"finish-{key(p)}", None
    while time.time() < end:
        if p not in json.loads((D / "agents.json").read_text()):  # pane closed under the wait
            print(json.dumps({"error": {"code": "agent_not_found", "message": "pane closed"}}), file=sys.stderr)
            return 1
        if f.exists():
            try:
                (D / f"status-{key(p)}").write_text(f.read_text()); f.unlink()
            except FileNotFoundError:  # another waiter took it
                pass
        now = info(p)
        if seen is None and (now["agent_status"] in until if until else now["agent_status"] != "working"):
            seen = now
        if seen is not None and not (D / f"stall-{key(p)}").exists():
            print(json.dumps({"result": {"agent": seen}})); return 0
        time.sleep(0.02)
    print(json.dumps({"error": {"code": "timeout", "message": "timed out waiting for agent status"}}), file=sys.stderr)
    return 1
a = sys.argv[1:]
timeout = int(a[a.index("--timeout") + 1]) if "--timeout" in a else 60000
with (D / "calls.jsonl").open("a") as f:
    f.write(json.dumps(a) + "\n")
def ok(result): print(json.dumps({"result": result})); sys.exit(0)
def new_pane():
    n = len(list(D.glob("new-*"))) + 1
    (D / f"new-{n}").write_text(""); return f"w9:n{n}"
if a[:2] == ["pane", "get"]:
    agents = json.loads((D / "agents.json").read_text())
    ok({"pane": {"pane_id": a[2], "tab_id": "w9:t1", **({"agent": agents[a[2]]["agent"]} if a[2] in agents else {})}})
if a[:2] == ["pane", "process-info"] and a[3] not in json.loads((D / "agents.json").read_text()):
    pgid = 1 if (D / "not-foreground").exists() else os.getpgrp()  # the launcher's group
    ok({"process_info": {"foreground_process_group_id": pgid}})
if a[:2] == ["tab", "get"]:
    ok({"tab": {"tab_id": a[2], "pane_count": int((D / "pane-count").read_text()),
                "focused": (D / "tab-focused").exists()}})
if a[:2] == ["pane", "split"]:
    ok({"pane": {"pane_id": new_pane(), "tab_id": "w9:t1"}})
if a[:2] == ["pane", "move"]:  # the agent keeps its identity under the new pane id
    agents, new = json.loads((D / "agents.json").read_text()), new_pane()
    agents[new] = agents.pop(a[2]); (D / "agents.json").write_text(json.dumps(agents))
    (D / f"status-{key(new)}").write_text((D / f"status-{key(a[2])}").read_text())
    ok({"move_result": {"pane": {"pane_id": new}}})
if a[:2] in (["pane", "swap"], ["pane", "run"], ["pane", "focus"]):
    ok({"type": "ok"})
if a[:2] == ["pane", "layout"]:
    ok({"layout": json.loads((D / "layout.json").read_text())})
if a[:2] == ["agent", "start"]:
    pane, kind = a[a.index("--pane") + 1], a[a.index("--kind") + 1]
    if (D / f"start-fail-{kind}").exists():
        print(json.dumps({"error": {"code": "agent_not_ready", "message": "blocked"}}), file=sys.stderr); sys.exit(1)
    native = a[a.index("--") + 1:] if "--" in a else []
    resumed = native[1] if native[:1] == ["resume"] else native[native.index("--resume") + 1] if "--resume" in native else None
    if resumed and (D / f"resume-missing-{kind}").exists():
        print(json.dumps({"error": {"code": "agent_not_ready", "message": "agent exited"}}), file=sys.stderr); sys.exit(1)
    session = (D / f"resume-as-{kind}").read_text() if (D / f"resume-as-{kind}").exists() else resumed
    with (D / "start.lock").open("a") as lock:  # both agents start in parallel: one agents.json update at a time
        fcntl.flock(lock, fcntl.LOCK_EX)
        agents = json.loads((D / "agents.json").read_text())
        agents[pane] = {"agent": kind, "agent_session": {"value": f"sess-{kind}"}, "terminal_id": f"term-{kind}",
                        "workspace_id": "w9", "tab_id": "w9:t1", "cwd": os.environ["FAKE_CWD"]}
        if resumed:
            agents[pane]["agent_session"] = {"value": None if kind == "codex" else session}
            agents[pane]["reveal"] = session
        (D / "agents.json").write_text(json.dumps(agents)); (D / f"status-{key(pane)}").write_text("idle")
    ok({"agent": info(pane)})
if a[:2] == ["agent", "get"] and (D / "slow-get").exists():  # holds a dispatch before it sends
    time.sleep(1.5)
if a[:2] == ["agent", "get"] and (D / "bad-get").exists():  # unexpected result shape
    print(json.dumps({"result": {"agent": None}})); sys.exit(0)
if a[:2] == ["agent", "get"] and a[2] not in json.loads((D / "agents.json").read_text()):
    print(json.dumps({"error": {"code": "agent_not_found", "message": f"agent target {a[2]} not found"}}), file=sys.stderr)
    sys.exit(1)
if a[:2] == ["pane", "close"]:
    if (D / "close-fail").exists():
        print(json.dumps({"error": {"code": "internal", "message": "close failed"}}), file=sys.stderr); sys.exit(1)
    agents = json.loads((D / "agents.json").read_text())
    agents.pop(a[2], None); (D / "agents.json").write_text(json.dumps(agents))
    ok({"type": "ok"})
if a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": info(a[2])}})); sys.exit(0)
if a[:2] == ["pane", "process-info"]:
    pgid = json.loads((D / "agents.json").read_text())[a[3]].get("pgid", 100)
    print(json.dumps({"result": {"process_info": {"foreground_process_group_id": pgid}}})); sys.exit(0)
if a[:2] == ["agent", "list"]:
    ok({"agents": [info(p) for p in json.loads((D / "agents.json").read_text())]})
if a[:2] == ["agent", "send-keys"]:
    status = info(a[2])["agent_status"]
    with (D / "keys.jsonl").open("a") as f:
        f.write(json.dumps({"pane": a[2], "keys": a[3:], "status": status, "time": time.time()}) + "\n")
    if a[3:] == ["esc"] and status == "working" and not (D / f"esc-ignored-{key(a[2])}").exists():
        (D / f"status-{key(a[2])}").write_text("idle")
    ok({"type": "ok"})
if a[:2] == ["agent", "prompt"]:
    if "--wait" not in a and (D / "slow-update").exists():
        time.sleep(1)
    agents = json.loads((D / "agents.json").read_text())
    if "reveal" in agents[a[2]]:  # its first turn starts: Herdr learns the session id now
        agents[a[2]]["agent_session"] = {"value": agents[a[2]].pop("reveal")}
        (D / "agents.json").write_text(json.dumps(agents))
    before = info(a[2])
    with (D / "prompts.jsonl").open("a") as f:
        f.write(json.dumps({"pane": a[2], "text": a[3], "time": time.time(), "wait": "--wait" in a}) + "\n")
    if "--wait" in a:
        (D / f"status-{key(a[2])}").write_text("working")
        sys.exit(settle(a[2], timeout))
    if (D / f"prompt-fail-{key(a[2])}").exists():  # written, but the CLI reports a failure
        print(json.dumps({"error": {"code": "timeout", "message": "no reply"}}), file=sys.stderr); sys.exit(1)
    if before["agent_status"] != "working":  # the input starts a new native turn, seen late
        (D / f"lag-{key(a[2])}").write_text(json.dumps([time.time() + 0.4, before["agent_status"]]))
        (D / f"status-{key(a[2])}").write_text("working")
    ok({"agent": before, "type": "agent_prompted"})
if a[:2] == ["agent", "wait"]:
    until = [a[i + 1] for i, x in enumerate(a) if x == "--until"]
    if until and (D / f"until-fail-{key(a[2])}").exists():  # a failure other than Herdr's timeout
        print(json.dumps({"error": {"code": "internal", "message": "server connection lost"}}), file=sys.stderr); sys.exit(1)
    if not until and info(a[2])["agent_status"] != "working" and not (D / f"stall-{key(a[2])}").exists():
        print(json.dumps({"result": {"agent": info(a[2])}})); sys.exit(0)
    sys.exit(settle(a[2], timeout, until))
print(json.dumps({"error": {"code": "unsupported", "message": " ".join(a)}}), file=sys.stderr); sys.exit(1)
'''


class Harness:
    def __init__(self, tmp, turn_timeout_ms=60000, use_pty=False, transcript=(), owned=False, pending=None,
                 size=(24, 100)):
        self.d = Path(tmp)
        self.size = size
        self.fake = self.d / "fake"
        self.fake.mkdir()
        herdr = self.d / "herdr"
        herdr.write_text(f"#!{sys.executable}\n" + FAKE_HERDR)
        herdr.chmod(0o755)
        ident = {a: {"agent": a, "agent_session": {"value": f"sess-{a}"}, "terminal_id": f"term-{a}",
                     "workspace_id": "w9", "tab_id": "w9:t1"} for a in PANES}
        (self.fake / "agents.json").write_text(json.dumps({PANES[a]: ident[a] for a in PANES}))
        for a in PANES:
            self.set_status(a, "idle")
        self.state = self.d / "state"
        self.state.mkdir()
        self.sock = str(self.state / "chat.sock")
        cfg = {"herdr_bin": str(herdr), "herdr_socket": "/fake/herdr.sock", "socket": self.sock, "panes": PANES,
               "bottom_pane": "w9:p0", "tab_id": "w9:t1",
               "identity": {a: {"agent": a, "session": f"sess-{a}", "terminal_id": f"term-{a}",
                                "workspace_id": "w9", "pgid": 100} for a in PANES},
               "first": "codex", "turn_timeout_ms": turn_timeout_ms}
        if owned:  # fresh launch (native_argv recorded); Codex reports no session id before its first turn
            cfg["native_argv"] = {"codex": ["--model", "m"], "claude": ["--model", "m"]}
            cfg["identity"]["codex"]["session"] = None
            ident["codex"]["agent_session"] = {"value": None}
            (self.fake / "agents.json").write_text(json.dumps({PANES[a]: ident[a] for a in PANES}))
        if pending is not None:  # natively resumed Codex session bound as pending; Herdr reveals `pending` at its first turn
            cfg["native_argv"] = {"codex": ["--model", "m"], "claude": ["--model", "m"]}
            cfg["identity"]["codex"]["pending"] = True
            ident["codex"]["agent_session"] = {"value": None}
            if pending:
                ident["codex"]["reveal"] = pending
            (self.fake / "agents.json").write_text(json.dumps({PANES[a]: ident[a] for a in PANES}))
        (self.state / "config.json").write_text(json.dumps(cfg))
        if transcript:  # pre-existing chat state (e.g. from before progress.json existed)
            (self.state / "transcript.jsonl").write_text("".join(
                json.dumps({"seq": i + 1, "time": 0, "author": a, "text": t}) + "\n" for i, (a, t) in enumerate(transcript)))
        self.use_pty = use_pty
        self.launch()

    def launch(self, *args, env=None):
        env = dict(os.environ, FAKE_HERDR_DIR=str(self.fake), TERM="xterm", PROMPT_TOOLKIT_NO_CPR="1", **(env or {}))
        cmd = [sys.executable, str(SCRIPT), *(args or ("ui", "--state-dir", str(self.state)))]
        self.output = ""
        if self.use_pty:  # a sized terminal for the line editor; no cursor-position replies here
            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *self.size, 0, 0))
            self.proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
            os.close(slave)
        else:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, env=env, text=True)
            threading.Thread(target=self._pump, daemon=True).start()
        self.wait(lambda: os.path.exists(self.sock), "coordinator socket")

    def _pump(self):
        for line in self.proc.stdout:
            self.output += line

    def read_pty(self):
        while select.select([self.master], [], [], 0.05)[0]:
            try:
                self.output += os.read(self.master, 65536).decode(errors="replace")
            except OSError:
                break
        return self.output

    def type(self, text):
        os.write(self.master, text.encode())

    def screen(self):
        """The rows the full-screen line editor shows now: the PTY output replayed on a grid (only what prompt_toolkit's
        renderer uses: cursor moves, erases, the alternate screen; styles are dropped)."""
        rows, cols = self.size
        grid, saved, y, x = [[" "] * cols for _ in range(rows)], None, 0, 0
        for m in re.finditer(r"\x1b\[([?>]?)([0-9;]*)[ -/]*([@-~])|\x1b\][^\x07]*\x07|\x1b.|(.)", self.read_pty(), re.S):
            mark, args, op, ch = m.groups()
            n = int(args.split(";")[0] or 1) if op else 0
            if op == "H":
                y, x = [int(v or 1) - 1 for v in (args.split(";") + ["1", "1"])[:2]]
            elif op in ("A", "B", "C", "D"):
                y, x = y + {"A": -n, "B": n}.get(op, 0), x + {"C": n, "D": -n}.get(op, 0)
            elif op in ("J", "K"):
                start = (0, 0) if args == "2" else (y, x)
                grid[start[0]][start[1]:] = [" "] * (cols - start[1])
                if op == "J":
                    grid[start[0] + 1:] = [[" "] * cols for _ in grid[start[0] + 1:]]
            elif op in ("h", "l") and mark == "?" and args == "1049":
                if op == "h":
                    saved, grid = grid, [[" "] * cols for _ in range(rows)]
                elif saved:
                    grid, saved = saved, None
            elif ch == "\r":
                x = 0
            elif ch == "\n":
                y += 1
                if y == rows:
                    grid, y = grid[1:] + [[" "] * cols], rows - 1
            elif ch == "\x08":
                x = max(x - 1, 0)
            elif ch and ch >= " ":
                wide = unicodedata.east_asian_width(ch) in "WF"
                if x + wide < cols:
                    grid[y][x] = ch
                    if wide:
                        grid[y][x + 1] = ""
                x += 1 + wide
            y, x = min(max(y, 0), rows - 1), max(x, 0)
        return ["".join(r).rstrip() for r in grid]

    def text(self):
        return "\n".join(self.screen())

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def wait(self, pred, what, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; output:\n{self.output}")

    def set_status(self, agent, status):
        (self.fake / f"status-{PANES[agent].replace(':', '_')}").write_text(status)

    def finish(self, agent, status="done"):
        (self.fake / f"finish-{PANES[agent].replace(':', '_')}").write_text(status)

    def prompts(self):
        p = self.fake / "prompts.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def wait_prompts(self, n):
        self.wait(lambda: len(self.prompts()) >= n, f"{n} prompts")
        return self.prompts()

    def reload(self):
        """Quiet coordinator reload on the same state and bound sessions."""
        self.close()
        os.unlink(self.sock)
        self.launch()

    def reboot(self, n):
        """The coordinator dies (its socket file stays); Herdr resumes the same native sessions with new terminal
        and process ids; bare `start` in the group's shared pane recovers the group."""
        self.close()
        (self.fake / "slow-get").unlink(missing_ok=True)
        agents = json.loads((self.fake / "agents.json").read_text())
        for a in PANES:
            agents[PANES[a]].update(terminal_id=f"term-{a}-{n}", pgid=300 + n)
            self.set_status(a, "idle")
        (self.fake / "agents.json").write_text(json.dumps(agents))
        root = self.d / "home/.local/state/herdr-group-chat"
        if not root.exists():
            root.mkdir(parents=True)
            (root / "g1").symlink_to(self.state)
        self.launch("start", "--herdr-bin", str(self.d / "herdr"), env={
            "HOME": str(self.d / "home"), "HERDR_ENV": "1", "HERDR_PANE_ID": "w9:p0", "HERDR_SOCKET_PATH": "/fake/herdr.sock"})
        self.wait(lambda: "Plain text goes to both agents" in self.output, f"recovery {n}")

    def crash_window(self, *lines):
        """Send lines while every `agent get` stalls (check() in dispatch()/update() runs before their event):
        the last public message exists, but no input event has been written for it yet."""
        (self.fake / "slow-get").write_text("")
        n = len(self.transcript())
        for line in lines:
            self.ctl(line)
        self.wait(lambda: len(self.transcript()) > n, "publication")
        time.sleep(0.2)
        assert not {"dispatch", "update"} & {e["kind"] for e in self.events()[-4:]}, self.events()

    def token(self, prompt):
        return re.search(r"--turn (\S+)", prompt["text"]).group(1)

    def run(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True,
                              text=True, env=dict(os.environ, **(env or {})), timeout=20)

    def claim(self, prompt_or_token, pane=None):
        tok = prompt_or_token if isinstance(prompt_or_token, str) else self.token(prompt_or_token)
        return self.run("claim", "--socket", self.sock, "--turn", tok, env={"HERDR_PANE_ID": pane or ""})

    def submit(self, token, body, pane=None):
        return self.run("submit", "--socket", self.sock, "--turn", token, stdin=body + "\n",
                        env={"HERDR_PANE_ID": pane or ""})

    def agent_of(self, prompt):
        return "codex" if prompt["pane"] == PANES["codex"] else "claude"

    def post(self, prompt, body):
        """Claim and post one message; the native turn keeps running."""
        r = self.claim(prompt)
        assert r.returncode == 0, r.stdout + r.stderr
        r = self.submit(self.token(prompt), body)
        assert r.returncode == 0, r.stdout + r.stderr

    def ack(self, prompt_or_token, pane=None):
        """The receipt an agent runs on reading an update (not a claim)."""
        tok = prompt_or_token if isinstance(prompt_or_token, str) else self.token(prompt_or_token)
        return self.run("ack", "--socket", self.sock, "--turn", tok, env={"HERDR_PANE_ID": pane or ""})

    def answer(self, prompt, body, status="done"):
        """Claim, post once, and end that native turn."""
        self.post(prompt, body)
        self.finish(self.agent_of(prompt), status)

    def newest(self, agent, seq):
        """Wait until `agent` has an input carrying public message #seq; return the latest such input."""
        get = lambda: [p for p in self.prompts() if p["pane"] == PANES[agent] and seq in seqs(p)]
        self.wait(get, f"#{seq} to {agent}")
        return get()[-1]

    def agent_prompts(self, agent, n):
        """Wait until `agent` has received n native prompts; return them in order."""
        get = lambda: [p for p in self.prompts() if p["pane"] == PANES[agent]]
        self.wait(lambda: len(get()) >= n, f"{n} prompts to {agent}")
        return get()

    def idle(self):
        s = self.status()
        return not (s["tasks"] or s["due"] or s["owner"] or s["queued"])

    def closes(self):
        calls = [json.loads(l) for l in (self.fake / "calls.jsonl").read_text().splitlines()]
        return [c[2] for c in calls if c[:2] == ["pane", "close"]]

    def line(self, text):
        """One line on the coordinator's own (non-tty) input."""
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def ctl(self, line):
        return self.run("ctl", "--socket", self.sock, line).stdout.strip()

    def status(self):
        return json.loads(self.run("ctl", "--socket", self.sock, "status-json").stdout)

    def transcript(self):
        p = self.state / "transcript.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def events(self, kind=None):
        evs = [json.loads(l) for l in (self.state / "events.jsonl").read_text().splitlines()]
        return [e for e in evs if kind is None or e["kind"] == kind]


def seqs(prompt):
    """Public message numbers carried by one native prompt."""
    return [int(n) for n in re.findall(r"^=== #(\d+) ", prompt["text"], re.M)]


SAFE = ("PAUSED: no native group turn running and both sessions idle when checked, native input boxes may be used "
        "(a delegated task's report can still wake an agent later; /pause again re-checks)")
BODY = "第一行：中文 ✓\n```python\nprint('a $HOME `x` \\n')\n```\n\n  indented & <tag> \"quotes\"\nlast line"
NOTE = "使用 ID2 方法與 Alphas 選擇；" * 5 + "結尾"
TABLE = ("## 結果\n\n| method | Global accuracy | 備註 |\n|---|---:|---|\n"
         f"| Alphas | 0.812 | {NOTE} |\n| ID2-a-very-long-identifier-without-spaces | 0.7 | ok |")


def load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("herdr_group_chat", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GroupChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = None

    def tearDown(self):
        if self.h:
            self.h.close()
        self.tmp.cleanup()

    def start(self, **kw):
        self.h = Harness(self.tmp.name, **kw)
        return self.h

    def test_each_grant_posts_one_message_and_running_turns_get_the_others_posts(self):
        h = self.start()
        h.ctl("hello group")
        [c1], [a1] = h.agent_prompts("codex", 1), h.agent_prompts("claude", 1)  # concurrent evaluations
        self.assertEqual((seqs(c1), seqs(a1)), ([1], [1]))
        r = h.claim(c1, pane=PANES["codex"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("GRANTED", r.stdout)
        lose = h.claim(a1, pane=PANES["claude"])  # private denial through the claiming tool itself
        self.assertEqual(lose.returncode, 2)
        self.assertIn("DENIED: Codex holds the speaking slot", lose.stdout)
        self.assertIn("NO reservation", lose.stdout)
        self.assertEqual(h.submit(h.token(a1), "sneak").returncode, 2)  # a loser cannot post
        t0 = time.time()
        r = h.submit(h.token(c1), BODY, pane=PANES["codex"])
        self.assertIn("received as group message #2; the speaking slot is released", r.stdout)
        self.assertLess(time.time() - t0, 3)
        self.assertIsNone(h.status()["owner"])  # released at once; Codex's native turn keeps running
        self.assertIn("claim first", h.submit(h.token(c1), "codex more").stdout)  # next message: fresh claim
        a2 = h.agent_prompts("claude", 2)[1]  # Claude, still working, gets #2 in its running turn
        self.assertEqual((seqs(a2), a2["wait"]), ([2], False))
        self.assertIn(BODY, a2["text"])
        self.assertIn("[Herdr group chat] Update", a2["text"])
        stale = h.claim(a1)  # Claude's first input predates #2
        self.assertEqual(stale.returncode, 2)
        self.assertIn("newer public messages (#2-#2)", stale.stdout)
        self.assertEqual(h.claim(a2).returncode, 0)
        self.assertIn("received as group message #3", h.submit(h.token(a2), "claude reply").stdout)
        c2 = h.agent_prompts("codex", 2)[1]  # Codex is still in its first native turn
        self.assertEqual((seqs(c2), c2["wait"]), ([3], False))
        self.assertEqual(h.claim(c1).returncode, 2)  # it must catch up on #3 first
        self.assertEqual(h.claim(c2).returncode, 0)
        h.submit(h.token(c2), "codex again")
        a3 = h.agent_prompts("claude", 3)[2]
        self.assertEqual(seqs(a3), [4])
        self.assertIn("NOT safe", h.ctl("/pause"))  # research continues after posting: still native-busy
        self.assertIn("a group turn is active", h.ctl("/exit"))
        self.assertIn("receipt recorded", h.ack(a3).stdout)  # Claude will stay silent; Codex's claim was its receipt
        h.finish("codex")
        h.finish("claude")
        h.wait(lambda: SAFE in h.output, "safe notice after both native turns ended")
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        h.wait(h.idle, "all done")
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 5)  # no own/old echo, no duplicate or extra native turn
        self.assertEqual([(m["author"], m["text"]) for m in h.transcript()],
                         [("user", "hello group"), ("codex", BODY), ("claude", "claude reply"), ("codex", "codex again")])
        grants = [e["kind"] for e in h.events() if e["kind"] in ("claim_granted", "release")]
        self.assertEqual(grants, ["claim_granted", "release"] * 3)  # one message per grant, never overlapping
        self.assertEqual(len(h.events("turn_end")), 2)
        self.assertEqual([p["seen"] for p in json.loads((h.state / "progress.json").read_text()).values()], [4, 4])

    def test_simultaneous_claims_yield_exactly_one_owner_and_retry_does_not_reallocate(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        results = []
        threads = [threading.Thread(target=lambda p=p: results.append((h.agent_of(p), h.claim(p))))
                   for p in (c1, a1) * 4]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        winners = {agent for agent, r in results if r.returncode == 0}
        self.assertEqual(len(winners), 1)
        self.assertTrue(all("GRANTED" in r.stdout for agent, r in results if agent in winners))
        self.assertTrue(all("holds the speaking slot" in r.stdout for agent, r in results if agent not in winners))
        self.assertEqual(len(h.events("claim_granted")), 1)
        [win] = winners
        self.assertEqual(h.status()["owner"], h.token(c1 if win == "codex" else a1))

    def test_progress_and_grants_never_skip_a_message_the_claimant_has_not_received(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        a2 = h.agent_prompts("claude", 2)[1]  # #2 written into Claude's turn: transport only
        seen = lambda a: json.loads((h.state / "progress.json").read_text())[a]["seen"]
        self.assertEqual(h.claim(a1).returncode, 2)  # acknowledges only what a1 carried
        self.assertEqual(seen("claude"), 1)
        h.post(c1, "c3")  # Codex is caught up: a fresh grant with its first input's command
        a3 = h.agent_prompts("claude", 3)[2]
        self.assertEqual(seqs(a3), [3])
        r = h.claim(a2)
        self.assertEqual(r.returncode, 2)
        self.assertIn("(#3-#3)", r.stdout)
        self.assertEqual(seen("claude"), 2)  # #3 is not counted as received
        self.assertEqual(h.submit(h.token(a3), "a4").returncode, 2)  # no grant, no post past #3
        self.assertEqual(h.claim(a3).returncode, 0)
        h.submit(h.token(a3), "a4")
        self.assertEqual(seen("claude"), 4)
        h.finish("claude")
        c2 = h.agent_prompts("codex", 2)[1]
        self.assertEqual(seqs(c2), [4])
        h.ack(c2)
        h.finish("codex")
        h.wait(h.idle, "all done")
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "c2", "c3", "a4"])

    def test_native_completion_never_releases_or_recreates_another_grant(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        a2 = h.agent_prompts("claude", 2)[1]
        self.assertEqual(h.claim(a2).returncode, 0)  # Claude's grant while Codex's turn still runs
        h.ctl("u4")  # user input during a grant waits; it is not a speaker queue
        h.finish("codex")  # Codex's native end, after its own grant was released
        h.wait(lambda: h.events("turn_end"), "Codex's task end")
        time.sleep(0.3)
        self.assertEqual(h.status()["owner"], h.token(a1))  # untouched, and Codex's grant not re-created
        self.assertEqual(h.claim(c1).returncode, 2)  # ended task: stale
        self.assertEqual(h.submit(h.token(c1), "late").returncode, 2)
        h.submit(h.token(a2), "a3")
        c2 = h.agent_prompts("codex", 2)[1]  # new native turn; the waiting user message joined the release
        self.assertEqual((seqs(c2), c2["wait"]), ([3, 4], True))
        self.assertEqual(seqs(h.agent_prompts("claude", 3)[2]), [4])  # update: never its own #3
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "c2", "a3", "u4"])

    def test_update_reaching_a_settled_session_is_tracked_until_its_new_turn_ends(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        (h.fake / f"stall-{PANES['claude'].replace(':', '_')}").write_text("")
        h.set_status("claude", "done")  # Claude's turn ended; the coordinator has not seen it yet
        h.post(c1, "c2")
        h.wait(lambda: h.events("update_written"), "update")
        self.assertEqual(h.events("update_written")[0]["status"], "done")  # it starts a new native turn
        (h.fake / f"stall-{PANES['claude'].replace(':', '_')}").unlink()
        time.sleep(1.0)  # Herdr shows `done` for a moment; the task must wait for that new turn
        self.assertIn("claude", h.status()["tasks"])
        self.assertEqual(h.events("turn_end"), [])
        self.assertIn("Claude Code", h.ctl("/pause"))
        h.ctl("/resume")
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's task end")
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 2)
        self.assertEqual(len(h.agent_prompts("claude", 2)), 2)  # no extra fresh prompt for #2

    def boundary_write(self, h, agent, lag):
        """The running turn ends just as an update written while `working` arrives; that update starts a new
        native turn, which Herdr keeps reporting as the old `done` for `lag` seconds (review repro F1)."""
        (h.fake / f"lag-{PANES[agent].replace(':', '_')}").write_text(json.dumps([time.time() + lag, "done"]))
        h.set_status(agent, "working")

    def boundary_setup(self, h):
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        h.wait(lambda: h.events("update_written"), "update to Claude")
        self.assertEqual(h.events("update_written")[0]["status"], "working")
        a2 = h.agent_prompts("claude", 2)[1]
        h.finish("codex")
        h.wait(lambda: "codex" not in h.status()["tasks"], "Codex's end")
        return a2

    def test_update_turn_at_the_native_boundary_stays_tracked(self):
        h = self.start()
        a2 = self.boundary_setup(h)
        h.ctl("u3")  # waits for the turns; would be published (and dispatched) when the last task ends
        self.boundary_write(h, "claude", 0.4)
        time.sleep(1.2)  # Herdr now shows the update's native turn
        self.assertIn("claude", h.status()["tasks"])
        self.assertEqual(h.events("turn_end"), [e for e in h.events("turn_end") if e["agent"] == "codex"])
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 1)
        self.assertEqual(len(h.agent_prompts("claude", 2)), 2)  # no fresh prompt into the running session
        self.assertEqual(h.transcript()[-1]["text"], "c2")  # u3 still waits
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertIn("is not Claude Code's pane", h.claim(a2, pane=PANES["codex"]).stdout)  # token still current
        h.finish("claude")  # the update's native turn ends
        h.wait(lambda: SAFE in h.output, "safe notice after the real end")
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 2)
        h.ctl("/resume")
        a3 = h.agent_prompts("claude", 3)[2]
        self.assertEqual((seqs(a3), a3["wait"]), ([3], True))

    def test_unacknowledged_update_without_a_visible_turn_stays_tracked_and_holds(self):
        h = self.start()
        a2 = self.boundary_setup(h)
        self.boundary_write(h, "claude", 6.5)  # Herdr shows the update's turn only after the 5 s window
        h.wait(lambda: h.status()["hold"], "unresolved hold", timeout=12)
        self.assertIn("has no receipt", h.status()["hold"])
        self.assertIn("claude", h.status()["tasks"])  # elapsed time is not taken as completion
        self.assertEqual(h.events("turn_end"), [e for e in h.events("turn_end") if e["agent"] == "codex"])
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 1)
        self.assertIn("is not Claude Code's pane", h.claim(a2, pane=PANES["codex"]).stdout)  # token still current
        self.assertIn("NOT safe", h.ctl("/pause"))
        h.ctl("u3")
        time.sleep(2.0)  # Herdr now shows Claude working
        self.assertEqual(len(h.agent_prompts("claude", 2)), 2)  # never a fresh prompt into it
        self.assertNotIn(SAFE, h.output)
        self.assertIn("reconciling Claude Code", h.ctl("/resume"))  # recovery: re-observe the native turn
        time.sleep(0.5)
        self.assertIn("claude", h.status()["tasks"])  # reconcile waits for the turn Herdr now shows
        h.finish("claude")
        self.assertEqual(seqs(h.agent_prompts("claude", 3)[2]), [2, 3])  # unconfirmed #2 repeated, not lost

    def test_receipt_only_silent_end_completes_after_the_quiet_window(self):
        h = self.start()
        a2 = self.boundary_setup(h)
        self.assertIn("not recorded", h.ack(a2, pane=PANES["codex"]).stdout)  # wrong pane: no receipt
        self.assertIn(" ack --socket ", a2["text"])
        r = h.ack(a2)  # receipt only: no grant, no public message, no other effect
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((h.status()["owner"], h.status()["due"], len(h.transcript())), (None, [], 2))
        h.finish("claude")  # then silence
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's natural end")
        self.assertEqual((h.status()["hold"], h.events("update_unresolved")), (None, []))
        self.assertEqual(len(h.events("update_settled")), 1)  # only after the quiet window
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 2)
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 3)

    def test_receipt_before_a_stale_done_does_not_end_the_running_follow_up(self):
        # Review recheck R1: the update reaches Claude, Claude's refused claim (a receipt) runs in the follow-up
        # turn it started, and only then does Herdr report the first turn's late `done`, for 3 s.
        h = self.start()
        a2 = self.boundary_setup(h)
        h.ctl("/pause")
        self.assertIn("paused or stopped", h.claim(a2).stdout)
        self.boundary_write(h, "claude", 3.0)
        time.sleep(1.5)  # Herdr reports `done`; Claude really works
        self.assertIn("claude", h.status()["tasks"])
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertNotIn(SAFE, h.output)
        time.sleep(2.5)  # Herdr now shows the follow-up turn; the task follows it
        self.assertIn("claude", h.status()["tasks"])
        h.finish("claude")
        h.wait(lambda: SAFE in h.output, "safe notice after the real end")

    def test_receipt_of_an_older_update_does_not_end_a_newer_batch(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        a2 = h.agent_prompts("claude", 2)[1]
        h.ack(a2)
        h.post(c1, "c3")
        a3 = h.newest("claude", 3)
        self.assertIn("not recorded", h.ack(a3, pane=PANES["codex"]).stdout)
        h.ack(a2)  # a repeated older receipt cannot cover #3
        h.finish("claude")
        h.wait(lambda: h.status()["hold"], "unresolved hold", timeout=12)
        self.assertIn("has no receipt", h.status()["hold"])
        self.assertIn("claude", h.status()["tasks"])
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 2)

    def test_write_during_the_quiet_window_is_followed_to_its_end(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        h.ack(h.agent_prompts("claude", 2)[1])
        h.finish("claude")
        time.sleep(1.0)  # inside Claude's quiet window
        h.post(c1, "c3")  # reaches settled Claude as a cold update and starts a new native turn
        a3 = h.agent_prompts("claude", 3)[2]
        self.assertEqual((seqs(a3), a3["wait"]), ([3], False))
        h.wait(lambda: len(h.events("update_written")) == 2, "second write")
        self.assertEqual(h.events("update_written")[-1]["status"], "done")
        h.ack(a3)  # the turn the cold update started reads it and sends its receipt
        time.sleep(1.0)
        self.assertIn("claude", h.status()["tasks"])  # never finalized by the older window
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "end of the turn the write started", timeout=12)
        self.assertEqual((h.status()["hold"], h.events("update_unresolved")), (None, []))
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 3)

    def test_late_working_reply_is_not_credited_with_a_newer_cold_update(self):
        # Review C1: Herdr sees T2 (from receipted U1) working, but its answer reaches the coordinator late; T2 ends
        # and cold U2 starts T3 (shown `done` for 0.4 s) before that old answer is processed.
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")
        h.ack(h.agent_prompts("claude", 2)[1])
        h.finish("claude")
        h.wait(lambda: h.events("native_return"), "Claude's settled return")
        time.sleep(0.2)  # the activity probe is running
        stall = h.fake / f"stall-{PANES['claude'].replace(':', '_')}"
        stall.write_text("")
        h.set_status("claude", "working")  # T2 seen; the answer is held
        time.sleep(0.3)
        h.set_status("claude", "done")  # T2 ends
        h.post(c1, "c3")  # cold U2 starts T3
        h.wait(lambda: len(h.events("update_written")) == 2, "cold U2")
        a3 = h.agent_prompts("claude", 3)[2]
        stall.unlink()  # the old `working` answer arrives now
        time.sleep(1.0)  # T3 runs
        self.assertIn("claude", h.status()["tasks"])
        self.assertIn("Claude Code", h.ctl("/pause"))  # Claude itself is native-busy, not only Codex
        self.assertEqual([e for e in h.events("turn_end") if e["agent"] == "claude"], [])
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 2)  # U2 unseen
        h.ack(a3)
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "T3's end", timeout=12)
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 3)

    def test_failed_activity_check_keeps_the_task_and_holds(self):
        h = self.start()
        self.boundary_setup(h)
        (h.fake / f"until-fail-{PANES['claude'].replace(':', '_')}").write_text("")
        h.finish("claude")
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn("server connection lost", h.status()["hold"])
        self.assertIn("claude", h.status()["tasks"])  # not taken as settled
        self.assertIn("completion unconfirmed", h.ctl("/pause"))
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 1)
        (h.fake / f"until-fail-{PANES['claude'].replace(':', '_')}").unlink()
        self.assertIn("reconciling Claude Code", h.ctl("/resume"))
        h.wait(lambda: not h.status()["tasks"], "reconciled")
        self.assertEqual(len(h.prompts()), 3)  # nothing resent

    def test_unconfirmed_update_holds_without_resend_and_stays_unseen(self):
        h = self.start()
        h.ctl("u1")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        (h.fake / f"prompt-fail-{PANES['claude'].replace(':', '_')}").write_text("")
        h.post(c1, "c2")
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn("update", h.status()["hold"])
        self.assertIn("may have reached", h.status()["hold"])
        h.finish("claude")
        h.finish("codex")
        h.wait(lambda: "codex" not in h.status()["tasks"], "Codex's end")
        time.sleep(0.3)
        self.assertIn("claude", h.status()["tasks"])  # completion after an unconfirmed write is not trusted
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertIn("reconciling Claude Code", h.ctl("/resume"))
        h.wait(h.idle, "reconciled")
        self.assertEqual(len(h.prompts()), 3)  # nothing resent
        h.ctl("u3")
        self.assertEqual(seqs(h.agent_prompts("claude", 3)[2]), [2, 3])  # #2 unconfirmed: repeated, not lost

    def test_duplicate_wrong_pane_unclaimed_and_stale_submissions_are_not_recorded(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        tok = h.token(c1)
        self.assertEqual(h.submit(tok, "unclaimed").returncode, 2)
        self.assertIn("claim first", h.submit(tok, "unclaimed").stdout)
        self.assertEqual(h.submit(tok, "   \n").returncode, 2)  # empty: no message, grant not needed
        self.assertEqual(h.claim(tok, pane=PANES["claude"]).returncode, 2)
        self.assertEqual(h.claim("t1-codex-forged").returncode, 2)
        self.assertEqual(h.claim(tok).returncode, 0)
        self.assertIn("already yours", h.claim(tok).stdout)
        self.assertEqual(h.submit(tok, "   ").returncode, 2)  # rejected empty input keeps the grant
        self.assertEqual(h.status()["owner"], tok)
        self.assertEqual(h.submit(tok, "x", pane=PANES["claude"]).returncode, 2)
        self.assertEqual(h.submit(tok, "first").returncode, 0)
        self.assertEqual(h.submit(tok, "second").returncode, 2)  # its grant was used by "first"
        self.assertEqual(h.claim(tok).returncode, 0)
        self.assertEqual(h.submit(tok, "second").returncode, 0)
        dup = h.submit(tok, "first")  # retry of an earlier accepted post, after its grant was released
        self.assertEqual(dup.returncode, 0)
        self.assertIn("received earlier as message #2; not recorded again", dup.stdout)
        a2 = h.newest("claude", 3)
        self.assertEqual(h.claim(a2).returncode, 0)  # Claude now holds a newer grant
        self.assertIn("message #3; not recorded again", h.submit(tok, "second").stdout)  # retry: owner untouched
        self.assertEqual(h.status()["owner"], h.token(a1))
        self.assertIn("Claude Code holds", h.claim(tok).stdout)
        self.assertEqual(h.submit(tok, "third").returncode, 2)
        h.submit(h.token(a2), "a4")
        h.ack(h.newest("codex", 4))
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all done")
        self.assertEqual(h.submit(tok, "late").returncode, 2)  # stale after the task ended
        self.assertEqual(h.claim(tok).returncode, 2)
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "first", "second", "a4"])

    def test_generated_commands_work_from_a_tool_shell_with_another_panes_ambient_id(self):
        # Codex may run tools in a shared app-server daemon whose HERDR_PANE_ID is the pane
        # that started the daemon, not the TUI pane the prompt was sent to.
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        line = lambda p, op: re.search(rf"^(.* {op} --socket .*?)(?: <<'GROUPCHAT_EOF')?$", p["text"], re.M).group(1)
        sh = lambda cmd, stdin=None: subprocess.run(["bash", "-c", cmd], input=stdin, capture_output=True, text=True,
                                                    env=dict(os.environ, HERDR_PANE_ID="w9:p0"), timeout=20)
        r = sh(line(c1, "claim"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = sh(line(a1, "claim"))  # the peer loses on ownership, not on its pane
        self.assertEqual(r.returncode, 2)
        self.assertIn("Codex holds the speaking slot", r.stdout)
        r = sh(line(c1, "submit"), stdin="from codex\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("received as group message #2", r.stdout)
        sh(line(c1, "claim"))
        forged = line(a1, "submit").replace(h.token(a1), h.token(c1))  # owner's token, peer's pane
        r = sh(forged, stdin="from claude\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"caller pane {PANES['claude']} is not Codex's pane", r.stdout)
        a2 = h.agent_prompts("claude", 2)[1]  # the update's commands work the same way
        self.assertEqual(sh(line(a2, "claim")).returncode, 2)  # Codex holds its second grant
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "from codex"])

    def test_owner_without_submission_stops_until_late_submit_and_resume(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.claim(c1)
        h.finish("claude")
        h.finish("codex")
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn("without submitting", h.status()["hold"])
        self.assertEqual(h.submit(h.token(c1), "late but same turn").returncode, 0)
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 2)
        self.assertEqual(h.ctl("/resume"), "resumed: reconciling Codex turn " + h.token(c1))
        self.assertEqual(seqs(h.agent_prompts("claude", 2)[1]), [2])

    def rejoin(self, tok, pane=PANES["claude"]):
        return self.h.run("rejoin", "--socket", self.h.sock, "--turn", tok, "--pane", pane)

    def test_parent_woken_after_its_turn_ended_rejoins_catches_up_and_posts_one_message(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        self.assertEqual(a1["text"].count(" rejoin --socket "), 1)  # taught once, in the turn prompt
        h.post(a1, "delegated; report later")  # #2, then Claude ends its group turn
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's turn end")
        c2 = h.newest("codex", 2)  # Codex keeps working
        h.set_status("claude", "working")  # a delegated task's report wakes the same native session
        old = h.claim(a1)  # the observed failure: the ended turn's token stays refused
        self.assertEqual(old.returncode, 2)
        self.assertIn("not a current input; if your group turn has ended, run the rejoin", old.stdout)
        line = re.search(r"^(.* rejoin --socket .*)$", a1["text"], re.M).group(1)
        r = subprocess.run(["bash", "-c", line], capture_output=True, text=True, timeout=20,
                           env=dict(os.environ, HERDR_PANE_ID="w9:p0"))  # generated command, wrong ambient pane
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[Herdr group chat] Rejoin", r.stdout)
        self.assertIn("no public messages you have not received (everything through #2", r.stdout)
        self.assertEqual(seqs({"text": r.stdout}), [])  # neither #1 nor its own #2 again
        tok = h.token({"text": r.stdout})
        self.assertEqual(h.events("rejoin")[-1]["via"], h.token(a1))
        h.post(c2, "codex meanwhile")  # #3 races with the catch-up
        a2 = h.newest("claude", 3)  # an update into the tracked callback turn, not a new native turn
        self.assertEqual((seqs(a2), a2["wait"]), ([3], False))
        self.assertNotIn(" rejoin --socket ", a2["text"])
        self.assertIn("newer public messages (#3-#3)", h.claim(tok).stdout)
        self.assertEqual(h.submit(tok, "too early").returncode, 2)
        h.post(a2, "child report: done")  # #4 under the regular gate
        c3 = h.newest("codex", 4)
        self.assertEqual(seqs(c3), [4])
        h.ack(c3)
        h.finish("codex")
        h.finish("claude")  # the callback turn ends natively
        h.wait(h.idle, "all done", timeout=15)
        self.assertEqual(len(h.agent_prompts("claude", 2)), 2)  # the dispatch and one update: rejoin sent nothing
        self.assertEqual([m["text"] for m in h.transcript()],
                         ["q", "delegated; report later", "codex meanwhile", "child report: done"])
        self.assertEqual([e["token"] for e in h.events("turn_end") if e["agent"] == "claude"], [h.token(a1), tok])
        grants = [e["kind"] for e in h.events() if e["kind"] in ("claim_granted", "release")]
        self.assertEqual(grants, ["claim_granted", "release"] * 3)
        for stale in (h.token(a1), tok, h.token(a2)):  # every ended token stays unable to claim
            self.assertEqual(h.claim(stale).returncode, 2)
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 4)

    def test_rejoin_refuses_unbound_idle_or_stopped_sessions_and_never_duplicates(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        tok1 = h.token(a1)
        h.post(a1, "a2")
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's turn end")
        h.post(h.newest("codex", 2), "c3")
        h.newest("claude", 3)  # #3 reaches Claude in a new turn, which Esc interrupts: not credited
        h.ctl("/interrupt")
        h.wait(lambda: "INTERRUPTED (Esc)" in h.output, "both stopped")
        h.set_status("claude", "working")  # woken natively while the chat is paused
        self.assertIn("paused or stopped, and rejoining does not resume it", self.rejoin(tok1).stdout)
        h.set_status("claude", "idle")
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        self.assertIn("not working in a native turn", self.rejoin(tok1).stdout)
        h.set_status("claude", "working")
        self.assertIn("was not issued", self.rejoin("t1-claude-forged").stdout)
        self.assertIn("caller pane w9:p7 is not Claude Code's pane", self.rejoin(tok1, "w9:p7").stdout)  # a child's
        self.assertIn("is not Codex's pane", self.rejoin(h.token(c1)).stdout)
        agents = json.loads((h.fake / "agents.json").read_text())
        agents[PANES["claude"]]["agent_session"]["value"] = "sess-other"
        (h.fake / "agents.json").write_text(json.dumps(agents))
        self.assertIn("no longer hosts the bound session", self.rejoin(tok1).stdout)
        agents[PANES["claude"]]["agent_session"]["value"] = "sess-claude"
        (h.fake / "agents.json").write_text(json.dumps(agents))
        self.assertEqual(h.events("rejoin"), [])
        r = self.rejoin(tok1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(seqs({"text": r.stdout}), [3])  # only what it has not received
        tok = h.token({"text": r.stdout})
        for again in (tok1, tok):  # a second re-entry before any receipt: the same reply, no new token or task
            self.assertEqual(self.rejoin(again).stdout, r.stdout)
        self.assertEqual(len(h.events("rejoin")), 1)
        self.assertIn("NOT safe", h.ctl("/pause"))  # the callback turn is tracked
        self.assertIn("a group turn is active", h.ctl("/exit"))
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        self.assertEqual(h.claim(tok).returncode, 0)  # its receipt credits #3
        h.submit(tok, "a4")
        h.finish("claude")
        h.finish(h.agent_of(h.newest("codex", 4)))
        h.wait(h.idle, "all done")
        h.ctl("/ask codex z")  # #5, asked of Codex only
        h.newest("codex", 5)
        h.set_status("claude", "working")
        self.assertIn("the latest user message asked only Codex", self.rejoin(tok1).stdout)
        h.set_status("claude", "idle")
        h.finish("codex")
        h.wait(h.idle, "all done")
        self.assertEqual(len(h.agent_prompts("claude", 2)), 2)  # no prompt from any rejoin
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "a2", "c3", "a4", "z"])

    def uncredited_catch_up(self):
        """Claude ended its turn after #2; #3 reached it in a turn Esc interrupted, so #3 is not credited."""
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(a1, "a2")
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's turn end")
        h.post(h.newest("codex", 2), "c3")
        h.newest("claude", 3)
        h.ctl("/interrupt")
        h.wait(lambda: "INTERRUPTED (Esc)" in h.output, "both stopped")
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        return h, c1, a1

    def seen(self, agent):
        return json.loads((self.h.state / "progress.json").read_text())[agent]["seen"]

    def test_unreceipted_rejoin_reply_is_returned_again_without_credit_or_new_task(self):
        h, c1, a1 = self.uncredited_catch_up()
        h.set_status("claude", "working")
        n = len(h.prompts())
        r = self.rejoin(h.token(a1))  # its reply carries #3; suppose it never reached the model (lost or truncated)
        self.assertEqual(seqs({"text": r.stdout}), [3])
        tok = h.token({"text": r.stdout})
        again = self.rejoin(h.token(a1))
        self.assertEqual((again.returncode, again.stdout), (0, r.stdout))  # the same reply, token and range
        self.assertIn("read the full saved output", r.stdout)
        self.assertEqual((len(h.events("rejoin")), self.seen("claude"), len(h.prompts()), h.status()["owner"]),
                         (1, 2, n, None))  # no new task, credit, prompt or grant
        self.assertEqual(h.claim(tok).returncode, 0)  # its receipt credits #3, read from the repeated reply
        self.assertEqual(self.seen("claude"), 3)
        self.assertIn("still current", self.rejoin(h.token(a1)).stdout)  # receipted: no further repeat
        h.submit(tok, "a4")
        h.finish("claude")
        h.wait(lambda: not h.status()["tasks"].get("claude"), "Claude's rejoined turn end")

    def test_update_after_an_unreceipted_rejoin_reply_carries_that_range_before_any_credit(self):
        h, c1, a1 = self.uncredited_catch_up()
        h.set_status("claude", "working")
        r = self.rejoin(h.token(a1))  # reply with #3, never read
        self.assertEqual(seqs({"text": r.stdout}), [3])
        h.set_status("codex", "working")  # Codex is woken too and rejoins to post #4
        rc = self.rejoin(h.token(c1), PANES["codex"])
        self.assertEqual(rc.returncode, 0, rc.stdout)
        h.post({"text": rc.stdout}, "c4")
        a3 = h.newest("claude", 4)  # a native input into Claude's tracked turn: #3 again, then #4
        self.assertEqual((seqs(a3), a3["wait"]), ([3, 4], False))
        self.assertEqual(self.seen("claude"), 2)  # written is not credited
        self.assertIn(f"(input {h.token(a3)})", self.rejoin(h.token(a1)).stdout)  # a newer input exists
        h.ack(a3)  # the receipt of the input that actually carried #3
        self.assertEqual(self.seen("claude"), 4)
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all done", timeout=15)
        self.assertEqual([m["text"] for m in h.transcript()], ["q", "a2", "c3", "c4"])

    def test_untracked_native_wake_is_never_reported_safe(self):
        h = self.start()
        h.ctl("q")
        h.agent_prompts("codex", 1)
        h.agent_prompts("claude", 1)
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's turn end")
        h.set_status("claude", "working")  # woken natively (e.g. a delegated report), not rejoined
        self.assertIn("Codex may still be working", h.ctl("/pause"))
        h.finish("codex")  # the last tracked turn ends while Claude still works natively
        h.wait(lambda: not h.status()["tasks"], "Codex's turn end")
        time.sleep(0.5)
        self.assertNotIn(SAFE, h.output)  # no quiet safe notice
        r = h.ctl("/pause")
        self.assertIn("NOT safe yet: Claude Code is working, not idle/done (no running group turn", r)
        h.set_status("claude", "blocked")
        self.assertIn("Claude Code is blocked", h.ctl("/pause"))
        h.set_status("claude", "done")
        self.assertEqual(h.ctl("/pause"), SAFE)  # a snapshot, worded as such
        h.close()
        chat = load_script().Chat(h.state, lambda *a, **k: None)
        chat.paused = True  # the in-memory status line no longer asserts that native input is safe
        self.assertNotIn("may be used", chat.status_line())

    def test_held_unsent_turn_of_a_natively_working_session_is_never_reported_safe(self):
        h = self.start()
        h.ctl("q")
        h.agent_prompts("claude", 1)
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "both silent")
        h.set_status("claude", "working")  # woken natively by a delegated report, not rejoined
        h.ctl("m2")  # Claude's dispatch holds before sending; Codex evaluates m2
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn("Claude Code turn not dispatched: Claude Code is working", h.status()["hold"])
        h.newest("codex", 2)
        self.assertIn("Codex may still be working", h.ctl("/pause"))
        h.finish("codex")  # Codex ends: only Claude's held, unsent task remains
        h.wait(lambda: "codex" not in h.status()["tasks"], "Codex's turn end")
        self.assertEqual({a: (t["sent"], t["running"]) for a, t in h.status()["tasks"].items()}, {"claude": (False, 0)})
        time.sleep(0.5)
        self.assertNotIn(SAFE, h.output)  # no quiet safe notice
        self.assertIn("NOT safe yet: Claude Code is working, not idle/done (no running group turn", h.ctl("/pause"))
        self.assertEqual(len(h.agent_prompts("claude", 1)), 1)  # nothing sent into the working session

    def test_restart_holds_a_rejoined_turn_without_resend_and_refuses_its_token(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "both silent")
        h.set_status("claude", "working")
        tok = h.token({"text": self.rejoin(h.token(a1)).stdout})
        h.set_status("claude", "idle")  # the callback turn ends while the coordinator is down
        h.reload()
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn(f"Claude Code turn {tok}", h.status()["hold"])
        self.assertIn("not a current input", h.claim(tok).stdout)
        h.set_status("claude", "working")
        self.assertIn("paused or stopped", self.rejoin(h.token(a1)).stdout)
        h.set_status("claude", "idle")
        self.assertIn("skipped", h.ctl("/skip"))
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 2)  # nothing resent

    def test_ask_restricts_eligibility_and_other_agent_catches_up_later(self):
        h = self.start()
        h.ctl("/ask claude q1")
        [a1] = h.agent_prompts("claude", 1)
        self.assertIn("queued (1 waiting)", h.ctl("/ask codex q2"))
        h.ctl("m3")
        h.post(a1, "a1")  # the /ask at the queue head waits until Claude's turn ends
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 1)
        h.finish("claude")
        [c1] = h.agent_prompts("codex", 1)  # Codex was never prompted for q1
        self.assertEqual(seqs(c1), [1, 2, 3])
        self.assertIn("=== #1 user (asked Claude Code only) ===\nq1", c1["text"])
        self.assertNotIn("===\nm3", c1["text"])  # no public message "m3" (the path may hold "m3")
        time.sleep(0.5)
        self.assertEqual(len(h.agent_prompts("claude", 1)), 1)  # q2 is Codex's alone
        h.post(c1, "c2")  # plain m3 joins this release: both agents are addressed again
        c2, a2 = h.agent_prompts("codex", 2)[1], h.agent_prompts("claude", 2)[1]
        self.assertEqual((seqs(c2), c2["wait"], seqs(a2), a2["wait"]), ([5], False, [3, 4, 5], True))
        h.ack(c2)
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all silent")
        self.assertEqual([(m["author"], m["text"], m.get("ask")) for m in h.transcript()],
                         [("user", "q1", "claude"), ("claude", "a1", None), ("user", "q2", "codex"),
                          ("codex", "c2", None), ("user", "m3", None)])

    def test_each_session_receives_only_public_messages_it_has_not_seen(self):
        h = self.start()
        h.ctl("u1")
        h.post(h.agent_prompts("codex", 1)[0], "c2")
        a2 = h.agent_prompts("claude", 2)[1]
        h.finish("codex")
        h.wait(lambda: "codex" not in h.status()["tasks"], "Codex's end")
        h.post(a2, "a3")
        h.finish("claude")
        h.agent_prompts("codex", 2)
        h.finish("codex")
        h.wait(h.idle, "all done")
        h.ctl("u4")
        c3, a3 = h.agent_prompts("codex", 3)[2], h.agent_prompts("claude", 3)[2]
        self.assertNotIn("u1", c3["text"])
        self.assertIn("New public group messages since your last turn (#4-#4", c3["text"])
        self.assertEqual([[seqs(p) for p in h.agent_prompts(a, 3)] for a in AGENT_ORDER],
                         [[[1], [3], [4]], [[1], [2], [4]]])
        self.assertEqual([m["seq"] for m in h.transcript()], [1, 2, 3, 4])  # full history kept
        self.assertIn("│ u1", h.ctl("/history"))

    def test_legacy_state_and_quiet_reload_keep_each_sessions_progress(self):
        # Pre-upgrade chat (full-history prompts, no progress.json, old "first" in config.json).
        h = self.start(transcript=[("user", "u1"), ("codex", "c2"), ("claude", "a3"), ("user", "u4"), ("claude", "a5")])
        self.assertEqual([(e["agent"], e["seen"]) for e in h.events("progress_reconstructed")],
                         [("codex", 2), ("claude", 5)])
        h.ctl("u6")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        self.assertEqual((seqs(c1), seqs(a1)), ([3, 4, 5, 6], [6]))
        h.post(c1, "c7")
        a2 = h.agent_prompts("claude", 2)[1]
        self.assertEqual((seqs(a2), a2["wait"]), ([7], False))  # into Claude's running turn
        h.ack(a2)
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all done")
        h.reload()
        h.ctl("u8")
        c2, a3 = h.agent_prompts("codex", 2)[1], h.agent_prompts("claude", 3)[2]
        self.assertEqual((seqs(c2), seqs(a3)), ([8], [8]))  # Claude saw #7 before the reload
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all silent")
        progress = json.loads((h.state / "progress.json").read_text())
        self.assertEqual(progress, {"codex": {"session": "sess-codex", "seen": 8},
                                    "claude": {"session": "sess-claude", "seen": 8}})
        # progress recorded for another session is never transferred to the bound one
        progress["claude"]["session"] = "sess-old"
        (h.state / "progress.json").write_text(json.dumps(progress))
        h.reload()
        h.ctl("u9")
        self.assertEqual(seqs(h.agent_prompts("claude", 4)[3]), list(range(1, 10)))

    def test_late_codex_session_id_stays_bound_across_reload_and_legacy_upgrade(self):
        def codex_session(h, value):
            agents = json.loads((h.fake / "agents.json").read_text())
            agents[PANES["codex"]]["agent_session"] = {"value": value}
            (h.fake / "agents.json").write_text(json.dumps(agents))

        def late_id_state(h, value, events=""):
            """Real start: config.json keeps Codex's session null; Codex reports `value` later."""
            h.close()
            cfg = json.loads((h.state / "config.json").read_text())
            cfg["identity"]["codex"]["session"] = None
            (h.state / "config.json").write_text(json.dumps(cfg))
            (h.state / "events.jsonl").write_text(events)
            codex_session(h, value)

        def refused_after_switch(h):
            codex_session(h, "sess-Y")  # another native session in the same pane/process
            h.close()
            os.unlink(h.sock)
            r = subprocess.run([sys.executable, str(SCRIPT), "ui", "--state-dir", str(h.state)], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=20, env=dict(os.environ, FAKE_HERDR_DIR=str(h.fake)))
            return r.returncode, r.stdout

        def round_trip(h, n):
            """Codex speaks once; Claude gets it as an update, sends its receipt, and stays silent."""
            c = h.agent_prompts("codex", n)[n - 1]
            codex_session(h, "sess-X")  # native order: progress advances before session_learned
            h.post(c, f"c{n}")
            h.ack(h.agent_prompts("claude", 2 * n)[2 * n - 1])
            h.finish("codex")
            h.finish("claude")
            h.wait(h.idle, "all done")
            return c

        with self.subTest("id learned after first claim, then quiet reload"):
            h = self.start()
            late_id_state(h, None)
            h.reload()
            h.ctl("u1")
            round_trip(h, 1)
            self.assertEqual(json.loads((h.state / "progress.json").read_text())["codex"],
                             {"session": "sess-X", "seen": 2})
            h.reload()
            h.ctl("u3")
            self.assertEqual(seqs(round_trip(h, 2)), [3])
            code, out = refused_after_switch(h)
            self.assertEqual(code, 1)
            self.assertIn("no longer hosts the bound session", out)
            h.close()
        for evidence in (True, False):
            with self.subTest("legacy upgrade", session_learned_event=evidence), tempfile.TemporaryDirectory() as tmp:
                h = self.h = Harness(tmp, transcript=[("user", "u1"), ("codex", "c2"), ("claude", "a3")])
                late_id_state(h, "sess-X", json.dumps({"time": 0, "kind": "session_learned", "agent": "codex",
                                                       "session": "sess-X"}) + "\n" if evidence else "")
                h.reload()
                h.ctl("u4")
                # without identity evidence the old cursor is not bound to the current session
                self.assertEqual(seqs(h.agent_prompts("codex", 1)[0]), [3, 4] if evidence else [1, 2, 3, 4])
                if evidence:
                    round_trip(h, 1)
                    self.assertEqual(refused_after_switch(h)[0], 1)
                h.close()

    def test_undelivered_or_unconfirmed_messages_stay_unseen(self):
        h = self.start(turn_timeout_ms=400)
        h.ctl("u1")
        h.agent_prompts("codex", 1)  # Herdr never confirms either prompt: ambiguous, no claim
        h.agent_prompts("claude", 1)
        h.wait(lambda: h.status()["hold"], "timeout hold")
        time.sleep(0.6)  # both evaluations have timed out (each holds)
        h.set_status("codex", "idle")  # both finished natively without claiming
        h.set_status("claude", "idle")
        self.assertIn("skipped", h.ctl("/skip"))
        self.assertEqual(h.status()["tasks"], {})
        h.ctl("/resume")
        h.ctl("u2")
        c2, a2 = h.agent_prompts("codex", 2)[1], h.agent_prompts("claude", 2)[1]
        self.assertEqual((seqs(c2), seqs(a2)), ([1, 2], [1, 2]))  # #1 resent: delivery never confirmed

    def test_pause_stops_claims_lets_owner_finish_and_resume_reasks(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        self.assertEqual(h.claim(c1).returncode, 0)
        self.assertIn("pausing", h.ctl("/pause"))
        h.submit(h.token(c1), "c")  # the owner completes its speech while paused
        h.finish("codex")
        h.wait(lambda: h.status()["owner"] is None, "release")
        r = h.claim(a1)  # stale anyway; a new evaluation is not dispatched while paused
        self.assertEqual(r.returncode, 2)
        h.finish("claude")
        h.wait(lambda: "PAUSED: no native group turn running" in h.output, "paused")
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 2)
        h.set_status("claude", "working")  # user used Claude's native input while paused
        self.assertIn("not resumed", h.ctl("/resume"))
        h.set_status("claude", "idle")
        agents = json.loads((h.fake / "agents.json").read_text())
        agents[PANES["claude"]]["agent_session"]["value"] = "sess-replaced"
        (h.fake / "agents.json").write_text(json.dumps(agents))
        self.assertIn("no longer hosts the bound session", h.ctl("/resume"))
        agents[PANES["claude"]]["agent_session"]["value"] = "sess-claude"
        (h.fake / "agents.json").write_text(json.dumps(agents))
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        a2 = h.agent_prompts("claude", 2)[1]
        self.assertEqual(seqs(a2), [2])
        h.ctl("/pause")  # a current evaluation claiming while paused is refused and asked again later
        r = h.claim(a2)
        self.assertIn("paused or stopped", r.stdout)
        h.finish("claude")
        h.wait(lambda: not h.status()["tasks"], "evaluation ended")
        self.assertEqual(h.status()["due"], ["claude"])
        h.ctl("/resume")
        a3 = h.agent_prompts("claude", 3)[2]
        self.assertEqual(seqs(a3), [])
        self.assertIn("No new public messages", a3["text"])
        self.assertEqual(h.claim(a3).returncode, 0)

    def test_pause_with_stopped_owner_and_working_peer_is_not_reported_safe(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.claim(c1)
        h.finish("codex")  # the owner ends without submitting: stopped, while Claude still evaluates
        h.wait(lambda: h.status()["hold"], "hold")
        r = h.ctl("/pause")
        self.assertIn("NOT safe", r)
        self.assertIn("Claude Code", r)
        time.sleep(0.5)
        self.assertNotIn(SAFE, h.output)
        h.finish("claude")  # now no native group turn can be running
        h.wait(lambda: SAFE in h.output, "safe notice after the last native turn ended")

    def test_pause_and_exit_during_pre_send_dispatch_are_not_safe(self):
        h = self.start()
        (h.fake / "slow-get").write_text("1")
        h.ctl("q")
        h.wait(lambda: len(h.status()["tasks"]) == 2, "both dispatches in progress")
        self.assertEqual(h.prompts(), [])
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertIn("a group turn is active", h.ctl("/exit"))
        h.agent_prompts("codex", 1)  # dispatches already in progress still send
        h.agent_prompts("claude", 1)
        self.assertNotIn(SAFE, h.output)
        (h.fake / "slow-get").unlink()
        h.finish("codex")
        h.finish("claude")
        h.wait(lambda: SAFE in h.output, "safe notice")
        self.assertIsNone(h.proc.poll())

    def keys(self):
        p = self.h.fake / "keys.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_interrupt_stops_both_turns_without_credit_resend_or_reask(self):
        h = self.start()
        h.ctl("q1")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        self.assertEqual(h.claim(c1).returncode, 0)  # Codex holds a grant it has not used yet
        self.assertIn("interrupting both agents", h.ctl("/interrupt"))
        h.wait(lambda: "INTERRUPTED (Esc)" in h.output, "both confirmed stopped")
        s = h.status()
        self.assertEqual((s["tasks"], s["owner"], s["due"], s["paused"], s["hold"]), ({}, None, [], True, None))
        self.assertEqual(sorted((k["pane"], k["keys"], k["status"]) for k in self.keys()),
                         sorted((p, ["esc"], "working") for p in PANES.values()))
        self.assertEqual(h.submit(h.token(c1), "late").returncode, 2)  # the revoked grant posts nothing
        self.assertIn("not a current input", h.claim(a1).stdout)
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 0)  # not credited
        h.ctl("q2")  # accepted while stopped: queued, nothing dispatched or re-asked
        time.sleep(0.5)
        self.assertEqual((len(h.prompts()), [m["text"] for m in h.transcript()], h.status()["queued"]), (2, ["q1"], 1))
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        c2, a2 = h.agent_prompts("codex", 2)[1], h.agent_prompts("claude", 2)[1]
        self.assertEqual((seqs(c2), seqs(a2)), ([2], [1, 2]))  # Claude's uncredited #1 rides with its next input
        self.assertEqual(len(self.keys()), 2)

    def test_interrupt_before_send_or_with_one_idle_peer_sends_nothing_extra(self):
        h = self.start()
        (h.fake / "slow-get").write_text("")
        h.ctl("q1")
        h.wait(lambda: len(h.status()["tasks"]) == 2, "both dispatches before sending")
        h.ctl("/interrupt")
        h.wait(lambda: "INTERRUPTED (Esc)" in h.output, "stopped", timeout=20)
        (h.fake / "slow-get").unlink()
        self.assertEqual((h.prompts(), self.keys(), len(h.events("dispatch_cancelled"))), ([], [], 2))
        h.reload()  # a restart does not owe the cancelled evaluations either
        time.sleep(0.5)
        self.assertEqual((h.prompts(), h.status()["due"]), ([], []))
        h.ctl("/ask codex q2")
        self.assertEqual(seqs(h.agent_prompts("codex", 1)[0]), [1, 2])
        h.ctl("/interrupt")  # only Codex works: Esc reaches it alone
        h.wait(lambda: not h.status()["halting"] and not h.status()["tasks"], "Codex stopped")
        self.assertEqual([k["pane"] for k in self.keys()], [PANES["codex"]])
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), 1)

    def test_unconfirmed_interrupt_holds_honestly_and_refuses_late_commands(self):
        h = self.start(turn_timeout_ms=3000)
        h.ctl("q1")
        c1 = h.agent_prompts("codex", 1)[0]
        h.agent_prompts("claude", 1)
        (h.fake / f"esc-ignored-{PANES['codex'].replace(':', '_')}").write_text("")
        agents = json.loads((h.fake / "agents.json").read_text())
        agents[PANES["claude"]]["agent_session"]["value"] = "sess-replaced"
        (h.fake / "agents.json").write_text(json.dumps(agents))
        h.ctl("/interrupt")
        self.assertIn("already interrupting", h.ctl("/interrupt"))  # a repeated Esc starts no second round
        self.assertIn("still stopping", h.ctl("/resume"))
        r = h.claim(c1)  # still running natively: denied, and not asked again later
        self.assertIn("interrupted this turn", r.stdout)
        h.wait(lambda: "Esc interrupt not confirmed" in h.output and not h.status()["halting"], "honest hold", timeout=15)
        self.assertIn("no longer hosts the bound session", h.output)
        self.assertTrue(h.status()["hold"])
        keys = self.keys()
        self.assertEqual({k["pane"] for k in keys}, {PANES["codex"]})  # never a key to the replaced pane
        self.assertTrue(1 <= len(keys) <= 3 and all(k["status"] == "working" for k in keys))
        self.assertEqual((h.status()["due"], len(h.prompts())), ([], 2))

    def test_interrupt_lets_an_update_being_written_land_before_esc(self):
        h = self.start()
        h.ctl("q1")
        c1 = h.agent_prompts("codex", 1)[0]
        h.agent_prompts("claude", 1)
        (h.fake / "slow-update").write_text("")
        h.post(c1, "c")  # Claude is owed #2: its update write takes 1 s
        h.wait(lambda: h.events("update"), "update being written")
        h.ctl("/interrupt")
        h.wait(lambda: not h.status()["halting"], "stopped", timeout=20)
        update = next(p for p in h.prompts() if not p["wait"])
        esc = next(k for k in self.keys() if k["pane"] == PANES["claude"])
        self.assertLess(update["time"], esc["time"])
        self.assertIn("no receipt", h.status()["hold"])  # the update's fate is unknown: held, not resent
        self.assertEqual(json.loads((h.state / "progress.json").read_text())["claude"]["seen"], 0)

    def test_timed_out_turns_are_not_safe_after_their_wrapper_threads_stop(self):
        h = self.start(turn_timeout_ms=400)
        h.ctl("q")
        h.agent_prompts("codex", 1)
        h.agent_prompts("claude", 1)
        h.wait(lambda: len(h.status()["tasks"]) == 2 and not any(t["running"] for t in h.status()["tasks"].values()),
               "both waits timed out")  # native sessions still working
        r = h.ctl("/pause")
        self.assertIn("completion unconfirmed", r)
        self.assertIn("a group turn is active", h.ctl("/exit"))
        time.sleep(0.5)
        self.assertNotIn(SAFE, h.output)
        h.set_status("codex", "idle")
        h.set_status("claude", "idle")
        self.assertIn("skipped", h.ctl("/skip"))  # confirms idleness before dropping them
        self.assertEqual(h.ctl("/pause"), SAFE)

    def test_timeout_keeps_unused_grant_and_never_resends_or_wakes_peer(self):
        h = self.start(turn_timeout_ms=400)
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        self.assertEqual(h.claim(c1).returncode, 0)
        h.wait(lambda: h.status()["hold"], "timeout hold")
        self.assertIn("timeout is not cancellation", h.status()["hold"])
        h.set_status("claude", "idle")  # Claude's evaluation ended natively
        h.ctl("/resume")  # Codex still working: reconcile waits (and times out again), no resend
        h.wait(lambda: len([e for e in h.events("native_return") if e["token"] == h.token(c1)]) >= 2, "second wait")
        self.assertEqual(h.status()["owner"], h.token(c1))  # the unused grant stays
        self.assertEqual(len(h.prompts()), 2)
        h.submit(h.token(c1), "slow reply")  # posted while stopped: released, delivered after /resume
        time.sleep(0.6)
        self.assertEqual(len(h.prompts()), 2)
        h.finish("codex")
        h.ctl("/resume")
        a2 = h.agent_prompts("claude", 2)[1]
        self.assertEqual(seqs(a2), [1, 2])  # Claude's timed-out prompt was never confirmed: #1 again
        self.assertEqual(len(h.agent_prompts("codex", 1)), 1)

    def test_blocked_or_replaced_participant_stops_visibly(self):
        h = self.start()
        h.ctl("q")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c")
        h.agent_prompts("claude", 2)
        h.finish("codex", "blocked")
        h.wait(lambda: h.status()["hold"], "blocked hold")
        self.assertIn("blocked", h.status()["hold"])
        self.assertIn("codex", h.status()["tasks"])  # no end guessed on an unknown/blocked state
        h.set_status("codex", "idle")
        h.ack(h.agent_prompts("claude", 2)[1])
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's end")
        agents = json.loads((h.fake / "agents.json").read_text())
        agents[PANES["claude"]]["pgid"] = 200  # agent restarted in the same pane
        (h.fake / "agents.json").write_text(json.dumps(agents))
        self.assertIn("no longer hosts the bound session", h.ctl("/resume"))
        self.assertEqual(len(h.prompts()), 3)
        self.assertIn("STOPPED", h.output)

    def test_dispatcher_error_holds_visibly_and_resume_does_not_resend(self):
        h = self.start()
        (h.fake / "bad-get").write_text("1")
        h.ctl("q")
        h.wait(lambda: h.status()["hold"], "error hold")
        self.assertIn("coordinator error", h.status()["hold"])
        (h.fake / "bad-get").unlink()
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]  # each dispatched once
        (h.fake / "bad-get").write_text("1")  # errors after sending: Claude's update and Codex's end
        h.post(c1, "c")
        h.finish("codex")
        h.wait(lambda: h.status()["hold"], "post-send error hold")
        time.sleep(0.3)
        (h.fake / "bad-get").unlink()
        h.finish("claude")
        h.wait(lambda: "claude" not in h.status()["tasks"], "Claude's silent end")
        h.ctl("/resume")
        self.assertEqual(seqs(h.agent_prompts("claude", 2)[1]), [2])  # the unsent update's content
        self.assertEqual(len(h.agent_prompts("codex", 1)), 1)

    def test_second_coordinator_fails_without_replacing_the_live_socket(self):
        h = self.start()
        r = subprocess.run([sys.executable, str(SCRIPT), "ui", "--state-dir", str(h.state)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=20, env=dict(os.environ, FAKE_HERDR_DIR=str(h.fake)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("another coordinator owns", r.stdout)
        self.assertEqual(h.status()["messages"], 0)  # the first coordinator still serves its socket

    def test_ui_start_shows_the_full_saved_history_without_sending_it(self):
        h = self.start(transcript=[("user", f"u{i}") for i in range(1, 12)] + [("claude", BODY)])
        h.wait(lambda: "public messages above" in h.output, "history")
        for i in range(1, 12):
            self.assertIn(f"│ u{i}\n", h.output)
        for line in BODY.split("\n"):
            self.assertIn("│ " + line, h.output)
        self.assertIn("all 12 public messages above", h.output)
        self.assertEqual((h.prompts(), h.events("progress")), ([], []))  # display only: nothing sent or credited
        self.assertEqual(h.status()["messages"], 12)

    def test_pending_resumed_session_is_credited_only_once_herdr_reports_the_same_id(self):
        for reveal, problem in (("", "has not yet reported Codex's session id"), ("sess-other", "no longer hosts"),
                                ("sess-codex", None)):
            with self.subTest(reveal=reveal), tempfile.TemporaryDirectory() as tmp:
                h = self.h = Harness(tmp, pending=reveal)
                h.ctl("/ask codex u1")
                c1 = h.agent_prompts("codex", 1)[0]
                self.assertEqual(seqs(c1), [1])
                r = h.claim(c1)
                if problem:
                    self.assertIn(problem, r.stdout)
                    self.assertIn("not recorded", h.ack(c1).stdout)
                    h.finish("codex")
                    h.wait(lambda: "nothing is credited" in h.output, "hold")
                    self.assertEqual(h.events("progress"), [])
                    self.assertEqual(h.events("session_confirmed"), [])
                    self.assertTrue(json.loads((h.state / "config.json").read_text())["identity"]["codex"]["pending"])
                else:
                    self.assertIn("GRANTED", r.stdout)
                    h.answer(c1, "c2")
                    h.wait(h.idle, "done")
                    self.assertEqual([e["session"] for e in h.events("session_confirmed")], ["sess-codex"])
                    cfg = json.loads((h.state / "config.json").read_text())
                    self.assertEqual(cfg["identity"]["codex"], {"agent": "codex", "session": "sess-codex",
                                                                "terminal_id": "term-codex", "workspace_id": "w9",
                                                                "pgid": 100})
                    self.assertEqual(json.loads((h.state / "progress.json").read_text())["codex"],
                                     {"session": "sess-codex", "seen": 2})
                h.close()

    def test_reboot_recovery_keeps_history_progress_and_queue_and_holds_unconfirmed_turns(self):
        h = self.start()
        h.ctl("u1")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        h.post(c1, "c2")  # Codex's receipt of #1, then #2 goes into Claude's running turn (no receipt)
        h.agent_prompts("claude", 2)
        self.assertIn("queued", h.ctl("/ask claude u3"))  # accepted; waits for both turns
        n = len(h.prompts())
        h.reboot(2)
        h.wait(lambda: "STOPPED" in h.output, "recovery hold")
        for text in ("recovering the saved group chat", "│ c2", "all 2 public messages", "1 accepted user message",
                     "previous coordinator ended before confirming"):
            self.assertIn(text, h.output)
        cfg = json.loads((h.state / "config.json").read_text())
        self.assertEqual([cfg["identity"][a]["terminal_id"] for a in AGENT_ORDER], ["term-codex-2", "term-claude-2"])
        self.assertEqual(sorted(h.status()["tasks"]), ["claude", "codex"])  # the old turns, held
        self.assertIn("is not a current input", h.claim(a1).stdout)  # old commands refused; no grant recreated
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertEqual(len(h.prompts()), n)  # nothing resent, no model turn from recovery alone
        self.assertTrue(h.ctl("/resume").startswith("resumed: reconciling"))
        a2 = h.agent_prompts("claude", 3)[2]  # the queued /ask: only Claude, with its unconfirmed #1-#2 unseen
        self.assertEqual((seqs(a2), a2["wait"]), ([1, 2, 3], True))
        h.answer(a2, "a4")
        h.wait(h.idle, "all done")
        self.assertEqual(len(h.agent_prompts("codex", 1)), 1)
        h.reload()  # a published queued message is never queued or posted again
        self.assertEqual(h.status()["queued"], 0)
        self.assertEqual([m["text"] for m in h.transcript()], ["u1", "c2", "u3", "a4"])

    def test_repeated_restart_keeps_held_turns_and_an_owed_update_ahead_of_the_queue(self):
        h = self.start()
        h.ctl("u1")
        c1, a1 = h.agent_prompts("codex", 1)[0], h.agent_prompts("claude", 1)[0]
        (h.fake / "slow-get").write_text("")
        h.post(c1, "c2")  # Claude is owed #2 in its running turn; its update stalls in check(): never written
        self.assertIn("queued", h.ctl("u3"))
        time.sleep(0.2)
        self.assertNotIn("update", [e["kind"] for e in h.events()])
        h.reboot(2)
        h.reboot(3)  # a second restart while both turns are still held
        h.wait(lambda: h.status()["hold"], "held again")
        s = h.status()
        self.assertEqual((sorted(s["tasks"]), s["due"], s["queued"]), (["claude", "codex"], ["claude"], 1))
        self.assertIn("is not a current input", h.claim(a1).stdout)
        self.assertIn("NOT safe", h.ctl("/pause"))
        self.assertTrue(h.ctl("/exit").startswith("a group turn is active"))
        self.assertEqual(len(h.prompts()), 2)  # nothing resent or sent while held
        self.assertTrue(h.ctl("/resume").startswith("resumed: reconciling"))
        a2 = h.newest("claude", 2)  # the owed #2 (with the never-confirmed #1 if in a new turn), not u3
        self.assertNotIn(3, seqs(a2))
        self.assertEqual(h.status()["queued"], 1)  # u3 waits behind Claude's owed evaluation

    def test_never_sent_evaluations_continue_after_restart_before_the_restored_queue(self):
        cases = ((("u1", "/ask claude u2"), {"codex": [1], "claude": [1]}, {"claude": [2]}),
                 (("/ask claude u1", "u2"), {"claude": [1]}, {"codex": [1, 2], "claude": [2]}))
        for lines, first, then in cases:
            with self.subTest(lines=lines), tempfile.TemporaryDirectory() as tmp:
                h = self.h = Harness(tmp)
                h.crash_window(*lines)  # the first line is public, the second accepted behind it; nothing sent
                h.reboot(2)
                self.assertEqual({a: seqs(h.agent_prompts(a, 1)[0]) for a in first}, first)  # same /ask scope
                self.assertIn("never sent before the restart", h.output)
                time.sleep(0.3)
                self.assertEqual((len(h.prompts()), h.status()["queued"]), (len(first), 1))  # queue waits
                for a in first:
                    h.finish(a)
                self.assertEqual({a: seqs(h.agent_prompts(a, 1 + (a in first))[-1]) for a in then}, then)
                time.sleep(0.3)
                self.assertEqual(len(h.prompts()), len(first) + len(then))
                h.close()

    def test_restart_keeps_a_skipped_evaluation_dropped_but_owes_later_content(self):
        h = self.start()
        h.set_status("claude", "working")  # Claude's evaluation is never dispatched: its check() fails
        h.ctl("u1")
        h.wait(lambda: h.status()["hold"], "not-dispatched hold")
        h.agent_prompts("codex", 1)
        h.finish("codex")
        h.wait(lambda: "codex" not in h.status()["tasks"], "Codex's end")
        h.set_status("claude", "idle")
        self.assertIn("skipped", h.ctl("/skip"))
        self.assertEqual(h.ctl("/resume"), "resumed", h.output)
        h.reboot(2)
        time.sleep(0.5)
        self.assertEqual((h.status()["due"], len(h.prompts())), ([], 1))  # the skipped evaluation is not revived
        h.crash_window("u2")
        h.reboot(3)
        c2, a1 = h.agent_prompts("codex", 2)[1], h.agent_prompts("claude", 1)[0]
        self.assertEqual((seqs(c2), seqs(a1)), ([2], [1, 2]))  # later content is owed; #1 stays unseen, not lost

    def test_invalid_ask_is_rejected_without_queueing(self):
        h = self.start()
        for line in ("/ask", "/ask codex", "/ask codex   ", "/ask gemini hi", "/ask  hi there"):
            with self.subTest(line):
                self.assertEqual(h.ctl(line), "usage: /ask codex|claude MESSAGE")
        self.assertEqual(h.ctl("/asking codex hi"), "unknown command; /help")
        self.assertEqual(h.ctl("/first claude"), "unknown command; /help")  # ordering control removed
        time.sleep(0.5)
        self.assertEqual((h.prompts(), h.transcript(), h.events("user_queued")), ([], [], []))

    def test_skip_of_stopped_ask_turn_frees_the_chat(self):
        h = self.start()
        h.ctl("/ask codex q")
        [c1] = h.agent_prompts("codex", 1)
        h.claim(c1)
        h.finish("codex")  # claimed, no submission: held
        h.wait(lambda: h.status()["hold"], "hold")
        self.assertIn("skipped Codex turn", h.ctl("/skip"))
        self.assertEqual(h.claim(c1).returncode, 2)  # skipped token is stale
        h.ctl("/resume")
        h.wait(h.idle, "nothing left")  # /ask: Claude is not eligible for q
        h.ctl("next")
        self.assertEqual(seqs(h.agent_prompts("claude", 1)[0]), [1, 2])

    def test_multi_cancel_keeps_coordinator_and_turn(self):
        h = self.start(use_pty=True)
        h.wait(lambda: "group>" in h.read_pty(), "prompt")
        h.type("hello\r")
        c1 = h.agent_prompts("codex", 1)[0]
        h.type("/multi\r")
        h.wait(lambda: h.screen()[-2] == "...", "multi prompt")
        h.type("draft line")
        h.wait(lambda: h.screen()[-2] == "... draft line", "draft")
        h.type("\r")
        h.wait(lambda: h.screen()[-2] == "...", "second multi prompt")
        os.kill(h.proc.pid, signal.SIGINT)  # Ctrl-C abandons the draft only
        h.wait(lambda: "multi-line message cancelled" in h.text(), "cancel")
        self.assertIsNone(h.proc.poll())
        h.answer(c1, "reply")
        h.agent_prompts("claude", 1)
        self.assertEqual([m["text"] for m in h.transcript()], ["hello", "reply"])

    def test_multi_non_tty_eof_cancels_without_busy_loop_or_closing_sessions(self):
        h = self.start(owned=True)
        h.proc.stdin.write("/multi\npartial\n")
        h.proc.stdin.close()  # EOF cancels the draft; stdin EOF then ends the chat, never as /exit
        self.assertEqual(h.proc.wait(timeout=5), 0)
        h.wait(lambda: "native sessions left running" in h.output, "EOF notice")
        self.assertIn("multi-line message cancelled", h.output)
        self.assertEqual((h.transcript(), h.closes()), ([], []))

    def test_interactive_ui_through_pty(self):
        h = self.start(use_pty=True)
        h.wait(lambda: "group>" in h.read_pty(), "prompt")
        h.type("hello from the pty\r")
        a1 = h.agent_prompts("claude", 1)[0]
        h.agent_prompts("codex", 1)
        h.claim(a1)
        h.type("/status\r")  # control accepted while agents run
        h.wait(lambda: "Claude Code turn" in h.text() and "speaking" in "".join(h.screen()), "status")  # wrapped
        h.type("/pause\r")
        h.wait(lambda: "pausing" in h.text(), "pausing")
        h.submit(h.token(a1), "pty reply ✓")
        h.finish("claude")
        h.finish("codex")
        h.wait(lambda: "PAUSED" in h.text(), "paused")
        self.assertEqual(len(h.prompts()), 2)
        h.type("/history\r")
        h.wait(lambda: h.text().count("pty reply ✓") >= 2, "history")
        h.type("/resume\r")
        h.agent_prompts("codex", 2)
        h.type("/exi")
        h.wait(lambda: "/exit!" in h.text(), "slash menu")
        h.type("\x1b[B")  # Down selects /exit; Enter only fills the line
        h.type("\r")
        time.sleep(0.5)
        self.assertNotIn("a group turn is active", h.text())
        self.assertEqual(h.screen()[-2], "group> /exit")
        h.type("\r")  # the next Enter executes it
        h.wait(lambda: "a group turn is active" in h.text(), "exit refusal")
        h.type("/exit!\r")  # attached (--codex/--claude) sessions are never closed
        self.assertEqual(h.proc.wait(timeout=5), 0)
        self.assertIn("attached native Codex and Claude Code sessions are left running", h.read_pty())
        self.assertEqual(h.closes(), [])
        out = h.read_pty()  # Shift+Enter reporting (modifyOtherKeys 1) on for the editor, default again after
        self.assertLess(out.index("\x1b[>4;1m"), out.index("group>"))
        self.assertGreater(out.rindex("\x1b[>4m"), out.rindex("group>"))
        self.assertGreater(out.rindex("\x1b[?1000l"), out.rindex("group>"))  # mouse reporting off again

    def test_pty_shows_markdown_tables_while_transcript_prompts_and_draft_keep_raw_text(self):
        h = self.start(use_pty=True, transcript=[("claude", TABLE)], size=(60, 100))
        h.wait(lambda: "public messages above" in h.text(), "history")
        h.type("go\r")
        c1 = h.agent_prompts("codex", 1)[0]
        h.type("draft\x1b[27;2;13~kept")  # Shift+Enter as Herdr sends it under modifyOtherKeys 1; not sent
        time.sleep(0.3)
        h.post(c1, TABLE)
        h.wait(lambda: h.text().count("Global accuracy") >= 2, "live table")
        self.assertIn(TABLE, h.newest("claude", 3)["text"])  # agents get the raw Markdown
        screen = h.screen()
        self.assertEqual(screen[-3:-1], ["group> draft", "kept"])  # Shift+Enter's line break, in the draft
        h.type("\r")  # Enter sends the two-line draft (queued behind the running turns)
        h.wait(lambda: len(h.events("user_queued")) == 2, "draft sent")
        self.assertEqual([e["text"] for e in h.events("user_queued")], ["go", "draft\nkept"])
        self.assertEqual([m["text"] for m in h.transcript()], [TABLE, "go", TABLE])
        self.assertEqual(sum("Global accuracy" in l for l in screen), 2)
        for start in [i for i, l in enumerate(screen) if "Global accuracy" in l]:
            head, rule = screen[start], screen[start + 1]
            self.assertIn("─" * 20, rule)
            rows = list(itertools.takewhile(lambda l: l.startswith("│ "), screen[start + 2:]))
            self.assertEqual(head.index("accuracy") + 8, rows[0].index("0.812") + 5)  # right-aligned column
            self.assertIn(NOTE.replace(" ", ""), "".join(l.replace(" ", "").lstrip("│") for l in rows))
            self.assertIn("ID2-a-very-long-identifier-without-spaces", "".join(l.split()[1] for l in rows[2:]))
        self.assertNotIn("|---", "".join(screen))
        self.assertNotIn("…", "".join(screen))

    def test_pty_reading_view_holds_its_place_and_esc_closes_the_menu_before_interrupting(self):
        h = self.start(use_pty=True, transcript=[("user", f"old-{i:02d}") for i in range(1, 31)])
        h.wait(lambda: "public messages above" in h.text(), "history")
        h.type("draft text")
        h.wait(lambda: "group> draft text" in h.screen(), "draft")
        h.type("\x1b[5~")  # PgUp: read older output
        h.wait(lambda: "▲ earlier output" in h.text(), "reading notice")
        view = h.screen()[:-3]  # above the notice, input and status lines
        h.ctl("news-1")
        c1 = h.agent_prompts("codex", 1)[0]
        h.agent_prompts("claude", 1)
        h.post(c1, "reply-1")
        h.wait(lambda: "2 new messages" in h.text(), "unread count")
        self.assertEqual(h.screen()[:-3], view)  # nothing moved or shifted
        self.assertNotIn("reply-1", h.text())
        self.assertEqual(h.screen()[-2], "group> draft text")
        h.type("\x1b[1;5F")  # Ctrl+End: back to the latest
        h.wait(lambda: "reply-1" in h.text() and "▲ earlier output" not in h.text(), "latest")
        h.type("\x1b[5~")
        h.wait(lambda: "▲ earlier output" in h.text(), "reading again")
        h.type("\x1b[6~" * 3)  # PgDn reaching the end follows new output again
        h.wait(lambda: "▲ earlier output" not in h.text() and "reply-1" in h.text(), "following")
        sent = len(h.prompts())
        h.type("\x15/int")  # Ctrl+U clears the draft; a slash draft opens the menu
        h.wait(lambda: "interrupt both agents now" in h.text(), "menu")
        h.type("\x1b")  # the first Esc only closes the menu
        h.wait(lambda: "interrupt both agents now" not in h.text(), "menu closed")
        time.sleep(1)
        self.assertEqual((h.events("interrupt"), self.keys()), ([], []))
        h.type("\x1b")  # the next Esc interrupts both agents (still evaluating news-1)
        h.wait(lambda: "INTERRUPTED (Esc)" in h.text(), "interrupted")
        self.assertEqual(h.screen()[-2], "group> /int")  # the draft stays
        self.assertEqual(sorted(k["pane"] for k in self.keys()), sorted(PANES.values()))
        time.sleep(0.5)
        self.assertEqual(len(h.prompts()), sent)

    def test_pty_mouse_wheel_scrolls_the_view_in_small_steps_and_keeps_the_draft(self):
        long = "wrapped " + "w" * 150  # two rows at 100 columns
        h = self.start(use_pty=True, transcript=[("user", t) for t in
                                                 [f"old-{i:02d}" for i in range(1, 26)] + [long, "old-26", "old-27"]])
        h.wait(lambda: "public messages above" in h.text(), "history")
        self.assertIn("\x1b[?1006h", h.read_pty())  # SGR mouse reporting on: the terminal sends wheel reports
        h.type("draft text")
        h.wait(lambda: "group> draft text" in h.screen(), "draft")
        wheel = lambda up, row=5: h.type(f"\x1b[<{64 if up else 65};10;{row}M")  # SGR wheel report, 1-based
        follow = h.screen()[:-2]
        wheel(True)
        h.wait(lambda: "▲ earlier output" in h.text(), "reading notice")
        view = h.screen()[:-3]
        self.assertEqual(view[3:], follow[:len(view) - 3])  # three rows older, not a page
        wheel(True)
        h.wait(lambda: h.screen()[:-3] != view, "second step")
        self.assertEqual(h.screen()[3:-3], view[:-3])
        view = h.screen()[:-3]
        wheel(True, row=23)  # over the input line: neither the view nor the draft changes
        h.ctl("news-1")
        c1 = h.agent_prompts("codex", 1)[0]
        h.agent_prompts("claude", 1)
        h.post(c1, "reply-1")
        h.wait(lambda: "2 new messages" in h.text(), "unread count")
        self.assertEqual(h.screen()[:-3], view)  # new output keeps the place
        self.assertEqual(h.screen()[-2], "group> draft text")
        for _ in range(8):  # down until the end: following again, newest output shown
            wheel(False)
        h.wait(lambda: "▲ earlier output" not in h.text() and "reply-1" in h.text(), "following")
        self.assertEqual(h.screen()[-2], "group> draft text")
        self.assertEqual([e["text"] for e in h.events("user_queued")], ["news-1"])  # wheel bytes sent nothing
        self.assertEqual((h.events("interrupt"), self.keys()), ([], []))
        h.type("!\r")  # the draft and caret are intact: typing continues it
        h.wait(lambda: len(h.events("user_queued")) == 2, "draft sent")
        self.assertEqual(h.events("user_queued")[-1]["text"], "draft text!")

    def test_markdown_display_keeps_every_value_and_literal_text(self):
        render = load_script().markdown_renderer
        plain = render(False)
        lines = plain("| a | b | c | d |\n|---|---|---|---|\n| 1 | 2 | 3 | 4 |\n| 5 | 6 | 7 | 8 |", 20)
        self.assertEqual([re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines],  # too narrow: header: value
                         ["a: 1", "b: 2", "c: 3", "d: 4", "", "a: 5", "b: 6", "c: 7", "d: 8"])
        extra = "| a | b |\n|---|---|\n| 1 | 2 | 3 |"
        self.assertEqual(plain(extra, 60), extra.split("\n"))  # GFM would drop the 3: shown as written
        self.assertEqual(plain("one\ntwo <b>x</b> ![alt](img.png) [doc](http://e.org) 2*3*4 **open", 98),
                         ["one", "two <b>x</b> !alt (\x1b[4mimg.png\x1b[0m) doc (\x1b[4mhttp://e.org\x1b[0m) "
                                 "2\x1b[3m3\x1b[0m4 **open"])
        sgr = lambda lines: set(re.findall(r"\x1b\[([0-9;]*)m", "".join(lines)))
        self.assertTrue({"36", "1;36;40"} & sgr(render(True)(TABLE + "\n\n`x`", 98)))
        self.assertLessEqual(sgr(plain(TABLE + "\n\n# h\n\n`x`\n\n```py\nx = 1\n```", 98)), {"0", "1", "3", "4", "1;4"})

    def test_exit_closes_only_owned_bound_panes_once_idle(self):
        h = self.start(owned=True)  # Codex bound with a null session id (pgid/terminal still bind it)
        h.ctl("q")
        h.agent_prompts("codex", 1)
        h.agent_prompts("claude", 1)
        self.assertIn("a group turn is active", h.ctl("/exit"))
        h.finish("codex")
        h.finish("claude")
        h.wait(h.idle, "all silent")
        h.set_status("claude", "working")  # the user started a native turn there
        self.assertIn("not exited: Claude Code is working", h.ctl("/exit"))
        self.assertEqual(h.closes(), [])
        h.set_status("claude", "idle")
        h.line("/exit")
        self.assertEqual(h.proc.wait(timeout=10), 0)
        h.wait(lambda: "group chat ended" in h.output, "exit reply")
        self.assertIn(f"closed Codex pane {PANES['codex']} and Claude Code pane {PANES['claude']}", h.output)
        self.assertEqual(h.closes(), [PANES["codex"], PANES["claude"]])
        self.assertEqual([m["text"] for m in h.transcript()], ["q"])  # state kept, socket gone
        self.assertTrue((h.state / "progress.json").exists())
        self.assertFalse(os.path.exists(h.sock))

    def test_forced_exit_closes_only_still_bound_panes_and_reports_failures(self):
        with self.subTest("running turns are interrupted, nothing dispatched after"):
            h = self.start(owned=True)
            h.ctl("q")
            h.agent_prompts("codex", 1)
            h.agent_prompts("claude", 1)
            h.line("/exit!")
            self.assertEqual(h.proc.wait(timeout=10), 0)
            h.wait(lambda: "group chat ended" in h.output, "exit reply")
            self.assertEqual(h.closes(), [PANES["codex"], PANES["claude"]])
            self.assertIn("group chat ended; closed Codex pane", h.output)
            self.assertNotIn("STOPPED", h.output)
            self.assertEqual(len(h.prompts()), 2)
            h.close()
        with self.subTest("replaced session kept, failed close reported once"), tempfile.TemporaryDirectory() as tmp:
            h = self.h = Harness(tmp, owned=True)
            agents = json.loads((h.fake / "agents.json").read_text())
            agents[PANES["codex"]]["pgid"] = 200  # another process now occupies Codex's pane
            (h.fake / "agents.json").write_text(json.dumps(agents))
            (h.fake / "close-fail").write_text("")
            self.assertIn("not exited: Codex pane", h.ctl("/exit"))
            h.line("/exit!")
            self.assertEqual(h.proc.wait(timeout=10), 0)
            h.wait(lambda: "group chat ended" in h.output, "exit reply")
            self.assertEqual(h.closes(), [PANES["claude"]])  # never the replaced occupant; no retry
            self.assertIn("not every owned pane was closed: closed none", h.output)
            self.assertIn(f"Codex pane {PANES['codex']} (Codex pane {PANES['codex']} no longer hosts", h.output)
            self.assertIn("close failed); it may still be open", h.output)


MUTATING = {("pane", "split"), ("pane", "swap"), ("pane", "move"), ("pane", "run"), ("pane", "close"), ("pane", "focus"),
            ("agent", "start"), ("agent", "prompt")}


class StartTests(unittest.TestCase):
    """`start` against the fake Herdr: fresh launch from the caller's tab, and legacy binding."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.fake = self.d / "fake"
        self.fake.mkdir()
        self.herdr = self.d / "herdr"
        self.herdr.write_text(f"#!{sys.executable}\n" + FAKE_HERDR)
        self.herdr.chmod(0o755)
        (self.fake / "agents.json").write_text("{}")
        (self.fake / "pane-count").write_text("1")
        # New panes n1 (left) and n2 (right) sit above the caller w9:p0 after split + swap + split.
        self.layout({"w9:n1": (0, 0, 87, 36), "w9:n2": (87, 0, 86, 36), "w9:p0": (0, 36, 173, 16)})
        self.state = self.d / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def layout(self, rects):
        panes = [{"pane_id": p, "rect": dict(zip(("x", "y", "width", "height"), r))} for p, r in rects.items()]
        (self.fake / "layout.json").write_text(json.dumps(
            {"area": {"x": 0, "y": 0, "width": 173, "height": 52}, "panes": panes}))

    def start(self, *args):
        env = dict(os.environ, FAKE_HERDR_DIR=str(self.fake), FAKE_CWD=str(self.d),
                   HERDR_ENV="1", HERDR_PANE_ID="w9:p0")
        return subprocess.run([sys.executable, str(SCRIPT), "start", "--herdr-bin", str(self.herdr),
                               "--state-dir", str(self.state), "--cwd", str(self.d), *args],
                              input="", capture_output=True, text=True, env=env, timeout=30)

    def mutations(self):
        p = self.fake / "calls.jsonl"
        calls = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if tuple(c[:2]) in MUTATING]

    def test_fresh_launch_splits_caller_tab_starts_both_and_runs_ui_in_place(self):
        (self.fake / "tab-focused").write_text("")  # the user is looking at the caller tab
        r = self.start("--effort", "low", "--allow-submit")
        self.assertEqual(r.returncode, 0, r.stderr)  # stdin EOF quits the in-place UI
        m = self.mutations()
        self.assertEqual([c[:3] for c in m[:3]], [["pane", "split", "w9:p0"], ["pane", "swap", "--source-pane"],
                                                  ["pane", "split", "w9:n1"]])
        self.assertEqual(m[1][3:], ["w9:p0", "--target-pane", "w9:n1"])
        starts = {c[c.index("--kind") + 1]: c for c in m[3:5]}
        # Two agent starts, then focus back down to the shared input; no pane run: the UI runs here.
        self.assertEqual(m[5:], [["pane", "focus", "--direction", "down", "--pane", "w9:n1"]])
        self.assertEqual(starts["codex"][starts["codex"].index("--pane") + 1], "w9:n1")
        self.assertEqual(starts["codex"][starts["codex"].index("--") + 1:],
                         ["--model", "gpt-6-astra", "-c", "model_reasoning_effort=low"])
        self.assertEqual(starts["claude"][starts["claude"].index("--pane") + 1], "w9:n2")
        self.assertEqual(starts["claude"][starts["claude"].index("--") + 1:],
                         ["--model", "claude-fable-5-1", "--effort", "low", "--allowedTools",
                          f"Bash({sys.executable} {SCRIPT} claim:*)", f"Bash({sys.executable} {SCRIPT} submit:*)",
                          f"Bash({sys.executable} {SCRIPT} ack:*)", f"Bash({sys.executable} {SCRIPT} rejoin:*)"])
        cfg = json.loads((self.state / "config.json").read_text())
        self.assertEqual((cfg["panes"], cfg["bottom_pane"]), ({"codex": "w9:n1", "claude": "w9:n2"}, "w9:p0"))
        self.assertIn('"kind": "ui_start"', (self.state / "events.jsonl").read_text())

    def test_installed_command_means_start_in_the_invocation_cwd(self):
        """Packaging: bare `herdr-chat` options mean `start`; panes and agents use the caller's cwd, not the install."""
        cmd = shutil.which("herdr-chat", path=str(Path(sys.executable).parent))
        if not cmd:
            self.skipTest("herdr-chat is not installed beside this interpreter")
        work = (self.d / "work").resolve()
        work.mkdir()
        env = dict(os.environ, FAKE_HERDR_DIR=str(self.fake), FAKE_CWD=str(work), HERDR_ENV="1", HERDR_PANE_ID="w9:p0")
        r = subprocess.run([cmd, "--herdr-bin", str(self.herdr), "--state-dir", str(self.state)], cwd=work,
                           input="", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        splits = [c for c in self.mutations() if c[:2] == ["pane", "split"]]
        self.assertEqual([c[c.index("--cwd") + 1] for c in splits], [str(work)] * 2)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["cwd"], str(work))

    def test_invalid_start_context_fails_before_any_mutation(self):
        cases = {
            "only one legacy pane": (lambda: None, ("--codex", "w9:x"), "pass both"),
            "agent tool shell": (lambda: (self.fake / "agents.json").write_text(
                json.dumps({"w9:p0": {"agent": "codex"}})), (), "hosts codex"),
            "not foreground": (lambda: (self.fake / "not-foreground").write_text(""), (), "not the foreground"),
            "occupied tab": (lambda: (self.fake / "pane-count").write_text("3"), (), "has 3 panes"),
            "used state dir": (lambda: (self.state.mkdir(), (self.state / "config.json").write_text("{}")),
                               (), "already used"),
        }
        for name, (setup, args, message) in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                setup()
                r = self.start(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(message, r.stderr)
                self.assertEqual(self.mutations(), [])

    def test_failed_native_startup_reports_created_panes_and_does_not_start_chat(self):
        (self.fake / "start-fail-claude").write_text("")
        r = self.start()
        self.assertNotEqual(r.returncode, 0)
        for text in ("Group chat NOT started", "w9:n1", "w9:n2", "agent_not_ready", "herdr pane close"):
            self.assertIn(text, r.stderr)
        self.assertFalse((self.state / "config.json").exists())
        kinds = [c[c.index("--kind") + 1] for c in self.mutations() if c[:2] == ["agent", "start"]]
        self.assertEqual(sorted(kinds), ["claude", "codex"])  # no retry, nothing closed
        self.assertFalse(any(c[:2] in (["pane", "close"], ["pane", "focus"]) for c in self.mutations()))

    def saved(self, name, panes=("w9:a", "w9:b"), codex_session="sess-codex"):
        """A saved group whose shared pane is the caller w9:p0, under the default state root of a test HOME.
        Codex's session id is null in config.json and was recorded later in progress.json (a real fresh launch)."""
        state = self.d / "home/.local/state/herdr-group-chat" / name
        state.mkdir(parents=True)
        ident = {a: {"agent": a, "session": None if a == "codex" else "sess-claude", "terminal_id": f"term-{a}-old",
                     "workspace_id": "w9", "pgid": 1} for a in AGENT_ORDER}
        (state / "config.json").write_text(json.dumps({
            "herdr_bin": str(self.herdr), "herdr_socket": "/fake/herdr.sock", "socket": str(state / "s"),
            "panes": dict(zip(AGENT_ORDER, panes)), "bottom_pane": "w9:p0", "tab_id": "w9:t1", "identity": ident,
            "native_argv": None, "turn_timeout_ms": 60000}))
        (state / "progress.json").write_text(json.dumps({"codex": {"session": codex_session, "seen": 1},
                                                         "claude": {"session": "sess-claude", "seen": 1}}))
        (state / "transcript.jsonl").write_text(json.dumps({"seq": 1, "time": 0, "author": "user", "text": "hi"}) + "\n")
        return state

    def bare(self, *args):
        env = dict(os.environ, FAKE_HERDR_DIR=str(self.fake), FAKE_CWD=str(self.d), HERDR_ENV="1", HERDR_PANE_ID="w9:p0",
                   HOME=str(self.d / "home"), HERDR_SOCKET_PATH="/fake/herdr.sock")
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--herdr-bin", str(self.herdr)], input="",
                              capture_output=True, text=True, env=env, timeout=30)

    def test_bare_start_recovers_only_a_unique_matching_saved_group(self):
        def resumed(claude_session="sess-claude"):  # Herdr resumed both sessions with new terminal/process ids
            agents = {p: {"agent": a, "agent_session": {"value": s}, "terminal_id": f"term-{a}-new", "pgid": 300,
                          "workspace_id": "w9", "tab_id": "w9:t1"}
                      for p, a, s in (("w9:a", "codex", "sess-codex"), ("w9:b", "claude", claude_session))}
            (self.fake / "agents.json").write_text(json.dumps(agents))
            for p in agents:
                (self.fake / f"status-{p.replace(':', '_')}").write_text("idle")
            (self.fake / "pane-count").write_text("3")

        def unchanged(state):
            cfg = json.loads((state / "config.json").read_text())
            return cfg["identity"]["claude"]["terminal_id"] == "term-claude-old" and not (state / "events.jsonl").exists()

        cases = {"replaced Claude session": (lambda: (resumed("sess-other"), self.saved("g1")), "hosts"),
                 "Codex id never recorded": (lambda: (resumed(), self.saved("g1", codex_session=None)), "never recorded"),
                 "two candidates": (lambda: (resumed(), self.saved("g1"), self.saved("g2")), "g2")}
        for name, (setup, message) in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                setup()
                r = self.bare("start")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("cannot be recovered automatically", r.stderr)
                self.assertIn(message, r.stderr)
                self.assertEqual(self.mutations(), [])
                self.assertTrue(unchanged(self.d / "home/.local/state/herdr-group-chat/g1"))
        with self.subTest("explicit choice, then a live coordinator"):
            r = self.bare("recover", "--state-dir", str(self.d / "home/.local/state/herdr-group-chat/g2"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            g1, g2 = (self.d / "home/.local/state/herdr-group-chat" / g for g in ("g1", "g2"))
            self.assertTrue(unchanged(g1))
            with open(g1 / "coordinator.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                r = self.bare("recover", "--state-dir", str(g1))
            self.assertIn("another coordinator owns", r.stdout)
            self.assertTrue(unchanged(g1))
        with self.subTest("unique match beside an ended group"):
            self.tearDown()
            self.setUp()
            resumed()
            state, ended = self.saved("g1"), self.saved("g0", panes=("w9:x", "w9:y"))
            r = self.bare("start")
            self.assertEqual(r.returncode, 0, r.stderr)  # stdin EOF ends the recovered UI
            self.assertIn(f"recovering the saved group chat in {state}", r.stdout)
            self.assertIn("│ hi", r.stdout)
            self.assertEqual(self.mutations(), [])
            cfg = json.loads((state / "config.json").read_text())
            self.assertEqual(cfg["identity"], {a: {"agent": a, "session": f"sess-{a}", "terminal_id": f"term-{a}-new",
                                                   "workspace_id": "w9", "pgid": 300} for a in AGENT_ORDER})
            self.assertEqual([e["kind"] for e in map(json.loads, (state / "events.jsonl").read_text().splitlines())
                              if e["kind"] in ("rebind", "ui_start")], ["rebind", "ui_start"])
            self.assertTrue(unchanged(ended))
        with self.subTest("ended group only, empty tab: fresh launch"):
            self.tearDown()
            self.setUp()
            self.saved("g0", panes=("w9:x", "w9:y"))
            r = self.bare("start", "--cwd", str(self.d))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.mutations()[0][:3], ["pane", "split", "w9:p0"])

    HISTORY = [("user", f"u{i}") if i % 2 else ("codex", f"c{i}") for i in range(1, 12)] + [("claude", BODY)]

    def group(self, name, last=100, codex_session="sess-codex", legacy=False):
        """A group whose agent panes w9:a/w9:b (tab w9:t0) are gone, with 12 public messages; Codex's id was
        recorded in progress.json only, as in a real fresh launch. legacy: the shape of an older group bound with
        --codex/--claude: no native_argv, cwd or progress.json, Codex's id only in a session_learned event."""
        state = self.d / "home/.local/state/herdr-group-chat" / name
        state.mkdir(parents=True)
        ident = {a: {"agent": a, "session": None if a == "codex" else "sess-claude", "terminal_id": f"term-{a}-old",
                     "workspace_id": "w9", "pgid": 1} for a in AGENT_ORDER}
        argv = {"codex": ["--model", "m", "-c", "model_reasoning_effort=low"], "claude": ["--model", "m", "--effort", "low"]}
        cfg = {"herdr_bin": str(self.herdr), "herdr_socket": "/fake/herdr.sock", "socket": str(state / "s"),
               "panes": {"codex": "w9:a", "claude": "w9:b"}, "bottom_pane": "w9:q0", "tab_id": "w9:t0",
               "identity": ident, "native_argv": argv, "cwd": str(self.d), "turn_timeout_ms": 60000}
        if legacy:
            del cfg["native_argv"], cfg["cwd"]
            if codex_session:
                (state / "events.jsonl").write_text(json.dumps({"time": 1, "kind": "session_learned", "agent": "codex",
                                                                "session": codex_session}) + "\n")
        else:
            (state / "progress.json").write_text(json.dumps({"codex": {"session": codex_session, "seen": 12},
                                                             "claude": {"session": "sess-claude", "seen": 12}}))
        (state / "config.json").write_text(json.dumps(cfg))
        (state / "transcript.jsonl").write_text("".join(
            json.dumps({"seq": i + 1, "time": last - 12 + i, "author": a, "text": t}) + "\n"
            for i, (a, t) in enumerate(self.HISTORY)))
        return state

    def files(self, state):
        return {f.name: f.read_text() for f in sorted(state.iterdir()) if f.is_file() and f.name != "coordinator.lock"}

    def env(self):
        return dict(os.environ, FAKE_HERDR_DIR=str(self.fake), FAKE_CWD=str(self.d), HERDR_ENV="1", HERDR_PANE_ID="w9:p0",
                    HOME=str(self.d / "home"), HERDR_SOCKET_PATH="/fake/herdr.sock")

    def resume(self, stdin, *args):
        cmd = [sys.executable, str(SCRIPT), "resume", *args, "--herdr-bin", str(self.herdr)]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=self.env(), timeout=30, cwd=self.d)

    def test_resume_selector_lists_saved_conversations_and_cancel_or_invalid_choice_change_nothing(self):
        old, new = self.group("g1"), self.group("g2", last=500)
        (new / "transcript.jsonl").write_text(json.dumps({"seq": 1, "time": 500, "author": "user",
                                                          "text": "hello group\nsecond line"}) + "\n")
        snapshot = {g: self.files(g) for g in (old, new)}
        r = self.resume("")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [l for l in r.stdout.splitlines() if "messages" in l]
        self.assertEqual(len(rows), 2)
        self.assertRegex(rows[0], r"^  1\. \S+ \S+ +1 messages  hello group  \[g2\]$")  # newest first, first line
        self.assertRegex(rows[1], r"^  2\. \S+ \S+ +12 messages  u1  \[g1\]$")
        self.assertIn("cancelled; nothing changed", r.stdout)
        for choice in ("3\n", "x\n"):
            r = self.resume(choice)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("invalid choice", r.stderr)
        self.assertEqual(self.mutations(), [])
        for g in (old, new):
            self.assertEqual(self.files(g), snapshot[g])
            self.assertFalse((g / "coordinator.lock").exists())

    def test_resume_continues_a_closed_group_in_a_new_tab_with_the_same_native_sessions(self):
        state = self.group("g1")
        before = self.files(state)
        env, out = self.env(), []

        def launch(*cmd, stdin=""):
            proc = subprocess.Popen([sys.executable, str(SCRIPT), *cmd, "--herdr-bin", str(self.herdr)], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            proc.stdin.write(stdin)
            proc.stdin.flush()
            threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True).start()
            return proc

        def until(pred, what):
            end = time.time() + 10
            while not pred():
                self.assertLess(time.time(), end, f"timed out waiting for {what}: {''.join(out)}")
                time.sleep(0.05)

        def run(*cmd, stdin=None, pane=""):
            return subprocess.run([sys.executable, str(SCRIPT), *cmd], input=stdin, capture_output=True, text=True,
                                  env=dict(env, HERDR_PANE_ID=pane), timeout=20)

        def prompts():
            p = self.fake / "prompts.jsonl"
            return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

        events = lambda: [json.loads(l) for l in (state / "events.jsonl").read_text().splitlines()]
        proc = launch("resume", stdin="1\n")  # the history selector's only entry
        until(lambda: "public messages above" in "".join(out), "reopened UI")
        starts = {c[c.index("--kind") + 1]: c[c.index("--") + 1:] for c in self.mutations() if c[:2] == ["agent", "start"]}
        self.assertEqual(starts, {"codex": ["resume", "sess-codex", "--model", "m", "-c", "model_reasoning_effort=low"],
                                  "claude": ["--resume", "sess-claude", "--model", "m", "--effort", "low"]})
        cfg = json.loads((state / "config.json").read_text())
        self.assertEqual((cfg["panes"], cfg["bottom_pane"], cfg["tab_id"]), ({"codex": "w9:n1", "claude": "w9:n2"},
                                                                             "w9:p0", "w9:t1"))
        self.assertEqual({a: (cfg["identity"][a]["session"], cfg["identity"][a].get("pending")) for a in AGENT_ORDER},
                         {"codex": ("sess-codex", True), "claude": ("sess-claude", None)})
        self.assertEqual({k: v for k, v in self.files(state).items() if k in ("transcript.jsonl", "progress.json")},
                         {k: before[k] for k in ("transcript.jsonl", "progress.json")})
        self.assertEqual([e["kind"] for e in events()], ["reopen", "ui_start"])
        text = "".join(out)
        for _, t in self.HISTORY:
            for line in t.split("\n"):
                self.assertIn("│ " + line, text)
        self.assertIn("all 12 public messages above", text)
        self.assertIn("Codex resumed its recorded session sess-codex", text)
        self.assertEqual(prompts(), [])  # history is display only
        proc.kill()  # crash before Codex's first turn: the same pending process is recovered in the new shared pane
        proc.wait()
        out.clear()
        proc = launch("start")
        until(lambda: "public messages above" in "".join(out), "recovered UI")
        self.assertIn(f"recovering the saved group chat in {state}", "".join(out))
        self.assertNotIn("rebind", [e["kind"] for e in events()])
        sock = str(state / "s")
        run("ctl", "--socket", sock, "/ask codex u13")
        until(lambda: prompts(), "Codex prompt")
        c13 = prompts()[0]
        self.assertEqual((c13["pane"], [int(n) for n in re.findall(r"^=== #(\d+) ", c13["text"], re.M)]), ("w9:n1", [13]))
        token = re.search(r"--turn (\S+)", c13["text"]).group(1)
        self.assertIn("GRANTED", run("claim", "--socket", sock, "--turn", token, pane="w9:n1").stdout)
        self.assertIn("received as group message #14", run("submit", "--socket", sock, "--turn", token,
                                                           stdin="c14\n", pane="w9:n1").stdout)
        (self.fake / "finish-w9_n1").write_text("done")
        until(lambda: "turn_end" in [e["kind"] for e in events()], "turn end")
        self.assertEqual([e["session"] for e in events() if e["kind"] == "session_confirmed"], ["sess-codex"])
        self.assertNotIn("pending", json.loads((state / "config.json").read_text())["identity"]["codex"])
        self.assertEqual(json.loads((state / "progress.json").read_text())["codex"], {"session": "sess-codex", "seen": 14})
        self.assertEqual(len(prompts()), 1)  # no replay, Claude not asked
        self.assertIn("closed Codex pane w9:n1 and Claude Code pane w9:n2", run("ctl", "--socket", sock, "/exit").stdout)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)
        closes = [c[2] for c in self.mutations() if c[:2] == ["pane", "close"]]
        self.assertEqual(closes, ["w9:n1", "w9:n2"])  # only the panes this resume created

    def test_resume_continues_a_legacy_attached_group_without_invented_launch_options(self):
        state = self.group("g1", legacy=True)
        before, env, out = self.files(state), self.env(), []
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "resume", "--state-dir", str(state), "--herdr-bin",
                                 str(self.herdr)], env=env, cwd=self.d, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True).start()
        events = lambda: [json.loads(l) for l in (state / "events.jsonl").read_text().splitlines()]
        prompts = lambda: [json.loads(l) for l in (self.fake / "prompts.jsonl").read_text().splitlines()]
        ctl = lambda line: subprocess.run([sys.executable, str(SCRIPT), "ctl", "--socket", str(state / "s"), line],
                                          capture_output=True, text=True, env=env, timeout=20).stdout

        def until(pred, what):
            end = time.time() + 10
            while not pred():
                self.assertLess(time.time(), end, f"timed out waiting for {what}: {''.join(out)}")
                time.sleep(0.05)
        until(lambda: "public messages above" in "".join(out), "reopened UI")
        self.assertIn("launch options were never recorded; resuming without overrides", "".join(out))
        starts = {c[c.index("--kind") + 1]: c[c.index("--") + 1:] for c in self.mutations() if c[:2] == ["agent", "start"]}
        self.assertEqual(starts, {"codex": ["resume", "sess-codex"], "claude": ["--resume", "sess-claude"]})
        cfg = json.loads((state / "config.json").read_text())
        self.assertEqual((cfg["panes"], cfg["native_argv"], cfg["identity"]["codex"]["pending"]),
                         ({"codex": "w9:n1", "claude": "w9:n2"}, {"codex": [], "claude": []}, True))
        self.assertEqual(self.files(state)["transcript.jsonl"], before["transcript.jsonl"])
        reopen = [e for e in events() if e["kind"] == "reopen"]
        self.assertEqual([(e["old"]["native_argv"], e["argv"]) for e in reopen], [(None, starts)])
        self.assertFalse((self.fake / "prompts.jsonl").exists())  # history is display only
        ctl("/ask codex u13")  # Codex's reconstructed cursor (its own last message #10) carries #11-#13
        until(lambda: (self.fake / "prompts.jsonl").exists(), "Codex prompt")
        self.assertEqual([int(n) for n in re.findall(r"^=== #(\d+) ", prompts()[0]["text"], re.M)], [11, 12, 13])
        (self.fake / "finish-w9_n1").write_text("done")
        until(lambda: "turn_end" in [e["kind"] for e in events()], "turn end")
        self.assertEqual([e["session"] for e in events() if e["kind"] == "session_confirmed"], ["sess-codex"])
        self.assertIn("closed Codex pane w9:n1 and Claude Code pane w9:n2", ctl("/exit"))
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual([c[2] for c in self.mutations() if c[:2] == ["pane", "close"]], ["w9:n1", "w9:n2"])

    def test_resume_refuses_open_running_duplicate_unrecorded_or_failed_groups_without_changes(self):
        def live(agents):
            (self.fake / "agents.json").write_text(json.dumps(agents))
            for p in agents:
                (self.fake / f"status-{p.replace(':', '_')}").write_text("idle")

        def hold(state):  # a coordinator owns the group
            self.held = open(state / "coordinator.lock", "a")
            fcntl.flock(self.held, fcntl.LOCK_EX)
        host = lambda p, a, s, tab="w9:t0": {p: {"agent": a, "agent_session": {"value": s}, "terminal_id": f"term-{a}-old",
                                                 "pgid": 1, "workspace_id": "w9", "tab_id": tab}}
        cases = {
            "already open": (hold, "already open", False),
            "still running in its panes": (lambda g: live({**host("w9:a", "codex", "sess-codex"),
                                                           **host("w9:b", "claude", "sess-claude")}),
                                           "still runs in Codex pane w9:a", False),
            "one agent pane left": (lambda g: live(host("w9:b", "claude", "sess-claude")), "agent_not_found", False),
            "session live elsewhere": (lambda g: live(host("w9:z", "claude", "sess-claude", "w9:t5")),
                                       "already runs in pane w9:z (tab w9:t5)", False),
            "tab not empty": (lambda g: (self.fake / "pane-count").write_text("3"), "has 3 panes", False),
            "native resume fails": (lambda g: (self.fake / "resume-missing-codex").write_text(""),
                                    "saved group state unchanged", True),
            "another session came up": (lambda g: (self.fake / "resume-as-claude").write_text("sess-copy"),
                                        "runs session sess-copy, not the recorded sess-claude", True),
        }
        for (name, (setup, message, launched)), legacy in itertools.product(cases.items(), (False, True)):
            with self.subTest(name, legacy=legacy):
                self.tearDown()
                self.setUp()
                state = self.group("g1", legacy=legacy)
                setup(state)
                before = self.files(state)
                r = self.resume("", "--state-dir", str(state))
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(message, r.stderr)
                self.assertEqual(self.files(state), before)
                kinds = sorted(tuple(c[:2]) for c in self.mutations())
                self.assertEqual(kinds, sorted([("pane", "split")] * 2 + [("pane", "swap")] + [("agent", "start")] * 2)
                                 if launched else [])  # no retry, nothing closed
                if launched:
                    self.assertIn("Group chat NOT started. Panes created by this launch: ['w9:n1', 'w9:n2']", r.stderr)
        for legacy in (False, True):
            with self.subTest("Codex id never recorded", legacy=legacy):
                self.tearDown()
                self.setUp()
                state = self.group("g1", codex_session=None, legacy=legacy)
                before = self.files(state)
                r = self.resume("", "--state-dir", str(state))
                self.assertIn("was never recorded", r.stderr)
                self.assertEqual((self.files(state), self.mutations()), (before, []))

    def test_legacy_binding_with_both_panes_still_works(self):
        agents = {p: {"agent": a, "agent_session": {"value": f"sess-{a}"}, "terminal_id": f"term-{a}",
                      "workspace_id": "w9", "tab_id": "w9:t1"} for p, a in (("w9:a", "codex"), ("w9:b", "claude"))}
        (self.fake / "agents.json").write_text(json.dumps(agents))
        for p in agents:
            (self.fake / f"status-{p.replace(':', '_')}").write_text("idle")
        self.layout({"w9:a": (0, 0, 87, 36), "w9:n2": (87, 0, 86, 36), "w9:n1": (0, 36, 173, 16)})
        r = self.start("--codex", "w9:a", "--claude", "w9:b")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.mutations()
        self.assertEqual([c[:3] for c in m], [["pane", "split", "w9:a"], ["pane", "move", "w9:b"],
                                              ["pane", "run", "w9:n1"]])
        self.assertIn("ui --state-dir", m[2][3])
        cfg = json.loads((self.state / "config.json").read_text())
        self.assertEqual((cfg["panes"], cfg["bottom_pane"]), ({"codex": "w9:a", "claude": "w9:n2"}, "w9:n1"))


if __name__ == "__main__":
    unittest.main()
