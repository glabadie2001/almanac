"""Bring an old chat back: `claude --resume <id>` in the chat's own folder, in a new console on this PC.

With `remote`, the session also starts Remote Control (`--remote-control <name>`), so it can be driven from
claude.ai or the phone as well as from the console that opens on the desktop. The session keeps its id and
appends to the same transcript. `--settings` points at Almanac's settings file so a resumed chat is kept for
ten years whatever the global setting says.

Resuming sends the whole conversation again: the first reply re-reads the chat's context (the last turn's
input + cache tokens) and writes it to the prompt cache. `estimate()` prices that context at the input rate
from `prices` in config.json, a floor, since a cache write costs more than plain input.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SETTINGS = {"cleanupPeriodDays": 3650}


def settings_file(state: Path) -> Path:
    path = Path(state) / "resume-settings.json"
    if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != SETTINGS:
        path.write_text(json.dumps(SETTINGS) + "\n", encoding="utf-8")
    return path


def command(claude: str, session_id: str, settings: Path, name: str | None, remote: bool) -> list[str]:
    if not SESSION_ID.match(session_id or ""):
        raise ValueError("not a session id")
    argv = [claude, "--settings", str(settings), "--resume", session_id]
    if remote:
        argv += ["--remote-control", re.sub(r"[^\w .-]", "", name or "")[:60].strip() or f"resume-{session_id[:8]}"]
    return argv


WATCH = 4.0


def launch(argv: list[str], cwd: str, *, log: Path | None = None, watch: float | None = None, env: dict | None = None):
    """Start it in its own console and watch it for `watch` seconds. Returns (pid, None) when it is still up,
    or (pid, reason) when it died at once, the reason read from Claude's own debug log. The new session is not
    tied to Almanac's lifetime: stopping Almanac never takes a resumed chat with it."""
    if not Path(cwd).is_dir():
        raise FileNotFoundError(f"the chat's folder {cwd} is gone")
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = argv + ["--debug-file", str(log)]
    proc = subprocess.Popen(argv, cwd=cwd, creationflags=NEW_CONSOLE, close_fds=True,
                            stdin=None, stdout=None, stderr=None, startupinfo=subprocess.STARTUPINFO(), env=env)
    deadline = time.time() + (WATCH if watch is None else watch)
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            return proc.pid, f"claude exited at once (code {code}){': ' + last_error(log) if log else ''}"
        time.sleep(0.2)
    return proc.pid, None


def last_error(log: Path | None) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, AttributeError):
        return "no debug log"
    picked = [l for l in lines if "[ERROR]" in l and "CA certs" not in l] or lines[-3:]
    return " | ".join(l.split("] ", 1)[-1] for l in picked[-2:])[:400]


def clean_env(base: dict) -> dict:
    """The service's environment minus what marks a process as running inside Claude Code: a Claude started from
    a shell inside a session (Almanac started from a Claude Code shell) would otherwise think it is nested."""
    return {k: v for k, v in base.items() if not (k == "CLAUDECODE" or k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_")))}


class Prices:
    """Input prices from config.json: {model id: USD per million input tokens}. A model with no price is unpriced."""

    def __init__(self, table: dict | None = None):
        self.table = dict(table or {})

    def estimate(self, model: str | None, context: int) -> float | None:
        """USD to send `context` tokens once at the model's input rate, or None when there is no price for it."""
        if not model or not context:
            return None
        rate = self.table.get(model)
        if rate is None:
            return None
        return round(context * rate / 1_000_000, 4)
