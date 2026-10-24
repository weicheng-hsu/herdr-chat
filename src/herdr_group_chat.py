#!/usr/bin/env python3
"""External Herdr group chat for one Codex pane, one Claude Code pane, and the user.

`start`, run alone in an empty Herdr tab, starts both agents above a shared bottom
pane (or binds two existing agent panes with --codex/--claude); run without options in a saved
group's shared pane, it recovers that group after Herdr resumed its native sessions (`recover
--state-dir` chooses one explicitly). `resume` picks a saved group from a short history list and
continues it: in its shared pane as recovery, or, once its agent panes are gone, from an empty tab by
natively resuming the same recorded Codex and Claude Code sessions. `ui` is the single
local coordinator, and agents publish only through `claim` and `submit`. The coordinator
alone calls the original Herdr executable; direct Herdr calls and native input boxes
bypass it. New public content (a user message or an agent's message) goes to each addressed
agent: as an update input into its running native turn, or else as a new evaluation turn.
Each agent privately decides whether to speak and may keep researching. The first valid
`claim` by an agent that has received every public message gets the single speaking slot
for exactly one message: its `submit` records the message and releases the slot at once,
so a later message needs a fresh claim. A losing claim is told the holder or reason and has
no reservation (no queue, no promotion). `/ask AGENT` makes only that agent eligible for
that one message. Each input carries only the public messages that agent's bound session
has not yet received or written (progress.json). An agent woken natively after its group turn
ended (e.g. by a delegated task's report) may run `rejoin`: it gets those unseen messages and
fresh commands (not a claim), and that native turn is then tracked like a dispatched one.
"""
import argparse
import fcntl
from collections import deque
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

AGENTS = ("codex", "claude")
LABELS = {"codex": "Codex", "claude": "Claude Code"}
READY = ("idle", "done")
EOF_MARK = "GROUPCHAT_EOF"
# Herdr's own window for an input to show activity (`agent prompt --wait` reports a stall after it).
# Used after a mid-turn update; it is not a bound on Herdr's detection lag.
ACTIVITY_MS = 5000
# Esc interruption: how long a stop may take to be confirmed (capped by turn_timeout_ms), the least time between two
# Esc keys to one agent (never a quick double press), and how many keys it gets at most.
STOP_MS, ESC_GAP, ESC_KEYS = 30000, 2.0, 3
SAFE = ("PAUSED: no native group turn running and both sessions idle when checked, native input boxes may be used "
        "(a delegated task's report can still wake an agent later; /pause again re-checks)")
MAX_PROMPT_BYTES = 120_000  # Herdr takes the prompt as one argv string (Linux limit 128 KiB).
SCRIPT = Path(__file__).resolve()
HELP = """Plain text is a group message (start with // to send a leading slash); both agents
evaluate it and the first valid claim speaks.
/ask codex|claude MESSAGE  only that agent may answer this one (public) message
/interrupt  (Esc in the line editor, once no / menu is open) stop new dispatches and claims and interrupt both
       agents' current native turns at once; nothing is re-asked or resent, /resume continues
/pause  stop new dispatches and claims; running native turns finish first
/resume  check both sessions are ready (or still working) and bound, then continue
/skip  abandon stopped turns whose agent is idle (no resend)
/status   /history [N]   /multi (end with /end); in the line editor Shift+Enter adds a line
PgUp/PgDn  scroll the message view (new messages then wait below, counted); Ctrl+End or sending returns to the latest
/exit  end the chat; closes the two agent panes `start` launched (attached --codex/--claude
       sessions stay running); /exit! also while agents work, interrupting their turns"""


def other(agent):
    return AGENTS[1 - AGENTS.index(agent)]


def now():
    return time.time()


def stamp(t=None):
    return time.strftime("%H:%M:%S", time.localtime(t or now()))


def tint(text, code, enabled):
    return f"\x1b[{code}m{text}\x1b[0m" if enabled else text


def resolve_herdr(explicit=None):
    path = explicit or os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not path:
        sys.exit("cannot resolve the original herdr executable; pass --herdr-bin")
    path = os.path.realpath(path)
    if path == str(SCRIPT):
        sys.exit("--herdr-bin must be the original Herdr executable, not this wrapper")
    return path


class Herdr:
    """Original Herdr CLI; returns (exit code, parsed result, error text)."""

    def __init__(self, binary, socket_path=None):
        self.binary = binary
        self.env = dict(os.environ)
        if socket_path:
            self.env["HERDR_SOCKET_PATH"] = socket_path

    def call(self, *args, timeout=None):
        try:
            r = subprocess.run([self.binary, *args], env=self.env, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, None, "herdr CLI did not return (local subprocess timeout)"
        result = None
        try:
            result = json.loads(r.stdout)["result"] if r.stdout.strip() else None
        except (ValueError, KeyError):
            pass
        err = r.stderr.strip()
        try:
            e = json.loads(err)["error"]
            err = f"{e.get('code')}: {e.get('message')}"
        except (ValueError, KeyError, TypeError):
            pass
        return r.returncode, result, err

    def agent(self, pane):
        code, result, err = self.call("agent", "get", pane, timeout=60)
        if code != 0 or not result:
            return None, f"agent get {pane} failed: {err or code}"
        info = result["agent"]
        code, result, err = self.call("pane", "process-info", "--pane", pane, timeout=60)
        if code != 0 or not result:
            return None, f"pane process-info {pane} failed: {err or code}"
        info["foreground_pgid"] = result["process_info"].get("foreground_process_group_id")
        return info, None


def identity(info):
    return {"agent": info.get("agent"), "session": (info.get("agent_session") or {}).get("value"),
            "terminal_id": info.get("terminal_id"), "workspace_id": info.get("workspace_id"),
            "pgid": info.get("foreground_pgid")}


def read_events(state):
    path = Path(state) / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def observed(info, bound):
    """identity(info), compared against the bound identity. Codex reports its session id to Herdr only at its first
    turn (SessionStart hook), so after `resume` natively resumed the recorded id in a pane it created, that id is
    bound as `pending`: until Herdr reports an id, the same process (terminal and pgid) stands for it. Receipts,
    claims and progress of a pending session are not credited (Chat.unconfirmed)."""
    cur = identity(info)
    if bound.get("pending") and cur["session"] is None and (cur["terminal_id"], cur["pgid"]) == (
            bound["terminal_id"], bound["pgid"]):
        cur.update(session=bound["session"], pending=True)
    return cur


def resumed_problem(agent, bound, info, tab_id):
    """Why `info` (agent get) is not the saved native session `bound` in tab tab_id, else None. Only a recorded
    session id identifies a resumed conversation; Herdr's terminal and process ids change when it restores one."""
    cur = observed(info, bound)
    if not bound["session"]:
        return f"{LABELS[agent]}'s native session id was never recorded, so a resumed session cannot be verified"
    if (cur["agent"], cur["session"], cur["workspace_id"], info.get("tab_id")) != (
            agent, bound["session"], bound["workspace_id"], tab_id):
        return (f"{LABELS[agent]} pane {info.get('pane_id')} hosts {dict(cur, tab_id=info.get('tab_id'))}, "
                f"not the saved session {bound['session']} in tab {tab_id}")
    return None


def save_config(state, cfg):
    tmp = Path(state) / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, Path(state) / "config.json")


def take_lock(state):
    """The group's coordinator.lock, held until this process exits; None if another process holds it."""
    lock = open(Path(state) / "coordinator.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def request(sock_path, payload, timeout=30):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(json.dumps(payload).encode())
        s.shutdown(socket.SHUT_WR)
        data = b"".join(iter(lambda: s.recv(65536), b""))
    return json.loads(data)


class Chat:
    def __init__(self, state_dir, out, color=False):
        self.dir = Path(state_dir)
        self.cfg = json.loads((self.dir / "config.json").read_text())
        self.herdr = Herdr(self.cfg["herdr_bin"], self.cfg.get("herdr_socket"))
        self.panes = self.cfg["panes"]
        self.out = out
        self.color = color
        self.render = None  # display-only text -> lines (markdown_renderer); None shows the raw lines
        self.cv = threading.Condition()
        self.queue = deque()  # (text, ask, qid): ask is None (both agents) or the one agent asked
        self.scope = set()     # agents the latest user message addressed (both, or the /ask one)
        self.due = set()       # agents owed new public content (or a re-ask) in their next input
        # agent -> its one tracked native task: the native turn(s) our inputs cause in that
        # session, from the first prompt until Herdr shows it settled after the last input.
        self.tasks = {}
        # Input token -> (task, through): every prompt or update has its own token, so a claim
        # proves which public messages (through seq) reached the model.
        self.inputs = {}
        self.ninputs = 0
        self.owner = None      # the task holding the speaking slot: from a valid claim to its one submit
        self.paused = False
        self.hold = None    # visible stop reason; only /resume or /skip clears it
        self.halting = set()  # agents an Esc interrupt is still stopping (halt); /resume waits for none
        self.halted = {}      # agent -> (problem or None, Esc keys sent) of the current interrupt
        self.quit = False
        path = self.dir / "transcript.jsonl"
        self.transcript = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        # Per agent: the bound session and the last public seq it is known to have received
        # or written. Every new task's prompt carries all of that session's unseen messages.
        # A claim is granted only when this reaches the newest message, and while a grant is
        # held only its owner appends, so submission #k still proves receipt of everything
        # before k. State from before this file existed (full-history prompts) is
        # reconstructed from that rule.
        path = self.dir / "progress.json"
        self.progress = json.loads(path.read_text()) if path.exists() else {}
        for agent in AGENTS:
            if agent not in self.progress:
                own = [m["seq"] for m in self.transcript if m["author"] == agent]
                self.progress[agent] = {"session": None, "seen": max(own, default=0)}
                self.event("progress_reconstructed", agent=agent, seen=self.progress[agent]["seen"])
        # config.json keeps Codex's session null (it reports its id only after its first turn);
        # restore the id learned earlier so check() refuses a different session after a reload.
        events = read_events(self.dir)
        # Every input token issued to each agent: identifies that agent's session to `rejoin` after its turn ended
        # (an ended token still cannot claim, ack or submit).
        self.issued = {e["token"]: e["agent"] for e in events
                       if e["kind"] in ("dispatch", "update", "rejoin") and e.get("agent")}
        learned = {e["agent"]: e["session"] for e in events if e["kind"] == "session_learned"}
        for agent in AGENTS:
            bound, rec = self.cfg["identity"][agent], self.progress[agent]
            if bound["session"] is None:
                bound["session"] = rec["session"] or learned.get(agent)
                if bound["session"]:
                    self.event("session_restored", agent=agent, session=bound["session"])
                elif rec["seen"]:  # cursor of an unknown session: resend everything, lose nothing
                    self.event("progress_unbound_reset", agent=agent, seen=rec["seen"])
                    rec["seen"] = 0
        # After a restart (crash, reboot): accepted user messages not yet published are queued again (a published
        # one carries its qid in the transcript). Earlier versions kept the queue only in memory: such losses in
        # the last run are counted, not recovered.
        published = {m.get("qid") for m in self.transcript}
        self.queue.extend((e["text"], e.get("ask"), e["qid"]) for e in events
                          if e["kind"] == "user_queued" and "qid" in e and e["qid"] not in published)
        run = events[max((i for i, e in enumerate(events) if e["kind"] == "ui_start"), default=-1) + 1:]
        legacy = sum(e["kind"] == "user_queued" and "qid" not in e for e in run)
        self.lost = max(0, legacy - sum(e["kind"] == "user_message" for e in run)) if legacy else 0
        # A turn an earlier coordinator sent but never saw end or skip stays a stopped, unconfirmed task here (held
        # by cmd_ui): never resent, never credited, and its old commands are refused (no inputs are registered).
        # All runs count, so a turn still held at another restart is held again; a later dispatch to the same agent
        # supersedes it. Older protocols (round/generation records) ended turns differently and are not tracked.
        by_token = {}
        for e in events:
            t = self.tasks.get(e.get("agent"))
            if e["kind"] in ("dispatch", "rejoin") and not {"round", "gen"} & e.keys():
                self.tasks[e["agent"]] = by_token[e["token"]] = t = self.new_task(e["agent"], e["token"])
                t["sent"], t["delivered"] = True, e["seqs"][1]
                if e["kind"] == "rejoin":
                    t["rejoin"] = (e["seqs"][0] - 1, e["seqs"][1])
            elif e["kind"] == "update" and t and t["token"] == e["turn"]:
                by_token[e["token"]] = t
                t["delivered"], t["last"] = e["seqs"][1], e["token"]
            elif e["kind"] == "submit_received" and e["token"] in by_token:
                by_token[e["token"]]["submissions"].append(e["seq"])
            elif e["kind"] in ("turn_end", "skip") and t and t["token"] == e["token"]:
                del self.tasks[e["agent"]]
        # Owed evaluations, for current transcripts only (the latest user message has a qid): dispatch() and update()
        # record their event before calling Herdr, so an addressee of that message whose cursor, own posts, input
        # events and explicit /skip all stop short of the newest message was never sent it. It is due again (after
        # any hold, before the restored queue), with that message's /ask scope; uncertain sends stay held above.
        last = next((m for m in reversed(self.transcript) if m["author"] == "user"), None)
        if last and "qid" in last:
            self.scope = {last["ask"]} if last.get("ask") else set(AGENTS)
            covered = {a: max([self.progress[a]["seen"]] + [m["seq"] for m in self.transcript if m["author"] == a])
                       for a in AGENTS}
            for e in events:
                if e["kind"] in ("dispatch", "update", "rejoin") and "seqs" in e:
                    covered[e["agent"]] = max(covered[e["agent"]], e["seqs"][1])
                elif e["kind"] == "skip" and e.get("due") is False:  # what /skip dropped stays dropped
                    covered[e["agent"]] = max(covered[e["agent"]], e["through"])
                elif e["kind"] == "interrupt":  # Esc cancelled every owed evaluation; nothing is re-asked
                    covered = {a: max(c, e["through"]) for a, c in covered.items()}
            self.due = {a for a in self.scope if covered[a] < len(self.transcript)}

    # records -------------------------------------------------------------
    def event(self, kind, **data):
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"time": now(), "kind": kind, **data}, ensure_ascii=False) + "\n")

    def append(self, author, text, **extra):
        msg = {"seq": len(self.transcript) + 1, "time": now(), "author": author, "text": text, **extra}
        self.transcript.append(msg)
        with (self.dir / "transcript.jsonl").open("a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return msg

    def show(self, msg):
        name = "You" if msg["author"] == "user" else LABELS.get(msg["author"], msg["author"])
        header = (tint(f"── {name}", "1", self.color)
                  + tint(f"  #{msg['seq']} · {stamp(msg['time'])} ──", "2", self.color))
        rail = tint("│ ", "2", self.color)
        lines = msg["text"].split("\n")
        if self.render:  # the transcript and agent inputs keep the raw text
            try:
                lines = self.render(msg["text"], shutil.get_terminal_size().columns - 2)
            except Exception:  # never lose a message to the display: show it raw
                pass
        return "\n" + header + "\n" + "\n".join(rail + line for line in lines)

    def seen(self, agent):
        rec, bound = self.progress[agent], self.cfg["identity"][agent]["session"]
        if rec["session"] and bound and rec["session"] != bound:  # never carry progress to another session
            self.event("progress_session_mismatch", agent=agent, recorded=rec["session"], bound=bound)
            self.progress[agent] = rec = {"session": bound, "seen": 0}
        return rec["seen"]

    def advance(self, agent, seq):
        """Record that the bound session has received/written public messages through seq."""
        with self.cv:
            if seq <= self.seen(agent):
                return
            self.progress[agent] = {"session": self.cfg["identity"][agent]["session"], "seen": seq}
            self.save_progress()
            self.event("progress", agent=agent, seen=seq)

    def rebind(self):
        """Recovery only (Herdr resumed the saved native sessions, e.g. after a reboot): adopt the panes' new
        terminal and process ids for both agents, or for neither, and only where each pane hosts the same
        positively known session. Runtime checks stay strict against the result."""
        cur = {}
        for agent in AGENTS:
            info, err = self.herdr.agent(self.panes[agent])
            problem = err or resumed_problem(agent, self.cfg["identity"][agent], info, self.cfg.get("tab_id"))
            if problem:
                return problem
            cur[agent] = observed(info, self.cfg["identity"][agent])
        old = self.cfg["identity"]
        if cur != old:
            self.cfg["identity"] = cur
            self.save_config()
            self.event("rebind", old=old, new=cur)
        return None

    def save_config(self):
        save_config(self.dir, self.cfg)

    def save_progress(self):
        tmp = self.dir / "progress.json.tmp"
        tmp.write_text(json.dumps(self.progress, indent=2))
        os.replace(tmp, self.dir / "progress.json")

    def set_hold(self, reason):
        with self.cv:
            self.hold = reason
            self.event("hold", reason=reason, owner=self.owner and self.owner["token"])
            if self.quit:  # the chat has ended; nothing is left to resume
                return
        self.out(f"STOPPED: {reason}\n  Queue held; nothing resent. Fix natively if needed, then /resume (or /skip).", kind="error")

    # participant checks --------------------------------------------------
    def observe(self, agent):
        """(agent get info, None) while the pane hosts the bound session, else (info or None, the problem)."""
        info, err = self.herdr.agent(self.panes[agent])
        if err:
            return None, err
        bound = self.cfg["identity"][agent]
        cur = observed(info, bound)
        if bound.get("pending") and cur == {k: v for k, v in bound.items() if k != "pending"}:
            with self.cv:  # Herdr now reports the natively resumed id itself: confirmed, durably
                bound.pop("pending", None)
                self.save_config()
                self.event("session_confirmed", agent=agent, session=cur["session"])
        if bound["session"] is None and cur["session"]:
            # Codex reports its session id only after its first turn; adopt it once.
            with self.cv:  # durable at once: a reload restores it even if seen never advances again
                bound["session"] = cur["session"]
                self.event("session_learned", agent=agent, session=cur["session"])
                if self.progress[agent]["session"] is None:
                    self.progress[agent]["session"] = cur["session"]
                    self.save_progress()
        if cur != bound:
            return info, f"{LABELS[agent]} pane {self.panes[agent]} no longer hosts the bound session ({bound} -> {cur})"
        return info, None

    def check(self, agent, need_ready=True):
        info, problem = self.observe(agent)
        if problem:
            return problem
        if need_ready and info.get("agent_status") not in READY:
            return f"{LABELS[agent]} is {info.get('agent_status')}, not idle/done"
        return None

    def check_both(self):
        return "; ".join(p for p in (self.check(a) for a in AGENTS) if p)

    def unconfirmed(self, agent):
        """None once Herdr reports the agent's bound session id; else why its receipts cannot be credited yet."""
        if not self.cfg["identity"][agent].get("pending"):
            return None
        problem = self.check(agent, need_ready=False)
        if problem or not self.cfg["identity"][agent].get("pending"):
            return problem
        return (f"Herdr has not yet reported {LABELS[agent]}'s session id, so the resumed session "
                f"{self.cfg['identity'][agent]['session']} is not verified")

    def unverified(self, token):
        """For a claim or receipt: unconfirmed() of the input's agent (outside cv; it may call Herdr)."""
        with self.cv:
            t, _ = self.find(token)
        return t and self.unconfirmed(t["agent"])

    # dispatcher ----------------------------------------------------------
    def native_unsafe(self):
        """Outside cv: {agent: reason} for agents whose native input is not safe now: native_busy() ones, plus any
        other agent (no task, or one held before sending) whose bound session Herdr does not show idle/done at this
        one observation (e.g. woken natively by a delegated task's report before rejoin). A snapshot: a later
        external wake-up can still start a native turn."""
        with self.cv:
            unobserved = [a for a in AGENTS if a not in self.native_busy()]
        unsafe = {a: f"{p} (no running group turn, e.g. woken natively)" for a in unobserved if (p := self.check(a))}
        with self.cv:  # rechecked after the observation: a group turn may have started meanwhile
            return {**{a: f"{LABELS[a]} may still be working" for a in self.native_busy()}, **unsafe}

    def native_busy(self):
        """Under cv: agents whose native group turn may be running or about to be sent. A stopped
        wrapper thread is not proof: a sent turn counts until Herdr confirmed its completion."""
        return [a for a, t in self.tasks.items() if t["running"] or (t["sent"] and not t["ended"])]

    def live(self):
        """Under cv: whether an agent may still be working on, or about to receive, public content."""
        return self.owner or self.due or self.tasks

    def publish_user(self):
        """Under cv, slot free: make the oldest queued user message public; its addressees are due."""
        text, ask, qid = self.queue.popleft()
        msg = self.append("user", text, qid=qid, **({"ask": ask} if ask else {}))
        self.out(self.show(msg), kind="message")
        self.scope = {ask} if ask else set(AGENTS)
        self.due |= self.scope
        self.event("user_message", seq=msg["seq"], due=sorted(self.due))

    def new_token(self, agent):
        self.ninputs += 1
        return f"t{self.ninputs}-{agent}-{secrets.token_hex(4)}"

    @staticmethod
    def new_task(agent, token):
        return {"token": token, "agent": agent, "sent": False, "running": 0, "ended": False, "delivered": 0,
                "writing": False, "writes": 0, "waited": 0, "cold": False, "unconfirmed": False, "reask": False,
                "last": None, "acked": None, "submissions": [], "interrupted": False,
                "rejoin": None}  # (start, through) of a rejoin reply: returned only as command output

    def work(self):
        """Under cv: (task, kind) jobs to start now: "prompt" opens a native task, "update" writes new
        content into a running one, "resume" handles a stopped one after /resume. New content is
        delivered only while the slot is free (a grant ends with its one message)."""
        if self.paused or self.hold:
            return []
        if self.queue and not self.live():
            self.publish_user()
        jobs = [(t, "resume") for t in self.tasks.values() if not t["running"]]
        for agent in [a for a in AGENTS if a in self.due and self.owner is None]:
            t = self.tasks.get(agent)
            if t is None:
                self.due.discard(agent)
                self.tasks[agent] = t = self.new_task(agent, self.new_token(agent))
                jobs.append((t, "prompt"))
            elif not (t["sent"] and t["running"]) or t["ended"] or t["writing"]:
                continue  # not sent yet, stopped, ending, or writing: decided again when that changes
            elif t["delivered"] < len(self.transcript) or t["reask"]:
                t["writing"] = True  # one update write at a time keeps a session's inputs in order
                jobs.append((t, "update"))
            else:
                self.due.discard(agent)  # its latest input already carried everything
        return jobs

    def dispatcher(self):
        while True:
            with self.cv:
                while not self.quit and not (work := self.work()):
                    self.cv.wait()
                if self.quit:
                    return
                for t, _ in work:
                    t["running"] += 1
            for t, kind in work:
                threading.Thread(target=self.run_task, args=(t, kind), daemon=True).start()

    def run_task(self, t, kind):
        try:  # `sent` decides reconcile vs. dispatch on /resume; never resend
            if kind == "update":
                self.update(t)
            elif kind == "resume" and t["sent"]:
                self.reconcile(t)
            else:
                self.dispatch(t)
        except Exception as e:
            self.set_hold(f"coordinator error during {LABELS[t['agent']]} turn {t['token']}: {e!r}")
        with self.cv:
            t["running"] -= 1
            if kind == "update":
                t["writing"] = False
            self.cv.notify_all()
            quiet = self.paused and not self.native_busy()
        if quiet and not self.native_unsafe():
            with self.cv:
                quiet = self.paused
            if quiet:
                self.out(SAFE)

    def commands(self, agent, token):
        base = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))}"
        # Bound pane in the command: Codex tool shells may carry another pane's HERDR_PANE_ID.
        sock = f"--socket {shlex.quote(self.cfg['socket'])} --turn {token} --pane {shlex.quote(self.panes[agent])}"
        return (f"{base} claim {sock}", f"{base} submit {sock} <<'{EOF_MARK}'\n<your public message>\n{EOF_MARK}",
                f"{base} ack {sock}", f"{base} rejoin {sock}")

    def prompt(self, agent, token, start, through, update=False, rejoin=False):
        claim, submit, ack, back = self.commands(agent, token)
        new = self.transcript[start:through]
        history = "\n".join(f"=== #{m['seq']} {LABELS.get(m['author'], m['author'])}"
                            + (f" (asked {LABELS[m['ask']]} only)" if m.get("ask") else "")
                            + f" ===\n{m['text']}" for m in new)
        if rejoin:  # the reply of the agent's own `rejoin` command, not a native prompt
            intro = (f"public messages you have not received (#{start + 1}-#{through}, {len(new)} messages)" if new
                     else f"no public messages you have not received (everything through #{through} reached you)")
            return f"""[Herdr group chat] Rejoin {token} ({LABELS[agent]}): {intro}. This is not a claim; this native turn is now your group-chat turn, and later public messages reach you in it as Update inputs.
The protocol of your turn prompt still applies. Read this whole reply first (if your tool shows only part of it, read the full saved output; if it is missing, run the same rejoin command once more: it returns this same reply until a receipt is recorded), then run this receipt once (not a claim; silence is still fine):
{ack}
From now on use these commands (a claim with an older one is refused while newer messages exist):
{claim}
{submit}
{history}
=== end of rejoin ==="""
        if update:
            intro = (f"new public messages since your last input (#{start + 1}-#{through}, {len(new)} messages)" if new
                     else f"no new public messages (everything through #{through} was already delivered to you); you "
                          "are asked again because your claim was refused while the chat was paused or stopped")
            return f"""[Herdr group chat] Update {token} to your current group-chat turn ({LABELS[agent]}): {intro}.
The protocol of your turn prompt still applies. On reading this update, first run this receipt once (not a claim; silence is still fine):
{ack}
From now on use these commands (a claim with an older one is refused while newer messages exist):
{claim}
{submit}
If your turn had already ended, this input starts a new one: decide again (silence is fine).
{history}
=== end of update ==="""
        intro = (f"Public group transcript so far (oldest first, {len(new)} messages)" if start == 0 else
                 f"New public group messages since your last turn (#{start + 1}-#{through}, "
                 f"{len(new)} messages; earlier ones, including your own, were already delivered to you)"
                 if new else f"No new public messages (everything through #{through} was already "
                             "delivered to you); you are asked again because your claim was refused while the chat "
                             "was paused or stopped. Decide again")
        return f"""[Herdr group chat] Turn {token}: you are {LABELS[agent]}, in a group chat with the user and {LABELS[other(agent)]}.
Group-chat protocol (external wrapper):
- Decide privately whether to say something publicly. You may research privately in this turn, before or after posting (for example read files or run a small bounded check). Silence is fine: then just end your turn, with no command or message.
- Each public message needs its own claim of the single public speaking slot, made when that message is ready (returns at once):
{claim}
- GRANTED: post exactly one message by running this command, with your message between the markers:
{submit}
  The receipt records it and releases the slot at once; it is not an instruction to stop. To post another message later, claim again.
- DENIED: you have NO reservation and will not be called back for this denial; do not post and do not retry in a loop. If it says newer messages were posted, they reach you as a separate input in this turn: read them, then decide again with the commands they carry. Otherwise continue privately or end your turn.
- Public messages posted while you work reach you as "[Herdr group chat] Update" inputs during this turn, each with the commands to use from then on.
- Do not wait for, prompt, or notify {LABELS[other(agent)]}, and do not send group messages with herdr directly.
- Do not keep a turn open to wait for delegated work. If this session is woken after your group turn ended (e.g. by a delegated task's report) and you may want to speak publicly, run this once, early (not a claim; it returns the public messages you have not received and fresh commands):
{back}
- Text you write only in this pane is private. If your message contains a line exactly {EOF_MARK}, write it to a file and use --file PATH instead of the heredoc.
{intro}:
{history}
=== end of transcript ==="""

    def dispatch(self, t):
        agent = t["agent"]
        problem = self.check(agent)  # this session only; the other may be evaluating concurrently
        if problem:
            return self.set_hold(f"{LABELS[agent]} turn not dispatched: {problem}")
        with self.cv:  # the content actually sent: a claim with this token acknowledges it
            start, through = self.seen(agent), len(self.transcript)
        text = self.prompt(agent, t["token"], start, through)
        if len(text.encode()) > MAX_PROMPT_BYTES:
            return self.set_hold(f"prompt is {len(text.encode())} bytes, above the {MAX_PROMPT_BYTES}-byte argv limit; not dispatched")
        with self.cv:
            if self.quit:  # /exit stopped dispatch: never send after it
                return
            if t["interrupted"]:  # Esc came first: never sent; its messages stay unseen (sent with the next input)
                del self.tasks[agent]
                self.event("dispatch_cancelled", token=t["token"], agent=agent)
                self.cv.notify_all()
                return
            self.inputs[t["token"]] = (t, through)
            self.issued[t["token"]] = agent
            t["sent"], t["delivered"], t["reask"] = True, through, False
            self.event("dispatch", token=t["token"], agent=agent, prompt_bytes=len(text.encode()),
                       seqs=[start + 1, through])
            self.cv.notify_all()  # content that arrived meanwhile can now go in as an update
        self.out(f"[{stamp()}] → {LABELS[agent]} evaluates (turn {t['token']})", kind="routine")
        code, result, err = self.herdr.call("agent", "prompt", self.panes[agent], text,
                                            "--wait", "--timeout", str(self.cfg["turn_timeout_ms"]))
        self.settle(t, code, result, err, through)

    def update(self, t):
        """Write new public content (or a re-ask) into the agent's running task as its own input."""
        agent = t["agent"]
        problem = self.check(agent, need_ready=False)  # identity only: the session is expected to be working
        if problem:  # nothing written; the agent stays due
            return self.set_hold(f"{LABELS[agent]} update not sent: {problem}")
        with self.cv:
            if self.quit or t["interrupted"]:  # nothing is written after Esc: it would start a new native turn
                return
            start, through, token = t["delivered"], len(self.transcript), self.new_token(agent)
            if t["rejoin"] and self.seen(agent) < t["rejoin"][1]:  # its rejoin reply has no receipt: carried again
                start = self.seen(agent)  # as input, so no later receipt or settled batch credits it unwritten
            text = self.prompt(agent, token, start, through, update=True)
            if len(text.encode()) > MAX_PROMPT_BYTES:
                problem = f"update is {len(text.encode())} bytes, above the {MAX_PROMPT_BYTES}-byte argv limit; not sent"
            else:
                self.due.discard(agent)
                self.inputs[token] = (t, through)
                self.issued[token] = agent
                t["delivered"], t["reask"], t["last"] = through, False, token
                self.event("update", token=token, turn=t["token"], agent=agent, prompt_bytes=len(text.encode()),
                           seqs=[start + 1, through])
        if problem:
            return self.set_hold(problem)
        self.out(f"[{stamp()}] → {LABELS[agent]} gets an update in its running turn ({token})", kind="routine")
        # Transport only: Herdr wrote the input. The model sees it in this turn (Codex steers it in,
        # Claude Code after its current tool call) or, if that turn just ended, in a new turn.
        code, result, err = self.herdr.call("agent", "prompt", self.panes[agent], text, timeout=60)
        status = (result or {}).get("agent", {}).get("agent_status") if code == 0 else None
        self.event("update_written", token=token, exit_code=code, status=status, error=err or None)
        with self.cv:
            if code == 0:
                t["writes"] += 1
                t["cold"] = t["cold"] or status != "working"  # landed on a settled session: starts a new turn
            else:  # held in the same critical section, so the stopped task is not reconciled before /resume
                t["unconfirmed"] = True
                self.set_hold(f"{LABELS[agent]} update {token} not confirmed ({err or code}); it may have reached "
                              "the session. Nothing is resent; the turn stays until /resume or /skip")

    def reconcile(self, t):
        with self.cv:  # resumed after the stop: observe the session afresh, nothing is resent
            t["unconfirmed"], t["cold"], t["waited"] = False, False, t["writes"]
        pane = self.panes[t["agent"]]
        info, err = self.herdr.agent(pane)
        if err:
            return self.set_hold(f"{LABELS[t['agent']]} turn {t['token']}: {err}")
        if info.get("agent_status") != "working":
            return self.settle(t, 0, {"agent": info}, "")
        self.event("reconcile_wait", token=t["token"])
        self.out(f"[{stamp()}] waiting for {LABELS[t['agent']]} to finish turn {t['token']}", kind="routine")
        code, result, err = self.herdr.call("agent", "wait", pane, "--timeout", str(self.cfg["turn_timeout_ms"]))
        self.settle(t, code, result, err)

    def settle(self, t, code, result, err, through=None):
        """Follow a Herdr wait of task t until the session settled after its last input, then end t.
        Only this waiting thread ends the task; update writers just count their writes."""
        agent, name, pane = t["agent"], LABELS[t["agent"]], self.panes[t["agent"]]
        timeout = str(self.cfg["turn_timeout_ms"])
        while True:
            status = (result or {}).get("agent", {}).get("agent_status") if code == 0 else None
            self.event("native_return", token=t["token"], exit_code=code, status=status, error=err or None)
            if code != 0:
                return self.set_hold(f"{name} turn {t['token']} completion not confirmed ({err or code}); "
                                     "timeout is not cancellation, the turn stays with " + name)
            if status not in READY:
                return self.set_hold(f"{name} is {status} during turn {t['token']}")
            if through and (problem := self.unconfirmed(agent)):  # never credit an unverified session
                return self.set_hold(f"{name} turn {t['token']}: {problem} after the turn; nothing is credited")
            if through and not t["interrupted"]:  # Herdr saw the session settle after every input through this seq
                self.advance(agent, through)  # (an interrupted turn may have stopped before reading it: not credited)
            with self.cv:
                while t["writing"]:  # an update being written may start more native work
                    self.cv.wait()
                if t["unconfirmed"]:  # a failed update write stopped the chat; /resume re-observes
                    return
                if t["writes"] == t["waited"]:
                    t["ended"] = True  # Herdr confirmed the session settled after our last input
                    break
                t["waited"], through, cold, t["cold"] = t["writes"], t["delivered"], t["cold"], False
                # Receipt (ack or claim) of this batch's newest input, read now: after the settled state above was
                # returned and before the probe below starts. It need not precede that settled state.
                acked = t["acked"] == t["last"]
            # Herdr may still show the settled state although an update started a new native turn: always after
            # a cold write; after one written while `working`, if it landed as that turn ended. Wait to see it.
            code, result, err = self.herdr.call("agent", "wait", pane, "--until", "working", "--until", "blocked",
                                                "--timeout", timeout if cold else str(ACTIVITY_MS))
            if not cold and code == 1 and err.startswith("timeout:"):
                if not acked:  # no receipt and no turn shown, but a later one is not excluded: the task stays
                    # tracked and busy and its cursor is not advanced. Any other failure is held above the same way.
                    self.event("update_unresolved", token=t["token"], through=through, window_ms=ACTIVITY_MS)
                    return self.set_hold(
                        f"{name} turn {t['token']}: update #{through} was written while it worked and has no receipt, "
                        f"and Herdr showed no new native turn within {ACTIVITY_MS // 1000} s; one may still start. "
                        f"Nothing is resent. Check {name}'s native pane: /resume once it is idle with no queued input "
                        "(re-observes it), or /skip")
                # Receipted newest input, then no running turn shown for the whole window: settled, as far as Herdr
                # exposes running work. Ends above with this state, unless a later write started another batch.
                self.event("update_settled", token=t["token"], through=through, window_ms=ACTIVITY_MS)
                code, result, err = self.herdr.call("agent", "get", pane, timeout=60)
            if code != 0 or (result or {}).get("agent", {}).get("agent_status") != "working":
                continue  # failed or blocked: held above
            with self.cv:  # a cold write counted since the snapshot may postdate the `working` just seen: not folded
                t["cold"] = False  # into it; its batch takes the receipt + activity rule after this turn ends
            code, result, err = self.herdr.call("agent", "wait", pane, "--timeout", timeout)
        problem = self.check(agent, need_ready=False)
        if problem:
            return self.set_hold(problem)
        with self.cv:
            if self.tasks.get(agent) is not t:  # never let a superseded callback touch newer state
                self.event("late_return_ignored", token=t["token"])
                return
            missing = self.owner is t  # a granted slot without its message is never released by completion
            if not missing:
                del self.tasks[agent]
                self.inputs = {k: v for k, v in self.inputs.items() if v[0] is not t}
                self.event("turn_end", token=t["token"], agent=agent, submissions=t["submissions"],
                           **({"interrupted": True} if t["interrupted"] else {}))
                self.cv.notify_all()
            settled = not (self.live() or self.queue)
        if missing:
            return self.set_hold(f"{name} claimed the slot but finished turn {t['token']} without submitting; "
                                 "it may still submit with the same turn (e.g. asked natively while stopped)")
        tail = "; all done, waiting for the next user message" if settled and not self.paused else ""
        did = f"posted #{', #'.join(map(str, t['submissions']))}" if t["submissions"] else "ended without speaking"
        did += "; interrupted by Esc, its input not credited" if t["interrupted"] else ""
        self.out(f"[{stamp()}] ✓ {name} turn {t['token']} ended: {did}{tail}", kind="routine")

    # socket --------------------------------------------------------------
    def serve(self, server):
        while True:
            conn, _ = server.accept()
            threading.Thread(target=self.handle_conn, args=(conn,), daemon=True).start()

    def handle_conn(self, conn):
        with conn:
            try:
                req = json.loads(b"".join(iter(lambda: conn.recv(65536), b"")))
                if req.get("op") == "submit":
                    resp = self.submit(req.get("turn"), req.get("body"), req.get("pane"))
                elif req.get("op") == "claim":
                    resp = self.claim(req.get("turn"), req.get("pane"))
                elif req.get("op") == "ack":
                    resp = self.ack(req.get("turn"), req.get("pane"))
                elif req.get("op") == "rejoin":
                    resp = self.rejoin(req.get("turn"), req.get("pane"))
                elif req.get("op") == "status":
                    resp = {"ok": True, "status": self.status()}
                elif req.get("op") == "command":
                    self.out(f"[ctl] {req.get('line', '')[:80]}")
                    resp = {"ok": True, "message": self.command(req.get("line", ""))}
                else:
                    resp = {"ok": False, "message": "unknown op"}
            except Exception as e:  # report to the short-lived client, keep serving
                resp = {"ok": False, "message": f"error: {e}"}
            conn.sendall(json.dumps(resp, ensure_ascii=False).encode())

    def find(self, token):
        """Under cv: (task, through) for an input token of a current task, else (None, 0)."""
        t, through = self.inputs.get(token, (None, 0))
        return (t, through) if t is not None and self.tasks.get(t["agent"]) is t else (None, 0)

    def receipt(self, t, token, through):
        """Under cv: the token proves this input reached the model; only the newest input's receipt counts
        for ending the task (settle)."""
        self.advance(t["agent"], through)
        if token == t["last"]:
            t["acked"] = token

    def ack(self, token, pane):
        """Receipt of an update only: no grant, owner, due, re-ask, queue or public message."""
        if problem := self.unverified(token):
            return {"ok": False, "message": f"not recorded: {problem}"}
        with self.cv:
            t, through = self.find(token)
            if t is None:
                return {"ok": False, "message": f"not recorded: turn {token} is not a current input"}
            if pane and pane != self.panes[t["agent"]]:
                return {"ok": False, "message": f"not recorded: caller pane {pane} is not {LABELS[t['agent']]}'s pane"}
            self.receipt(t, token, through)
            self.event("ack", token=token, agent=t["agent"], through=through)
        return {"ok": True, "message": "receipt recorded (not a claim; silence is fine)."}

    def claim(self, token, pane):
        """Check and grant in one critical section; the reply is the claimant's private answer."""
        if problem := self.unverified(token):
            self.event("claim_denied", token=token, reason=problem, holder=None)
            return {"ok": False, "message": f"DENIED: {problem}. You have NO reservation and will not be called back "
                                            "for this denial: do not post, and do not retry in a loop."}
        with self.cv:
            t, through = self.find(token)
            if t and pane and pane != self.panes[t["agent"]]:
                self.event("claim_rejected", token=token, reason=f"caller pane {pane}")
                return {"ok": False, "message": f"DENIED: caller pane {pane} is not {LABELS[t['agent']]}'s pane"}
            if t is not None and self.owner is t:  # retry of a granted claim: same grant, no new one
                return {"ok": True, "message": "GRANTED (already yours): post your one message with submit."}
            if t is not None:
                self.receipt(t, token, through)
            holder = self.owner and LABELS[self.owner["agent"]]
            if t is None:
                reason = f"turn {token} is not a current input" + (
                    "; if your group turn has ended, run the rejoin command of your turn prompt first"
                    if token in self.issued else "")
            elif t["interrupted"]:  # no re-ask: Esc ends this evaluation
                reason = "the user interrupted this turn (Esc); stop here and end your turn"
            elif holder:
                reason = f"{holder} holds the speaking slot"
            elif self.paused or self.hold:
                reason = "the group chat is paused or stopped; you will be asked again after /resume"
                self.due.add(t["agent"])
                t["reask"] = True
            elif self.seen(t["agent"]) < len(self.transcript):  # never grant over an unseen message
                reason = (f"newer public messages (#{self.seen(t['agent']) + 1}-#{len(self.transcript)}) were posted "
                          "after the input whose command you used; they reach you as a separate input in this turn "
                          "(after your current command). Read them, then claim with the command shown there if you "
                          "still want to speak")
            else:
                self.owner = t
                self.event("claim_granted", token=token, agent=t["agent"], turn=t["token"], through=through)
                reason = None
            if reason:
                self.event("claim_denied", token=token, reason=reason, holder=self.owner and self.owner["token"])
        if reason:
            if t:
                self.out(f"[{stamp()}] {LABELS[t['agent']]} claim denied: {reason}", kind="routine")
            return {"ok": False, "message": f"DENIED: {reason}. You have NO reservation and will not be called back "
                                            "for this denial: do not post, and do not retry in a loop. You may "
                                            "continue privately or end your turn."}
        self.out(f"[{stamp()}] {LABELS[t['agent']]} holds the speaking slot ({token})", kind="routine")
        return {"ok": True, "message": "GRANTED: the single public speaking slot is yours for ONE message. Post it "
                                       "now with submit; the receipt releases the slot."}

    def rejoin(self, token, pane):
        """An agent's bound session, woken natively after its group turn ended (e.g. by a delegated task's report),
        asks to take part again, with any token it was issued. Not a claim and not a resume: refused while the chat
        is paused or stopped, under the other agent's /ask, or while it has a current task. The reply carries only the
        public messages it has not received and commands with a new input token; its running native turn becomes its
        tracked task (updates, /pause, Esc, /exit and completion as for a dispatched turn). The reply is command
        output, not a native input: its range is credited by a receipt (ack or claim) with its token, which is a
        protocol acknowledgment, not proof the whole reply was shown or read. Until then a repeated rejoin returns the
        same reply (no new task or delivery), and a later update carries that range again as native input, so only
        that update's receipt or settled batch credits it."""
        with self.cv:
            agent = self.issued.get(token)

        def refused(reason):
            self.event("rejoin_refused", token=token, agent=agent, reason=reason)
            if agent:
                self.out(f"[{stamp()}] {LABELS[agent]} asked to rejoin; refused: {reason}")
            return {"ok": False, "message": f"not rejoined: {reason}. You have NO reservation and will not be called "
                                            "back: do not retry in a loop. Continue privately or end your turn."}
        if agent is None:
            return refused(f"turn {token} was not issued in this group chat")
        name = LABELS[agent]
        if pane != self.panes[agent]:
            return refused(f"caller pane {pane} is not {name}'s pane")
        problem = self.unconfirmed(agent)
        if not problem:  # the bound session itself, running the native turn this command comes from
            info, problem = self.observe(agent)
            if not problem and info.get("agent_status") != "working":
                problem = f"Herdr shows {name}'s session {info.get('agent_status')}, not working in a native turn"
        if problem:
            return refused(problem)
        repeat = True  # unless a new task is registered below
        with self.cv:
            t = self.tasks.get(agent)
            if self.quit:
                problem = "the group chat has ended"
            elif self.paused or self.hold:
                problem = "the group chat is paused or stopped, and rejoining does not resume it"
            elif self.scope and agent not in self.scope:
                problem = f"the latest user message asked only {LABELS[other(agent)]}; wait for the next user message"
            elif t is not None and t["rejoin"] and t["last"] is None and self.seen(agent) < t["rejoin"][1]:
                (start, through), new = t["rejoin"], t["token"]  # its reply may never have arrived: the same again
                self.event("rejoin_repeated", token=new, agent=agent, via=token)
            elif t is not None:
                problem = (f"your group turn {t['token']} is still current: use the newest commands you received in "
                           f"it (input {t['last'] or t['token']})")
            else:
                start, through, new = self.seen(agent), len(self.transcript), self.new_token(agent)
                self.tasks[agent] = t = self.new_task(agent, new)
                t["sent"], t["running"], t["delivered"], t["rejoin"] = True, 1, through, (start, through)
                self.inputs[new] = (t, through)
                repeat = False
                self.issued[new] = agent
                self.event("rejoin", token=new, agent=agent, via=token, seqs=[start + 1, through])
                self.cv.notify_all()
        if problem:
            return refused(problem)
        if not repeat:  # followed until Herdr shows the session settled (reconcile), never prompted: nothing is sent
            threading.Thread(target=self.run_task, args=(t, "resume"), daemon=True).start()
            self.out(f"[{stamp()}] {name} rejoins the group chat from its native turn ({new})", kind="routine")
        return {"ok": True, "message": self.prompt(agent, new, start, through, rejoin=True)}

    def submit(self, token, body, pane):
        with self.cv:
            if not isinstance(body, str) or not body.strip():
                return {"ok": False, "message": "rejected: empty message"}
            t, _ = self.find(token)
            if t and pane and pane != self.panes[t["agent"]]:
                self.event("submit_rejected", token=token, reason=f"caller pane {pane}")
                return {"ok": False, "message": f"rejected: caller pane {pane} is not {LABELS[t['agent']]}'s pane"}
            # A body identical to one this task already posted is a retry (e.g. after an ambiguous client
            # error), even if meant as a repeat: acknowledged, not recorded, and no grant is used or touched.
            same = t and next((seq for seq in t["submissions"] if self.transcript[seq - 1]["text"] == body), None)
            if same:
                self.event("submit_duplicate", token=token, seq=same)
                return {"ok": True, "message": f"received earlier as message #{same}; not recorded again."}
            if t is None or self.owner is not t:
                self.event("submit_rejected", token=token, reason="not the slot owner")
                return {"ok": False, "message": f"rejected: turn {token} does not hold the speaking slot (claim first: "
                                                "each public message needs its own granted claim; a denied, used, "
                                                "or ended grant cannot post)"}
            msg = self.append(t["agent"], body, turn=token)
            t["submissions"].append(msg["seq"])
            t["delivered"] = msg["seq"]  # never sent back to its author
            self.owner = None  # one message per grant: released as soon as it is recorded
            self.event("submit_received", token=token, seq=msg["seq"], chars=len(body))
            self.event("release", token=token, agent=t["agent"], seq=msg["seq"])
            self.advance(t["agent"], msg["seq"])  # granted only when caught up; only the owner appended since
            self.out(self.show(msg), kind="message")
            self.due |= self.scope - {t["agent"]}
            if self.queue and self.queue[0][1] is None:  # a waiting plain user message joins the release
                self.publish_user()
            self.cv.notify_all()
        return {"ok": True, "message": f"received as group message #{msg['seq']}; the speaking slot is released. "
                                       "To post another message, claim again first. You may continue privately; "
                                       "otherwise end your turn (do not wait for a reply)."}

    # user commands -------------------------------------------------------
    def status(self):
        with self.cv:
            return {"paused": self.paused, "hold": self.hold, "halting": sorted(self.halting), "queued": len(self.queue),
                    "messages": len(self.transcript), "due": sorted(self.due),
                    "owner": self.owner and self.owner["token"],
                    "tasks": {a: {k: t[k] for k in ("token", "sent", "running", "submissions")}
                              for a, t in self.tasks.items()}}

    def status_text(self):
        s = self.status()
        tasks = ", ".join(f"{LABELS[a]} turn {t['token']} ({'speaking' if t['token'] == s['owner'] else 'evaluating'}"
                          f"{'' if t['sent'] else ', not sent'}"
                          + (f", submitted #{', #'.join(map(str, t['submissions']))}" if t["submissions"] else "") + ")"
                          for a, t in s["tasks"].items()) or "none"
        state = "STOPPED: " + s["hold"] if s["hold"] else ("paused" if s["paused"] else "running")
        if s["halting"]:
            state += f" (Esc: stopping {', '.join(LABELS[a] for a in s['halting'])})"
        due = ", ".join(LABELS[a] for a in s["due"]) or "none"
        return f"turns={tasks} due={due} queued={s['queued']} messages={s['messages']} state={state}"

    def status_line(self):
        """The UI's live status line, from in-memory state only (no Herdr call). There is no claim
        queue: an agent is idle, evaluating, or holding the slot; native input is reported safe
        only under the same rule as the PAUSED notice."""
        with self.cv:
            agents = []
            for a in AGENTS:
                t = self.tasks.get(a)
                state = "idle" if t is None else "holding the slot" if self.owner is t else "evaluating"
                agents.append(f"{LABELS[a]} {state}" + (" (stopped)" if t and not t["running"] else ""))
            if self.halting:
                flow = ("Esc: waiting for " + ", ".join(LABELS[a] for a in AGENTS if a in self.halting)
                        + " to stop")
            elif self.hold:
                flow = "stopped: /resume or /skip"
            elif self.paused:
                flow = ("pausing, native input not safe yet" if self.native_busy()
                        else "paused, no group turn (/pause checks native input)")
            else:
                flow = "running"
            return " │ ".join([*agents, f"{len(self.queue)} queued", flow])

    def command(self, line):
        line = line.rstrip("\n")
        if not line.strip():
            return ""
        ask = None
        if line.split(" ", 1)[0] == "/ask":
            ask, _, line = line[5:].lstrip(" ").partition(" ")
            if ask not in AGENTS or not line.strip():
                return "usage: /ask codex|claude MESSAGE"
        if ask or not line.startswith("/") or line.startswith("//"):
            text = line[1:] if line.startswith("//") and not ask else line
            with self.cv:  # durable before it is accepted: a restart queues it again until published
                qid = secrets.token_hex(4)
                self.event("user_queued", chars=len(text), qid=qid, text=text, **({"ask": ask} if ask else {}))
                self.queue.append((text, ask, qid))
                self.cv.notify_all()
                busy = self.tasks or self.live() or self.paused or self.hold
            return f"queued ({len(self.queue)} waiting)" if busy else ""
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if cmd == "pause":
            with self.cv:
                self.paused = True
                self.event("pause", owner=self.owner and self.owner["token"], tasks=sorted(self.tasks))
                busy = self.native_busy()
                held = [LABELS[a] for a in busy if not self.tasks[a]["running"]]
                if busy:
                    return (f"pausing: no new dispatches or claims; native input is NOT safe yet: "
                            f"{', '.join(LABELS[a] for a in busy)} may still be working"
                            + (f" ({', '.join(held)}: stopped, completion unconfirmed; /resume reconciles, "
                               "/skip once idle)" if held else ""))
            if unsafe := self.native_unsafe():  # observed in Herdr outside cv
                return ("pausing: no new dispatches or claims; native input is NOT safe yet: " + "; ".join(unsafe.values())
                        + ". No notice follows for a session outside a group turn: /pause again re-checks")
            return SAFE
        if cmd == "interrupt":
            return self.interrupt()
        if cmd == "resume":
            return self.resume()
        if cmd == "skip":
            return self.skip()
        if cmd == "status":
            return self.status_text()
        if cmd == "history":
            msgs = self.transcript[-int(arg):] if arg.isdigit() else self.transcript
            return "\n".join(self.show(m) for m in msgs) or "(no messages)"
        if cmd in ("exit", "exit!"):
            return self.exit(force=cmd == "exit!")
        if cmd == "help":
            return HELP
        return "unknown command; /help"

    def stop(self, **data):
        """End the chat: the dispatcher returns and no prompt is sent afterwards."""
        with self.cv:
            self.quit = True
            self.event("exit", tasks=sorted(self.tasks), **data)
            self.cv.notify_all()

    def exit(self, force):
        owned = self.cfg.get("native_argv") is not None  # fresh launch; --codex/--claude attach existing sessions
        busy = lambda: any(t["sent"] or t["running"] for t in self.tasks.values())
        refusal = ("a group turn is active; /pause and wait, or /exit! to "
                   + ("close the agent panes now, interrupting it" if owned else "leave it running natively"))
        with self.cv:
            if busy() and not force:
                return refusal
        problem = owned and not force and self.check_both()  # ordinary closing needs both bound and idle
        if problem:
            return (f"not exited: {problem}; wait or fix natively, or /exit! to close only the panes that "
                    "still host their bound sessions")
        with self.cv:
            if self.quit:  # a concurrent /exit (e.g. through ctl) already ended it
                return "group chat already ended"
            if busy() and not force:  # a turn started during the checks
                return refusal
            self.stop(force=force)
        if not owned:
            return "group chat ended; attached native Codex and Claude Code sessions are left running"
        return self.close_owned()

    def close_owned(self):
        """After stop(): close the panes this launch created, each only while it still hosts its bound
        session (agent, session, terminal, pgid). A failed or unconfirmed close is reported, not retried."""
        closed, kept = [], []
        for agent in AGENTS:
            pane = self.panes[agent]
            problem = self.check(agent, need_ready=False)
            if not problem:
                code, _, err = self.herdr.call("pane", "close", pane, timeout=60)
                if code != 0:
                    problem = f"herdr pane close failed or unconfirmed ({err or code}); it may still be open"
            self.event("close_owned", agent=agent, pane=pane, error=problem)
            if problem:
                kept.append(f"{LABELS[agent]} pane {pane} ({problem})")
            else:
                closed.append(f"{LABELS[agent]} pane {pane}")
        if not kept:
            return f"group chat ended; closed {' and '.join(closed)}"
        return (f"group chat ended, but not every owned pane was closed: closed {', '.join(closed) or 'none'}; "
                f"not closed by herdr-chat: {'; '.join(kept)}. Inspect natively; nothing was retried")

    def resume(self):
        with self.cv:
            if self.halting:
                return ("not resumed: the Esc interrupt is still stopping "
                        + " and ".join(LABELS[a] for a in AGENTS if a in self.halting))
            if not (self.paused or self.hold):
                return "not paused"
            tasks = dict(self.tasks)
        # A sent turn keeps its slot or evaluation: identity only, then wait/verify its completion.
        problem = "; ".join(p for p in (self.check(a, need_ready=not (a in tasks and tasks[a]["sent"]))
                                        for a in AGENTS) if p)
        if problem:
            return f"not resumed: {problem}"
        with self.cv:
            self.paused = False
            self.hold = None
            self.event("resume", tasks=sorted(tasks))
            self.cv.notify_all()
        held = [f"{LABELS[a]} turn {t['token']}" for a, t in tasks.items() if t["sent"] and not t["running"]]
        return "resumed" + (f": reconciling {', '.join(held)}" if held else "")

    def skip(self):
        with self.cv:
            stuck = [t for t in self.tasks.values() if not t["running"]] if self.hold else []
            if not stuck:
                return "nothing to skip (only a stopped turn can be skipped)"
        for t in stuck:  # never drop a turn whose session is still working
            problem = self.check(t["agent"]) if t["sent"] else None
            if problem:
                return f"not skipped: {problem}"
        with self.cv:
            for t in stuck:
                if self.tasks.get(t["agent"]) is not t:
                    continue
                # through/due: a restart restores only what the agent was still owed after this skip
                self.event("skip", token=t["token"], agent=t["agent"], submissions=t["submissions"],
                           through=len(self.transcript), due=t["agent"] in self.due)
                del self.tasks[t["agent"]]
                self.inputs = {k: v for k, v in self.inputs.items() if v[0] is not t}
                if self.owner is t:  # a grant without its message: nothing to deliver
                    self.owner = None
            self.cv.notify_all()
        return (f"skipped {', '.join(LABELS[t['agent']] + ' turn ' + t['token'] for t in stuck)} "
                "(tokens now stale); /resume to continue")

    def interrupt(self):
        """Esc: pause at once (no new dispatch, update, claim or grant; an unused grant is revoked), then stop both
        agents' current native turns (halt). Every current task is marked interrupted: one not yet sent is never
        sent, and what a sent one carried is not credited, so it goes with that agent's next input. Owed evaluations
        are dropped (nothing is re-asked); accepted user messages stay queued. /resume continues."""
        with self.cv:
            if self.quit:
                return "group chat already ended"
            if self.halting:
                return ("already interrupting: waiting for "
                        + " and ".join(LABELS[a] for a in AGENTS if a in self.halting) + " to stop")
            self.paused = True
            for t in self.tasks.values():
                t["interrupted"] = True
            self.event("interrupt", through=len(self.transcript), owner=self.owner and self.owner["token"],
                       tasks=sorted(t["token"] for t in self.tasks.values()), owed=sorted(self.due))
            self.owner, self.halting, self.halted = None, set(AGENTS), {}
            self.due.clear()
            self.cv.notify_all()
        for agent in AGENTS:
            threading.Thread(target=self.halt, args=(agent,), daemon=True).start()
        return ("Esc: interrupting both agents; no new dispatches or claims. Nothing is resent or re-asked; "
                "/resume continues once both are confirmed stopped")

    def halt(self, agent):
        """Stop one agent for interrupt(): Herdr's Esc key while Herdr shows the bound session working (never to a
        settled, blocked or other session), until it is settled and no wrapper thread of its task can still send or
        follow input. Its own wait (settle) ends an interrupted task; a key or its receipt alone proves nothing."""
        name, pane = LABELS[agent], self.panes[agent]
        end, keys, last = now() + min(STOP_MS, self.cfg["turn_timeout_ms"]) / 1000, 0, 0.0
        while True:
            info, problem = self.observe(agent)
            if problem:
                break
            status = info.get("agent_status")
            with self.cv:
                t = self.tasks.get(agent)
                tracked, writing = t is not None and t["running"] > 0, t is not None and t["writing"]
            if status in READY and not tracked:
                break
            if status not in READY and status != "working":
                problem = f"{name} is {status}; no Esc sent (answer it natively)"
                break
            if now() > end:
                problem = f"{name} not confirmed stopped in time ({status}, {keys} Esc sent)"
                break
            # An update being written is let through first: Esc then reaches the turn it may start.
            if status == "working" and not writing and keys < ESC_KEYS and now() - last >= ESC_GAP:
                code, _, err = self.herdr.call("agent", "send-keys", pane, "esc", timeout=60)
                self.event("interrupt_key", agent=agent, exit_code=code, error=err or None)
                if code != 0:
                    problem = f"Esc to {name} failed or unconfirmed ({err or code})"
                    break
                keys, last = keys + 1, now()
            until = () if status == "working" else ("--until", "working", "--until", "blocked")
            self.herdr.call("agent", "wait", pane, *until, "--timeout", "1000", timeout=60)
        with self.cv:
            self.halting.discard(agent)
            self.halted[agent] = (problem, keys)
            self.event("halt", agent=agent, keys=keys, error=problem)
            if self.halting:
                return
            halted, n = dict(self.halted), len(self.transcript)
            report = []
            for a in AGENTS:
                line = f"{LABELS[a]} {'stopped' if halted[a][1] else 'had no running turn'}"
                if self.seen(a) < n:
                    line += f" (#{self.seen(a) + 1}-#{n} not yet received: sent with its next input)"
                if a in self.tasks:
                    line += f"; its turn {self.tasks[a]['token']} stays stopped until /resume or /skip"
                report.append(line)
        problems = [p for p, _ in halted.values() if p]
        if problems:
            return self.set_hold("Esc interrupt not confirmed: " + "; ".join(problems)
                                 + ". A native turn may still run; check the native pane")
        self.out("INTERRUPTED (Esc): " + "; ".join(report) + ". Paused: nothing is re-asked or resent, queued "
                 "messages wait; /resume continues")


# entry points ------------------------------------------------------------------
# Slash menu: the existing commands only. A choice fills the draft; Enter then executes it.
COMMANDS = (("/help", "/help", "full help"),
            ("/status", "/status", "detailed status"),
            ("/history", "/history [N]", "reprint the public transcript (N: only the last N)"),
            ("/ask codex ", "/ask codex MESSAGE", "only Codex may answer this public message"),
            ("/ask claude ", "/ask claude MESSAGE", "only Claude Code may answer this public message"),
            ("/multi", "/multi", "multi-line message; end with /end on its own line"),
            ("/interrupt", "/interrupt", "interrupt both agents now and pause (as Esc); nothing re-asked or resent"),
            ("/pause", "/pause", "stop new dispatches and claims; running native turns finish"),
            ("/resume", "/resume", "check both sessions are ready and bound, then continue"),
            ("/skip", "/skip", "abandon stopped turns whose agent is idle (no resend)"),
            ("/exit", "/exit", "end the chat; closes only the two agent panes launched by start"),
            ("/exit!", "/exit!", "end even while agents work, interrupting their turns"))


def toolkit_problem():
    """The interactive UI needs prompt_toolkit and rich in this interpreter; checked before any pane changes."""
    try:
        import prompt_toolkit  # noqa: F401
        import rich.markdown  # noqa: F401
    except ImportError as e:
        return (f"the interactive UI needs prompt_toolkit and rich, which {sys.executable} cannot import ({e}); "
                f"run the installed herdr-chat command, or install herdr-chat into this interpreter")


def line_editor(chat, color, out):
    """Interactive UI, full screen (prompt_toolkit): a message view above the input line and the status line. The
    view follows new output at its bottom; after PgUp it keeps the shown lines in place, and output arriving below
    is counted on a notice line until PgDn reaches the end, Ctrl+End, or sending a line returns to the latest. The
    mouse wheel (or a trackpad's wheel reports) over the view moves it the same way, WHEEL rows per notch. With
    mouse reporting on, a plain drag goes to this program; terminals usually select with a modifier key instead.
    The view holds this run's output only (the transcript stays in transcript.jsonl; terminal scrollback is not used).
    Esc with no / menu open interrupts both agents (Chat.interrupt). Returns read, read_multi, run(loop), show."""
    import queue
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition, completion_is_selected, has_completions
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_width, split_lines
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.processors import BeforeInput
    from prompt_toolkit.mouse_events import MouseEventType

    def slash(text):  # a command being typed; // starts a literal message
        return text.startswith("/") and not text.startswith("//") and "\n" not in text

    class Slash(Completer):
        def get_completions(self, document, complete_event):
            text = document.text
            if slash(text) and not document.text_after_cursor:
                for cmd, shown, note in COMMANDS:
                    if cmd.startswith(text) and cmd != text:
                        yield Completion(cmd, -len(text), display=shown, display_meta=note)

    class Log(UIControl):
        """Display lines of this run's output. top is None while the view follows the bottom, else the first line
        shown (a logical line, so a resize keeps it); new/notes count messages and other output added below it."""
        def __init__(self):
            self.lock = threading.Lock()
            self.rows, self.top, self.new, self.notes, self.last = [], None, 0, 0, ""

        def add(self, text, kind):  # any thread
            rows = list(split_lines(to_formatted_text(ANSI(text.replace("\t", "    ")))))  # a tab shows as ^I
            with self.lock:
                self.rows.extend(rows)
                self.last = text
                if self.top is not None:
                    self.new += kind == "message"
                    self.notes += kind != "message"
            app.invalidate()

        def create_content(self, width, height):
            rows, start = self.rows, self.top or 0
            n = len(rows) - start  # the window keeps the cursor line visible: the newest, or the first shown
            return UIContent(get_line=lambda i: rows[start + i], line_count=n, show_cursor=False,
                             cursor_position=Point(0, 0 if self.top is not None else max(n - 1, 0)))

        def mouse_handler(self, event):  # the wheel never reaches the draft
            if event.event_type not in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                return NotImplemented
            self.scroll(up=event.event_type == MouseEventType.SCROLL_UP, step=WHEEL)

        def follow(self):
            with self.lock:
                self.top, self.new, self.notes = None, 0, 0

        def scroll(self, up, step=None):  # a page (one line of overlap), or about step rows
            info = view.render_info
            if info is None:
                return
            width, height, rows = info.window_width, info.window_height, self.rows
            size = lambda row: max(1, -(-fragment_list_width(row) // max(width, 1)))
            page = max(1, min(step or height, height - 1))

            def fits(i):  # rows[i:] fit in the view: it is at the end
                used = 0
                for row in rows[i:]:
                    used += size(row)
                    if used > height:
                        return False
                return True

            top = self.top if self.top is not None else info.first_visible_line()
            i, used = top, 0
            if up:
                if self.top is None and fits(top) and top == 0:
                    return
                while i > 0 and used + size(rows[i - 1]) <= page:
                    i, used = i - 1, used + size(rows[i - 1])
                i = min(i, max(top - 1, 0))
            elif self.top is None:
                return
            else:
                while i < len(rows) and used + size(rows[i]) <= page:
                    i, used = i + 1, used + size(rows[i])
                i = max(i, top + 1)
                if fits(i):
                    return self.follow()
            with self.lock:
                self.top = i

    WHEEL = 3  # rows per wheel notch: a small step, unlike PgUp/PgDn
    log = Log()
    mode = ["main"]  # "multi" while /multi collects lines: "... " prompt, no menu
    lines = queue.Queue()  # accepted lines (or KeyboardInterrupt) for the command loop

    def accept(buffer):
        text = buffer.text
        if text.strip() == ("/multi" if mode[0] == "main" else "/end"):
            mode[0] = "multi" if mode[0] == "main" else "main"
        log.follow()  # sending returns to the latest output
        lines.put(text)

    buf = Buffer(completer=Slash(), history=InMemoryHistory(), accept_handler=accept, multiline=False,
                 complete_while_typing=Condition(lambda: mode[0] == "main" and slash(buf.text)))
    keys = KeyBindings()

    @keys.add("tab", filter=has_completions, eager=True)
    @keys.add("enter", filter=completion_is_selected, eager=True)
    def fill(event):  # never executes: the next Enter does
        b = event.current_buffer
        if b.complete_state.current_completion is None:
            b.go_to_completion(0)
        b.complete_state = None

    @keys.add("escape", filter=has_completions, eager=True)
    def dismiss(event):
        event.current_buffer.cancel_completion()

    @keys.add("escape", filter=~has_completions)  # not eager: Alt/Meta sequences still arrive as one key
    def interrupt(event):  # the draft stays
        out(chat.interrupt())

    # Shift+Enter adds a line to the draft; Enter (and Ctrl+J) still sends. Under xterm modifyOtherKeys level 1
    # (set by run() below; Herdr then changes no other key) Shift+Enter arrives as CSI 27;2;13~, which
    # prompt_toolkit reads as Enter; here it is read as F24, a key nothing else sends.
    ANSI_SEQUENCES["\x1b[27;2;13~"] = Keys.F24

    @keys.add("f24")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @keys.add("pageup")
    def older(event):
        log.scroll(up=True)

    @keys.add("pagedown")
    def newer(event):
        log.scroll(up=False)

    @keys.add("c-end")
    def latest(event):
        log.follow()

    @keys.add("c-c")
    @keys.add("<sigint>")
    def cancel(event):  # as before: the draft is dropped; the loop warns, or cancels /multi
        mode[0] = "main"
        event.current_buffer.reset()
        lines.put(KeyboardInterrupt())

    def notice():
        new = f"{log.new} new message" + "s" * (log.new != 1)
        extra = f", {log.notes} notice" + "s" * (log.notes != 1) if log.notes else ""
        return [("bold", f" ▲ earlier output · {new}{extra} below · PgDn down · Ctrl+End latest ")]

    prompt = lambda: "... " if mode[0] == "multi" else [("bold", "group> ")] if color else "group> "
    view = Window(log, wrap_lines=True)
    app = Application(layout=Layout(FloatContainer(HSplit([
        view,
        ConditionalContainer(Window(FormattedTextControl(notice), height=1), Condition(lambda: log.top is not None)),
        Window(BufferControl(buf, input_processors=[BeforeInput(prompt)]), wrap_lines=True,
               height=Dimension(min=1, max=10), dont_extend_height=True),
        Window(FormattedTextControl(chat.status_line), style="class:bottom-toolbar", height=1)]),
        [Float(CompletionsMenu(max_height=len(COMMANDS), scroll_offset=1), xcursor=True, ycursor=True)]),
        focused_element=buf), key_bindings=keys, full_screen=True, refresh_interval=0.5, mouse_support=True,
        enable_page_navigation_bindings=False)

    def read():
        item = lines.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def run(loop):
        """The UI in this (main) thread, the command loop in another; ends with the loop."""
        failed = []

        def body():
            try:
                loop()
            except BaseException as e:
                failed.append(e)
            finally:
                app.loop.call_soon_threadsafe(lambda: app.future.done() or app.exit())

        sys.stdout.write("\x1b[>4;1m")
        sys.stdout.flush()
        try:
            app.run(pre_run=lambda: threading.Thread(target=body, daemon=True).start())
        finally:
            sys.stdout.write("\x1b[>4m")  # the terminal's default key reporting again
            sys.stdout.flush()
        print(log.last)  # the full screen is gone: keep the final reply in the terminal
        if failed:
            raise failed[0]

    return read, read, run, log.add


def markdown_renderer(color):
    """Message bodies as Markdown in the line editor (display only): GFM tables, headings, lists, code.
    Every newline stays a line break, HTML and image syntax stay literal text, links show their URL.
    Table cells wrap instead of ending in "…"; under 8 columns per table column, each row prints as
    "header: value" lines; a row with more cells than the header (GFM would drop them) shows the table
    source as written. Without color (NO_COLOR) only bold/italic/underline mark headings, emphasis and code."""
    import io
    import re
    from markdown_it import MarkdownIt
    from rich import box
    from rich.console import Console
    from rich.markdown import CodeBlock, Markdown, TableElement
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    parser = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["strikethrough", "table"])
    parser.disable("image")

    class Code(CodeBlock):  # no padding: copied lines match the source
        def __rich_console__(self, console, options):
            yield Syntax(str(self.text).rstrip(), self.lexer_name, theme=self.theme, word_wrap=True)

    class Grid(TableElement):
        @classmethod
        def create(cls, markdown, token):
            grid = cls()
            grid.source = markdown.markup.split("\n")[token.map[0]:token.map[1]]
            return grid

        def __rich_console__(self, console, options):
            heads = [c.content.copy() for c in self.header.row.cells]
            rows = [[c.content for c in r.cells] for r in self.body.rows] if self.body else []
            cells = (len(re.split(r"(?<!\\)\|", re.sub(r"^[\s>]*", "", line).strip().strip("|")))
                     for line in self.source)
            if max(cells) > len(heads):
                yield Text("\n".join(self.source))
                return
            for h in heads:
                h.stylize("markdown.table.header")
            if options.max_width < 8 * len(heads):
                for i, row in enumerate(rows):
                    yield from [Text("")] if i else []
                    yield from (Text.assemble(h, ": ", c) for h, c in zip(heads, row))
                return
            table = Table(box=box.SIMPLE, pad_edge=False, style="markdown.table.border", show_edge=True,
                          collapse_padding=True)
            for h in heads:
                table.add_column(h, overflow="fold")
            for row in rows:
                table.add_row(*row)
            yield table

    class Message(Markdown):
        elements = {**Markdown.elements, "fence": Code, "code_block": Code, "table_open": Grid}

        def __init__(self, text):
            super().__init__(text, code_theme="ansi_dark", hyperlinks=False)
            self.parsed = parser.parse(text)
            for block in self.parsed:  # a single newline is a line break, as typed
                for t in block.children or ():
                    if t.type == "softbreak":
                        t.type = "hardbreak"

    def render(text, width):
        buf = io.StringIO()
        console = Console(file=buf, width=max(width, 20), force_terminal=True, no_color=not color, highlight=False)
        console.print(Message(text))
        # Rich pads lines to the width; trailing spaces would wrap into blank lines after a resize
        sgr = r"\x1b\[[0-9;]*m"
        lines = [re.sub(rf"(?: |{sgr})+$", lambda m: "".join(re.findall(sgr, m[0])), line)
                 for line in buf.getvalue().split("\n")]
        shown = [i for i, line in enumerate(lines) if re.sub(sgr, "", line)]  # no blank lines around the body
        return lines[shown[0]:shown[-1] + 1] if shown else [""]

    return render


def interactive():
    """Whether the UI runs the line editor: a capable terminal. Elsewhere plain lines, routine notices included."""
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def cmd_ui(args):
    lock = threading.Lock()
    tty = sys.stdin.isatty()
    rich = interactive()
    color = rich and "NO_COLOR" not in os.environ
    if rich and (problem := toolkit_problem()):
        print(f"cannot start: {problem}", file=sys.stderr)
        return 1

    show = None  # the line editor's message view, once it exists

    def out(text, kind="system"):
        if kind == "routine":
            if rich:  # the status line shows it; events.jsonl keeps it
                return
            kind = "system"
        if kind != "message":
            label, code = {"system": ("system", "2"), "warning": ("warning", "1;33"),
                           "error": ("stopped", "1;31")}[kind]
            if kind == "system":
                text = tint(f"[{label}] {text}", code, color)
            else:
                text = tint(f"[{label}]", code, color) + " " + text
        if show:
            return show(text, kind)
        with lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    # One coordinator per group: held until exit; a second fails before touching state or socket.
    owner_lock = getattr(args, "lock", None) or take_lock(args.state_dir)  # `resume` took it before rebinding
    if owner_lock is None:
        out(f"another coordinator owns {args.state_dir}", kind="error")
        return 1
    chat = Chat(args.state_dir, out, color=color)
    sock = chat.cfg["socket"]
    # Recovery (start/recover verified this caller is the group's shared pane): adopt resumed sessions' new ids.
    problems = getattr(args, "recover", False) and chat.rebind()
    if problems:
        out(f"cannot recover {chat.dir}: {problems}; config.json and progress.json unchanged (load diagnostics "
            "may have been appended to events.jsonl)", kind="error")
        return 1
    problems = chat.check_both()  # also: no native turn from an earlier coordinator still running
    if problems:
        out(f"cannot start: {problems}", kind="error")
        return 1
    if os.path.exists(sock):
        try:
            request(sock, {"op": "status"}, timeout=2)
            out(f"another coordinator is serving {sock}", kind="error")
            return 1
        except ConnectionRefusedError:  # no listener: stale socket file; any other error keeps it
            os.unlink(sock)
        except OSError as e:
            out(f"cannot verify existing socket {sock} ({e}); not replaced", kind="error")
            return 1
    if rich:
        read, read_multi, run, show = line_editor(chat, color, out)
        chat.render = markdown_renderer(color)
    else:
        read = (lambda: input("group> ")) if tty else sys.stdin.readline
        read_multi = (lambda: input("... ")) if tty else sys.stdin.readline
        run = lambda loop: loop()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock)
    os.chmod(sock, 0o600)
    server.listen()
    chat.event("ui_start", panes=chat.panes, pid=os.getpid())
    out(f"Herdr group chat: Codex {chat.panes['codex']} | Claude Code {chat.panes['claude']} · state {chat.dir}\n"
        "Plain text goes to both agents; type / for commands (a choice only fills the line), /help for details.")
    if chat.transcript:  # display only: nothing is sent to the agents to redraw it, no cursor moves
        out("\n".join(chat.show(m) for m in chat.transcript), kind="message")
        out(f"saved conversation: all {len(chat.transcript)} public messages above (display only; /history repeats them)")
        unseen = [f"{LABELS[a]} #{chat.seen(a) + 1}-#{len(chat.transcript)}" for a in AGENTS
                  if a not in chat.tasks and a not in chat.due and chat.seen(a) < len(chat.transcript)]
        if unseen:
            out(f"not yet received: {', '.join(unseen)}; sent with that agent's next input")
        if chat.due:
            out(f"never sent before the restart: {', '.join(LABELS[a] for a in AGENTS if a in chat.due)} evaluate"
                f"{'s' if len(chat.due) == 1 else ''} the newest messages "
                + ("once the held turns are resolved" if chat.tasks else "now") + ", before any queued message")
    for a in AGENTS:
        if chat.cfg["identity"][a].get("pending"):
            out(f"{LABELS[a]} resumed its recorded session {chat.cfg['identity'][a]['session']}; Herdr reports that id "
                "only from its next native turn, and its receipts and claims count only once Herdr reports the same id")
    if chat.queue:
        out(f"{len(chat.queue)} accepted user message(s) not yet published are queued again in order")
    if chat.lost:
        out(f"{chat.lost} user message(s) queued under the previous coordinator were kept only in memory by "
            "its version and cannot be recovered", kind="warning")
    if chat.tasks:
        chat.set_hold("the previous coordinator ended before confirming "
                      + ", ".join(f"{LABELS[a]} turn {t['token']} (input through #{t['delivered']})"
                                  for a, t in chat.tasks.items())
                      + "; whether it reached or finished in the model is unknown. Its old commands are refused, "
                        "and its messages stay unseen (sent with that agent's next input). Check the native pane")
    threading.Thread(target=chat.serve, args=(server,), daemon=True).start()
    threading.Thread(target=chat.dispatcher, daemon=True).start()

    def loop():
        while not chat.quit:
            try:
                line = read()
            except EOFError:
                line = ""
            except KeyboardInterrupt:
                out("use /exit to end the group chat", kind="warning")
                continue
            if not tty and line == "":  # input closed: end without closing any native session
                chat.stop(reason="eof")
                out("input closed: group chat ended; native sessions left running (only /exit closes them)")
                break
            if line.strip() == "/multi":
                parts = []
                try:
                    while True:
                        part = read_multi()
                        if not tty and part == "":
                            raise EOFError
                        if part.strip() == "/end":
                            break
                        parts.append(part.rstrip("\n"))
                except (EOFError, KeyboardInterrupt):
                    out("multi-line message cancelled", kind="warning")
                    continue
                line = "\n".join(parts)
                reply = chat.command(line if not line.startswith("/") else "/" + line)
            else:
                reply = chat.command(line)
            if reply:
                kind = "message" if line.strip().split()[0] == "/history" else "system"
                if reply.startswith(("not resumed:", "not skipped:", "not exited:", "a group turn is active",
                                     "group chat ended, but", "unknown command", "usage:")):
                    kind = "warning"
                out(reply, kind=kind)
    run(loop)
    server.close()
    os.unlink(sock)
    return 0


def cmd_open(args):
    """`ui` subcommand: run the coordinator of a group of this directory (start runs it there for --codex/--claude)."""
    try:
        cfg = json.loads((Path(args.state_dir) / "config.json").read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {args.state_dir}/config.json ({e}); nothing changed")
    problem = dir_problem(cfg, os.getcwd())
    if problem:
        sys.exit(f"cannot open {args.state_dir}: {problem}; nothing changed")
    return cmd_ui(args)


def cmd_submit(args):
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            sys.exit("provide the message on stdin (heredoc) or with --file")
        body = sys.stdin.buffer.read().decode("utf-8")
        if body.endswith("\n"):
            body = body[:-1]  # the heredoc terminator line adds exactly one newline
    try:
        resp = request(args.socket, {"op": "submit", "turn": args.turn, "body": body,
                                     "pane": args.pane or os.environ.get("HERDR_PANE_ID")})
    except OSError as e:
        print(f"not delivered to the group-chat coordinator: {e}", file=sys.stderr)
        return 3
    print(resp["message"])
    return 0 if resp["ok"] else 2


def cmd_claim(args):
    try:
        resp = request(args.socket, {"op": args.cmd, "turn": args.turn,  # claim, ack or rejoin
                                     "pane": args.pane or os.environ.get("HERDR_PANE_ID")})
    except OSError as e:
        if args.cmd == "rejoin":  # the coordinator may have rejoined the agent before the reply was lost
            print(f"rejoin reply not received ({e}): whether the coordinator rejoined you is unknown. Running the same "
                  "rejoin command once more returns the same reply while it has no receipt, or the reason", file=sys.stderr)
            return 3
        print(f"not delivered to the group-chat coordinator: {e}"
              + ("; you have no reservation" if args.cmd == "claim" else ""), file=sys.stderr)
        return 3
    print(resp["message"])
    return 0 if resp["ok"] else 2


def cmd_ctl(args):
    line = " ".join(args.line)
    resp = request(args.socket, {"op": "status"} if line == "status-json" else {"op": "command", "line": line})
    print(json.dumps(resp["status"], ensure_ascii=False) if "status" in resp else resp["message"])
    return 0 if resp["ok"] else 2


def new_state_dir(path):
    state = Path(path or Path.home() / ".local/state/herdr-group-chat" / time.strftime("%Y%m%dT%H%M%S"))
    if state.exists() and (not state.is_dir() or any(state.iterdir())):
        sys.exit(f"state dir {state} is already used; nothing changed. Pass a new --state-dir")
    return state


def check_layout(herdr, panes, bottom, ok):
    layout = ok("pane", "layout", "--pane", bottom)["layout"]
    rect = {p["pane_id"]: p["rect"] for p in layout["panes"]}
    c, a, b = rect.get(panes["codex"]), rect.get(panes["claude"]), rect.get(bottom)
    if not (len(rect) == 3 and c and a and b and c["y"] == a["y"] and c["x"] < a["x"]
            and b["width"] == layout["area"]["width"] and b["y"] >= c["y"] + c["height"]):
        return f"unexpected layout, chat not started: {json.dumps(layout)}"


def bind_existing(herdr, args):
    panes = {"codex": args.codex, "claude": args.claude}
    if args.codex == args.claude:
        sys.exit("Codex and Claude Code panes must differ")
    infos = {}
    for agent, pane in panes.items():
        info, err = herdr.agent(pane)
        if err:
            sys.exit(err)
        if info.get("agent") != agent:
            sys.exit(f"{pane} hosts {info.get('agent')!r}, not {agent}")
        if info.get("agent_status") not in READY:
            sys.exit(f"{pane} ({agent}) is {info.get('agent_status')}; wait until idle/done")
        infos[agent] = info
    if infos["codex"]["workspace_id"] != infos["claude"]["workspace_id"]:
        sys.exit("both panes must be in the same Herdr workspace")

    def ok(*cmd):
        code, result, err = herdr.call(*cmd, timeout=60)
        if code != 0:
            sys.exit(f"herdr {' '.join(cmd[:2])} failed: {err}; layout may be partial, panes were not closed")
        return result

    # Layout: Codex alone in a tab, split down for the shared pane, then move Claude
    # through a cross-tab move beside Codex (same-tab moves did not reparent in smoke tests).
    tab = ok("tab", "get", infos["codex"]["tab_id"])["tab"]
    if tab["pane_count"] > 1:
        panes["codex"] = ok("pane", "move", panes["codex"], "--new-tab", "--label", args.label,
                            "--no-focus")["move_result"]["pane"]["pane_id"]
    bottom = ok("pane", "split", panes["codex"], "--direction", "down", "--ratio", str(args.ratio),
                "--cwd", args.cwd, "--no-focus")["pane"]
    tab_id = bottom["tab_id"]
    panes["claude"] = ok("pane", "move", panes["claude"], "--tab", tab_id, "--split", "right",
                         "--target-pane", panes["codex"], "--no-focus")["move_result"]["pane"]["pane_id"]
    problem = check_layout(herdr, panes, bottom["pane_id"], ok)
    if problem:
        sys.exit(problem)
    for agent in AGENTS:
        info, err = herdr.agent(panes[agent])
        if err or identity(info) != identity(infos[agent]):
            sys.exit(f"{agent} session changed during layout: {err or identity(info)}")
        infos[agent] = info
    return panes, infos, bottom["pane_id"], tab_id


def caller_pane(herdr):
    """(id, pane) of the caller: an agent-free Herdr pane whose foreground process is this launcher; else exit."""
    caller = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not caller:
        sys.exit("not inside a Herdr pane (HERDR_ENV/HERDR_PANE_ID unset); run start from an empty Herdr tab")
    code, result, err = herdr.call("pane", "get", caller, timeout=60)
    if code != 0 or not result:
        sys.exit(f"cannot read caller pane {caller}: {err or code}; nothing changed")
    pane = result["pane"]
    if pane.get("agent"):  # a tool shell of an agent inherits that agent's pane id
        sys.exit(f"caller pane {caller} hosts {pane['agent']}; run start from the shell of a new empty tab. Nothing changed")
    code, result, err = herdr.call("pane", "process-info", "--pane", caller, timeout=60)
    if code != 0 or not result:
        sys.exit(f"pane process-info {caller} failed: {err or code}; nothing changed")
    if result["process_info"].get("foreground_process_group_id") != os.getpgrp():
        sys.exit(f"this launcher is not the foreground process of caller pane {caller}; nothing changed")
    return caller, pane


def recorded_sessions(state, cfg):
    """agent -> its recorded native session id, or None: the same session restore as Chat()."""
    path = Path(state) / "progress.json"
    progress = json.loads(path.read_text()) if path.exists() else {}
    learned = {e["agent"]: e["session"] for e in read_events(state) if e["kind"] == "session_learned"}
    return {a: cfg["identity"][a]["session"] or progress.get(a, {}).get("session") or learned.get(a) for a in AGENTS}


def panes_problem(herdr, state, cfg):
    """Read-only: None if both saved panes host their recorded native sessions in the saved tab; "" if both agent
    panes are gone (an ended group); else why not."""
    sessions, problems = recorded_sessions(state, cfg), []
    for agent in AGENTS:
        info, err = herdr.agent(cfg["panes"][agent])
        problems.append(err or resumed_problem(agent, dict(cfg["identity"][agent], session=sessions[agent]), info,
                                               cfg.get("tab_id")))
    if all(p and "agent_not_found" in p for p in problems):
        return ""
    return "; ".join(p for p in problems if p) or None


def own_pane(cfg, caller, tab_id):
    """Whether the caller is the saved group's shared pane, in the same tab and Herdr server."""
    return (cfg.get("bottom_pane"), cfg.get("tab_id"), cfg.get("herdr_socket")) == (
        caller, tab_id, os.environ.get("HERDR_SOCKET_PATH"))


def group_dir(cfg):
    """The canonical directory a saved group belongs to, or None: groups saved before directory history recorded
    none, and an empty, relative or non-text value names none (never inferred from the caller)."""
    cwd = cfg.get("cwd") if isinstance(cfg, dict) else None
    return os.path.realpath(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None


def dir_problem(cfg, here):
    """None if the saved group belongs to the canonical directory here (exactly, not a parent or child); else why."""
    d = group_dir(cfg)
    if d is None:
        return f"it records no directory (saved before directory history), so it cannot be shown to belong to {here}"
    if d != here:
        return f"it belongs to {d}, not this directory {here}"


def group_problem(herdr, state, cfg, caller, tab_id):
    """Read-only, before any lock: None if this caller pane is the saved group's shared pane in the same tab and
    Herdr server and both saved panes host their recorded native sessions; "" if both agent panes are gone (an
    ended group); else why it cannot be recovered here."""
    if not own_pane(cfg, caller, tab_id):
        return f"its shared pane is {cfg.get('bottom_pane')} in tab {cfg.get('tab_id')}, not this pane {caller} in {tab_id}"
    return panes_problem(herdr, state, cfg)


def saved_group(herdr, caller, tab_id, here):
    """The one saved group (default state root) this caller pane can recover in directory here, None if there is none;
    exits on any ambiguous or mismatching candidate, before anything changed."""
    found, refused = [], []
    for path in sorted((Path.home() / ".local/state/herdr-group-chat").glob("*/config.json")):
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not own_pane(cfg, caller, tab_id):
            continue  # another pane's, tab's or Herdr server's group
        problem = group_problem(herdr, path.parent, cfg, caller, tab_id)
        if problem is None:  # recoverable here only in its own directory; an ended group stays skipped
            problem = dir_problem(cfg, here)
        if problem is None:
            found.append(str(path.parent))
        elif problem:
            refused.append(f"{path.parent} ({problem})")
    if len(found) + len(refused) > 1 or refused:
        sys.exit("saved group(s) for this pane cannot be recovered automatically: " + "; ".join(found + refused)
                 + ". Nothing changed. " + ("Choose one with `herdr-chat recover --state-dir PATH`" if found else
                                            "Recovery needs the group's own directory and both agent panes to host "
                                            "their recorded native sessions")
                 + ", or start a new group from a new empty tab")
    return found[0] if found else None


def recover(state, lock=None):
    print(f"recovering the saved group chat in {state} (same native sessions; nothing is resent)", flush=True)
    return cmd_ui(argparse.Namespace(state_dir=state, recover=True, lock=lock))


def cmd_recover(args):
    herdr = Herdr(resolve_herdr(args.herdr_bin))
    caller, pane = caller_pane(herdr)
    try:
        cfg = json.loads((Path(args.state_dir) / "config.json").read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {args.state_dir}/config.json ({e}); nothing changed")
    problem = dir_problem(cfg, os.getcwd())
    if problem:
        sys.exit(f"cannot recover {args.state_dir}: {problem}; nothing changed")
    problem = group_problem(herdr, args.state_dir, cfg, caller, pane["tab_id"])
    if problem is not None:
        sys.exit(f"cannot recover {args.state_dir}: {problem or 'both agent panes are gone'}; nothing changed")
    return recover(args.state_dir)


def launch_fresh(herdr, args, caller, pane, argv, sessions=None):
    """Turn the caller's empty single-pane tab into Codex | Claude Code over this shared pane, started with argv.
    With sessions (a native resume), each must come up as that recorded session (Codex: or not yet reported)."""
    code, result, err = herdr.call("tab", "get", pane["tab_id"], timeout=60)
    if code != 0 or not result:
        sys.exit(f"cannot read caller tab {pane['tab_id']}: {err or code}; nothing changed")
    if result["tab"]["pane_count"] != 1:
        sys.exit(f"tab {pane['tab_id']} has {result['tab']['pane_count']} panes; run start alone in a new empty tab "
                 "(or bind existing agents with --codex ID --claude ID). Nothing changed")

    created = []

    def fail(msg):
        sys.exit(f"{msg}\nGroup chat NOT started. Panes created by this launch: {created or 'none'} (left open; "
                 f"no other pane touched{'; saved group state unchanged' if sessions else ''}). Inspect or answer any "
                 f"native dialog there; to retry, close them with `herdr pane close <id>` and rerun the command in "
                 f"this pane.")

    def ok(*cmd):
        code, result, err = herdr.call(*cmd, timeout=60)
        if code != 0 or not result:
            fail(f"herdr {' '.join(cmd[:2])} failed: {err or code}")
        return result

    # Split down, swap this pane into the bottom slot, split the new upper shell right.
    top = ok("pane", "split", caller, "--direction", "down", "--ratio", str(args.ratio),
             "--cwd", args.cwd, "--no-focus")["pane"]["pane_id"]
    created.append(top)
    ok("pane", "swap", "--source-pane", caller, "--target-pane", top)
    right = ok("pane", "split", top, "--direction", "right", "--cwd", args.cwd, "--no-focus")["pane"]["pane_id"]
    created.append(right)
    panes = {"codex": top, "claude": right}
    problem = check_layout(herdr, panes, caller, ok)
    if problem:
        fail(problem)

    suffix = secrets.token_hex(2)
    results = {}

    def start(agent):  # both start in parallel; `agent start` returns only after readiness
        results[agent] = herdr.call("agent", "start", f"gc-{agent}-{suffix}", "--kind", agent, "--pane",
                                    panes[agent], "--timeout", str(args.startup_timeout_ms), "--",
                                    *argv[agent], timeout=args.startup_timeout_ms / 1000 + 60)
    print(f"starting Codex in {top} and Claude Code in {right} (waiting for native readiness)...", flush=True)
    threads = [threading.Thread(target=start, args=(a,)) for a in AGENTS]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    problems = [f"{LABELS[a]} in {panes[a]}: {results[a][2] or results[a][0]}"
                + (" (likely a trust/login/approval dialog)" if "not_ready" in (results[a][2] or "") else "")
                for a in AGENTS if results[a][0] != 0]
    infos = {}
    for agent in AGENTS:
        if results[agent][0] != 0:
            continue
        info, err = herdr.agent(panes[agent])
        if err or info.get("agent") != agent or info.get("agent_status") not in READY or info.get("cwd") != args.cwd:
            problems.append(f"{LABELS[agent]} in {panes[agent]} not ready after start: "
                            f"{err or {k: info.get(k) for k in ('agent', 'agent_status', 'cwd')}}")
        elif sessions and identity(info)["session"] not in (sessions[agent], *([None] if agent == "codex" else [])):
            problems.append(f"{LABELS[agent]} in {panes[agent]} runs session {identity(info)['session']}, not the "
                            f"recorded {sessions[agent]}")
        infos[agent] = info
    if problems:
        fail("native startup failed: " + "; ".join(problems))
    # `agent start` was observed to leave the tab's focus on an agent pane. Herdr's focus
    # command also activates the tab, so return to the shared input only if it is active.
    code, result, _ = herdr.call("tab", "get", pane["tab_id"], timeout=60)
    if code == 0 and result and result["tab"].get("focused"):
        herdr.call("pane", "focus", "--direction", "down", "--pane", top, timeout=60)
    return panes, infos, caller, pane["tab_id"]


def fresh_argv(args):
    effort = {"codex": ["-c", f"model_reasoning_effort={args.effort or 'xhigh'}"],
              "claude": ["--effort", args.effort or "max"]}
    argv = {"codex": ["--model", args.codex_model], "claude": ["--model", args.claude_model]}
    for agent in AGENTS:
        argv[agent] += effort[agent]
    if args.allow_submit:  # exactly the commands Chat.prompt() asks agents to run
        argv["claude"] += ["--allowedTools", *(f"Bash({shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} {op}:*)"
                                               for op in ("claim", "submit", "ack", "rejoin"))]
    return argv


def saved_groups(here):
    """(state, messages) of each saved group of directory here with a public conversation (default state root),
    newest first, and how many saved groups record no directory. Other groups' transcripts are not read."""
    groups, unknown = [], 0
    for path in (Path.home() / ".local/state/herdr-group-chat").glob("*/config.json"):
        try:
            d = group_dir(json.loads(path.read_text()))
            if d != here:
                unknown += d is None
                continue
            msgs = [json.loads(l) for l in (path.parent / "transcript.jsonl").read_text().splitlines()]
        except (OSError, ValueError):
            continue
        if msgs:
            groups.append((str(path.parent), msgs))
    return sorted(groups, key=lambda g: (g[1][-1]["time"], g[0]), reverse=True), unknown


def choose_group(here):
    """The saved group of directory here the user selects from a short list, or None (cancelled); exits on an
    invalid choice."""
    groups, unknown = saved_groups(here)
    if unknown:
        print(f"{unknown} saved group(s) record no directory (saved before directory history); they are not listed "
              "and cannot be resumed")
    if not groups:
        sys.exit(f"no saved group conversation for {here} under ~/.local/state/herdr-group-chat; nothing changed")
    for i, (state, msgs) in enumerate(groups, 1):
        first = msgs[0]["text"].strip().split("\n")[0]
        print(f"{i:>3}. {time.strftime('%Y-%m-%d %H:%M', time.localtime(msgs[0]['time']))}  {len(msgs):>4} messages  "
              f"{first[:60] + ('…' if len(first) > 60 else '')}  [{Path(state).name}]")
    try:
        choice = input("open which conversation? number (Enter cancels): ").strip()
    except EOFError:
        choice = ""
    if not choice:
        print("cancelled; nothing changed")
        return None
    if not choice.isdigit() or not 1 <= int(choice) <= len(groups):
        sys.exit(f"invalid choice {choice!r}; nothing changed")
    return groups[int(choice) - 1][0]


def reopen(herdr, state, cfg, sessions, caller, pane, lock, args):
    """Under coordinator.lock, with both agent panes gone: lay out this empty tab, natively resume the two recorded
    sessions (with the recorded launch options, or none for a group bound with --codex/--claude, which never
    recorded them), then bind this same group to the new panes, which it now owns."""
    if not all(sessions.values()):
        sys.exit(f"cannot resume {state}: {', '.join(LABELS[a] for a in AGENTS if not sessions[a])}'s native session "
                 "id was never recorded, and a new conversation would not be this group's; nothing changed")
    code, result, err = herdr.call("agent", "list", timeout=60)
    if code != 0 or not result:
        sys.exit(f"herdr agent list failed: {err or code}; nothing changed")
    for a in result["agents"]:  # never a second native instance of a recorded session
        if (a.get("agent_session") or {}).get("value") in sessions.values():
            sys.exit(f"cannot resume {state}: session {a['agent_session']['value']} already runs in pane "
                     f"{a.get('pane_id')} (tab {a.get('tab_id')}); nothing changed")
    options = cfg.get("native_argv") or {a: [] for a in AGENTS}  # none recorded: no invented overrides
    argv = {"codex": ["resume", sessions["codex"], *options["codex"]],
            "claude": ["--resume", sessions["claude"], *options["claude"]]}
    print(f"resuming {state}: Codex session {sessions['codex']} and Claude Code session {sessions['claude']}", flush=True)
    if cfg.get("native_argv") is None:
        print("it was bound to existing sessions (--codex/--claude), so their launch options were never recorded; "
              "resuming without overrides: each native CLI restores what its session stored (observed: the model; "
              "Codex also its effort) and takes everything else from its current configuration", flush=True)
    panes, infos, bottom, tab_id = launch_fresh(herdr, argparse.Namespace(
        ratio=0.75, cwd=group_dir(cfg), startup_timeout_ms=args.startup_timeout_ms),
        caller, pane, argv, sessions)
    ident = {a: identity(infos[a]) for a in AGENTS}
    if ident["codex"]["session"] is None:  # see observed(): verified only once Herdr reports it
        ident["codex"].update(session=sessions["codex"], pending=True)
    old = {k: cfg.get(k) for k in ("panes", "bottom_pane", "tab_id", "herdr_socket", "identity", "native_argv")}
    new = {"panes": panes, "bottom_pane": bottom, "tab_id": tab_id,  # native_argv: /exit closes these new panes
           "herdr_socket": os.environ.get("HERDR_SOCKET_PATH"), "identity": ident, "native_argv": options}
    save_config(state, dict(cfg, **new))  # one replace: both agents or neither
    with (Path(state) / "events.jsonl").open("a") as f:
        f.write(json.dumps({"time": now(), "kind": "reopen", "old": old, "new": new, "argv": argv}) + "\n")
    return cmd_ui(argparse.Namespace(state_dir=state, lock=lock))


def cmd_resume(args):
    herdr = Herdr(resolve_herdr(args.herdr_bin))
    caller, pane = caller_pane(herdr)
    state = args.state_dir or choose_group(os.getcwd())
    if state is None:
        return 0
    try:
        cfg = json.loads((Path(state) / "config.json").read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {state}/config.json ({e}); nothing changed")
    problem = dir_problem(cfg, os.getcwd())
    if problem:
        sys.exit(f"cannot resume {state}: {problem}; nothing changed")
    lock = take_lock(state)
    if lock is None:
        sys.exit(f"{state} is already open: its coordinator holds coordinator.lock (shared pane {cfg.get('bottom_pane')}"
                 f", tab {cfg.get('tab_id')}); switch there. Nothing changed")
    if cfg.get("herdr_socket") != os.environ.get("HERDR_SOCKET_PATH"):
        sys.exit(f"cannot resume {state}: it belongs to the Herdr server at {cfg.get('herdr_socket')}; nothing changed")
    problem = panes_problem(herdr, state, cfg)
    if problem is None:
        if own_pane(cfg, caller, pane["tab_id"]):
            return recover(state, lock)
        sys.exit(f"{state} still runs in Codex pane {cfg['panes']['codex']} and Claude Code pane "
                 f"{cfg['panes']['claude']} (tab {cfg.get('tab_id')}); open it there from its shared pane "
                 f"{cfg.get('bottom_pane')} with bare herdr-chat. Nothing changed")
    if problem:
        sys.exit(f"cannot resume {state}: {problem}; nothing changed")
    return reopen(herdr, state, cfg, recorded_sessions(state, cfg), caller, pane, lock, args)


def cmd_start(args):
    if bool(args.codex) != bool(args.claude):
        sys.exit("pass both --codex and --claude to bind existing panes, or neither for a fresh launch; nothing changed")
    args.cwd = os.path.realpath(args.cwd)  # the group's directory: canonical, as its history is listed
    herdr = Herdr(resolve_herdr(args.herdr_bin))
    if not args.codex:
        caller, pane = caller_pane(herdr)
        # Bare start in a saved group's shared pane (e.g. after Herdr resumed its sessions) recovers that group.
        if not args.state_dir and (found := saved_group(herdr, caller, pane["tab_id"], args.cwd)):
            return recover(found)
    state = new_state_dir(args.state_dir)
    # The UI runs in this pane (fresh launch) or in a new terminal pane (--codex/--claude).
    if (args.codex or interactive()) and (problem := toolkit_problem()):
        sys.exit(f"{problem}; nothing changed")
    if args.codex:
        panes, infos, bottom, tab_id = bind_existing(herdr, args)
        argv = None
    else:
        argv = fresh_argv(args)
        panes, infos, bottom, tab_id = launch_fresh(herdr, args, caller, pane, argv)
    state.mkdir(parents=True, exist_ok=True)
    cfg = {"herdr_bin": herdr.binary, "herdr_socket": os.environ.get("HERDR_SOCKET_PATH"),
           "socket": str(state / "chat.sock"), "panes": panes, "bottom_pane": bottom,
           "tab_id": tab_id,
           "identity": {a: identity(infos[a]) for a in AGENTS}, "native_argv": argv, "cwd": args.cwd,
           "turn_timeout_ms": args.turn_timeout_ms}
    (state / "config.json").write_text(json.dumps(cfg, indent=2))
    print(json.dumps({"state_dir": str(state), **{k: cfg[k] for k in ("socket", "panes", "bottom_pane", "tab_id")}}, indent=2))
    if argv is not None:  # fresh launch: this pane is now the shared input
        return cmd_ui(argparse.Namespace(state_dir=str(state)))
    code, _, err = herdr.call("pane", "run", bottom,
                              shlex.join([sys.executable, str(SCRIPT), "ui", "--state-dir", str(state)]), timeout=60)
    if code != 0:
        sys.exit(f"herdr pane run failed: {err}; layout may be partial, panes were not closed")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="from an empty tab, start Codex and Claude Code above this shared pane "
                                     "(or bind two existing agent panes with --codex and --claude)")
    s.add_argument("--codex", help="legacy binding: pane id hosting an existing Codex session")
    s.add_argument("--claude", help="legacy binding: pane id hosting an existing Claude Code session")
    s.add_argument("--codex-model", default="gpt-6-astra")
    s.add_argument("--claude-model", default="claude-fable-5-1")
    s.add_argument("--effort", choices=("low", "medium", "high"),
                   help="override both agents' effort (default: Codex xhigh, Claude Code max; these defaults "
                        "cannot be selected with this flag, omit it to keep them)")
    s.add_argument("--startup-timeout-ms", type=int, default=120_000)
    s.add_argument("--allow-submit", action="store_true",
                   help="fresh launch: pre-allow only this wrapper's claim, submit, ack and rejoin commands in Claude Code "
                        "(--allowedTools); by default Claude Code asks natively")
    s.add_argument("--state-dir")
    s.add_argument("--label", default="group-chat")
    s.add_argument("--ratio", type=float, default=0.75)
    s.add_argument("--cwd", default=os.getcwd())
    s.add_argument("--turn-timeout-ms", type=int, default=30 * 60 * 1000)
    s.add_argument("--herdr-bin")
    u = sub.add_parser("ui", help="run the coordinator and shared input UI (started by `start`)")
    u.add_argument("--state-dir", required=True)
    r = sub.add_parser("recover", help="from a saved group's shared pane, recover that group after Herdr resumed its "
                                       "native sessions (bare `start` there does this when the group is unique)")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--herdr-bin")
    v = sub.add_parser("resume", help="choose a saved group from a history list (or --state-dir) and continue it: in its "
                                      "shared pane, or from an empty tab by natively resuming its recorded sessions")
    v.add_argument("--state-dir")
    v.add_argument("--herdr-bin")
    v.add_argument("--startup-timeout-ms", type=int, default=120_000)
    m = sub.add_parser("submit", help="agent: publish the one group message of a granted claim (releases the slot)")
    m.add_argument("--socket", required=True)
    m.add_argument("--turn", required=True)
    m.add_argument("--file")
    m.add_argument("--pane", help="caller pane bound to the turn (default: $HERDR_PANE_ID)")
    k = sub.add_parser("claim", help="agent: claim the single public speaking slot for one message")
    k.add_argument("--socket", required=True)
    k.add_argument("--turn", required=True)
    k.add_argument("--pane", help="caller pane bound to the turn (default: $HERDR_PANE_ID)")
    a = sub.add_parser("ack", help="agent: record receipt of an update input (not a claim)")
    a.add_argument("--socket", required=True)
    a.add_argument("--turn", required=True)
    a.add_argument("--pane", help="caller pane bound to the turn (default: $HERDR_PANE_ID)")
    j = sub.add_parser("rejoin", help="agent: woken natively after its group turn ended, get the public messages it "
                                      "has not received and fresh commands (not a claim)")
    j.add_argument("--socket", required=True)
    j.add_argument("--turn", required=True, help="any input token this agent was issued in this group chat")
    j.add_argument("--pane", help="caller pane bound to the agent (default: $HERDR_PANE_ID)")
    c = sub.add_parser("ctl", help="send a UI line (e.g. /pause) or status-json to the coordinator")
    c.add_argument("--socket", required=True)
    c.add_argument("line", nargs="+")
    args = p.parse_args(argv)
    return {"start": cmd_start, "ui": cmd_open, "recover": cmd_recover, "resume": cmd_resume, "submit": cmd_submit, "claim": cmd_claim, "ack": cmd_claim, "rejoin": cmd_claim, "ctl": cmd_ctl}[args.cmd](args)


def cli():
    """`herdr-chat` entry point: bare options mean `start`; a subcommand name runs that subcommand."""
    argv = sys.argv[1:]
    return main(argv if argv and argv[0] in SUBCOMMANDS else ["start", *argv])


SUBCOMMANDS = ("start", "ui", "recover", "resume", "submit", "claim", "ack", "rejoin", "ctl")


if __name__ == "__main__":
    sys.exit(main())
