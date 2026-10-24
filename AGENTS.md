# Contributing notes for coding agents

- `src/herdr_group_chat.py` is a single module by design; keep changes small and avoid new frameworks or
  dependencies.
- Herdr is external: drive its original CLI, never modify or fork it.
- Agents' callback commands run the installed module with the same interpreter
  (`sys.executable` plus the module path); keep that, and Claude Code's `--allowedTools` patterns, in sync.
- Do not commit local state, conversation transcripts, native session logs, pane/session ids from real runs,
  or machine-specific paths. Synthetic ids in tests are fine.
- Saved groups are scoped to their recorded canonical `cwd` (exact match after resolving symlinks); every user-facing
  open route (`resume` list and `--state-dir`, `recover`, bare-start recovery, `ui`) checks it before reading a
  transcript or changing state. Never infer a missing or relative `cwd` from the caller.
- Run `python -m unittest tests.test_herdr_group_chat` with the package installed in a virtual environment;
  tests use a fake Herdr and never start a model.
