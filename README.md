# herdr-chat

A shared group chat for one Codex CLI session, one
Claude Code session and you, running inside a
[Herdr](https://herdr.dev) tab. Both agents keep their native terminal UIs in the
upper left/right panes; a full-width input pane below is the group chat.

`herdr-chat` is an external wrapper: it drives the original Herdr CLI and does not modify Herdr, Codex or
Claude Code.

## How it works

- Each message you send starts a native evaluation turn in both agents. Each agent privately decides whether
  to answer and may keep working.
- An agent publishes only through the wrapper's `claim` and `submit` commands, which the chat teaches it in
  every input. The first valid `claim` from an agent that has received every public message gets the single
  speaking slot for exactly one message; there is no speaker queue.
- New public messages reach each agent as an update in its running native turn, or else as a new turn. Each
  input carries only the public messages that agent's session has not yet received.
- The coordinator alone calls Herdr. Typing into an agent's native input box bypasses the chat; `/pause`
  (or Esc) first.

## Requirements

Not bundled; install and authenticate these yourself:

- [Herdr](https://herdr.dev) with a running server (`herdr` on `PATH`, or `--herdr-bin`).
- The Codex CLI and Claude Code CLI, logged in, with the models you choose available to your accounts.
- Python 3.11 or newer on Linux. Only Python 3.11 on Linux has been tested. The code was checked natively with
  Herdr 0.9.3, Codex CLI 0.163 and Claude Code 2.1.304; this packaged version was checked against a fake Herdr
  only. It uses `fcntl` and Unix sockets; macOS and Windows are not tested.

Python dependencies (installed automatically): `prompt-toolkit`, `rich`, `markdown-it-py`.

## Install

From a clone, into a virtual environment of your choice:

```bash
python3.11 -m venv ~/.venvs/herdr-chat
~/.venvs/herdr-chat/bin/pip install /path/to/herdr-chat
# optional: put the command on PATH
ln -s ~/.venvs/herdr-chat/bin/herdr-chat ~/.local/bin/herdr-chat
```

Agents call back the installed module with the same interpreter (`<venv>/bin/python <site-packages>/herdr_group_chat.py claim ...`),
so keep that environment in place while a chat runs.

## Use

Open a new, empty Herdr tab (one shell pane), `cd` to the project the agents should work in, and run:

```bash
herdr-chat [--effort low|medium|high] [--codex-model MODEL] [--claude-model MODEL] [--cwd DIR]
```

It splits the tab, starts Codex top-left and Claude Code top-right in the current directory (or `--cwd`), waits
for both to be ready, and runs the chat in the bottom pane. No model turn is sent until you write a message.
If startup fails (for example a trust, login or approval dialog), it lists the panes it created and leaves them
open; close them and rerun.

- Default models are `gpt-6-astra` (Codex, effort `xhigh`) and `claude-fable-5-1` (Claude Code, effort `max`).
  These are the defaults of the original setup, not a recommendation: they must exist for your provider
  accounts. Pass `--codex-model`/`--claude-model` and `--effort` to change them. There is no model fallback.
- By default Claude Code asks for approval before running the chat's callback commands, following its own
  permission settings. `--allow-submit` pre-allows exactly the `claim`, `submit`, `ack` and `rejoin` commands
  (`--allowedTools`).
- `herdr-chat --help` shows all start options. Other subcommands: `herdr-chat resume`, `herdr-chat recover
  --state-dir PATH`, and `herdr-chat ctl --socket PATH LINE`. `claim`, `submit`, `ack` and `rejoin` are for
  the agents.

### Controls (bottom pane)

| Input | Effect |
|---|---|
| plain text | a group message (start with `//` to send a leading slash) |
| `/ask codex\|claude MESSAGE` | only that agent may answer this one public message |
| Esc or `/interrupt` | pause and interrupt both agents' current native turns; nothing is resent |
| `/pause`, `/resume` | stop new dispatches (running turns finish), then continue |
| `/skip` | abandon stopped turns whose agent is idle (no resend) |
| `/status`, `/history [N]`, `/help` | details, public transcript, full help |
| `/multi` ... `/end`, Shift+Enter | multi-line message |
| PgUp/PgDn, mouse wheel, Ctrl+End | scroll the message view; Ctrl+End or sending returns to the latest |
| `/exit`, `/exit!` | end the chat and close the two agent panes it launched (`/exit!` also while agents work) |

Typing `/` opens a command menu (descriptions are in Traditional Chinese). An Esc with the menu open only
closes the menu. Esc followed quickly by another key is read as Alt+key.

### Saved groups and recovery

- `herdr-chat resume` lists saved groups and continues the chosen one: in its own shared pane, or, once its
  agent panes are gone, from an empty tab by natively resuming the same recorded Codex and Claude Code sessions
  (`codex resume ID`, `claude --resume ID`) in the recorded working directory.
- After Herdr itself restores a group's native sessions (for example after a reboot), bare `herdr-chat` in
  the group's original bottom pane recovers it with its history and queue. Nothing is resent.
- If a group created with `--allow-submit` is resumed from a different installation or virtual environment,
  its recorded command permissions may no longer match; Claude Code then uses its native approval prompts.

## State and privacy

Each group is stored under `~/.local/state/herdr-group-chat/<timestamp>/` (or `--state-dir`; keep it short,
Unix socket paths are limited to 108 bytes): `config.json` (pane ids, native session ids, launch arguments,
working directory), `transcript.jsonl` (public messages), `events.jsonl`, `progress.json` and the control
socket. These files are local and unencrypted. The wrapper makes no network calls; it passes public messages
to the two agents, which send them to their model providers.

The agents' private reasoning and tool logs stay only in their native Codex/Claude Code session files.
Resuming a group therefore depends on those native sessions still existing and on the recorded project
directory. Groups saved without a recorded working directory resume in the current directory.

## Known limitations

- One Codex and one Claude Code session per group, on one Herdr server.
- Messages are rendered as Markdown text; images are not displayed (image syntax shows as text).
- The chat pane captures the mouse for wheel scrolling, so Herdr's drag selection and copy-on-select do not
  work over it; use your terminal's own selection bypass (often Shift+drag), which was not tested here.
- Interrupting with Esc stops the agents' foreground native turns; detached background jobs may keep running.
- A small bottom pane may not fit the full `/` menu; enlarge the pane.

## Tests

The tests drive the installed module against a fake Herdr; no model is invoked. They need Linux (PTY tests):

```bash
python3.11 -m venv .venv && .venv/bin/pip install .
.venv/bin/python -m unittest tests.test_herdr_group_chat
```

## License

No license has been chosen yet.
