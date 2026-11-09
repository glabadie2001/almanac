"""Every Claude process on this PC: what it is, what it is doing, and the two things you can do to it.

* `snapshot()` walks the process table (Toolhelp32, every call cheap) for `claude.exe`, reads each one's
  start time, CPU time and memory, and asks Windows for the command line only when a process is new
  (one PowerShell CIM query for the new pids; cached by pid + start time).
* `classify()` names each process's role: a Claude Code `session` (interactive or a daemon-run `bg` one),
  the `daemon` supervisor, a `pty-host` for a bg session, a `headless` `claude -p` worker, the Claude
  `desktop` app (its Electron helpers fold under it), or `other`.
* Sessions join their record in `~/.claude/sessions/<pid>.json` (session id, name, busy/idle, folder,
  Remote Control id) and their transcript under `~/.claude/projects/*/<session id>.jsonl`.
* `kill()` ends a process tree (`taskkill /T /F`) and `send()` types a line into a session's console
  (`almanac.console`), each only after a fresh scan finds the same pid with the same start time.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from almanac import console

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SESSION_FLAGS = ("--remote-control", "--resume", "-r", "--continue", "-c", "-n", "--name", "--model", "--effort",
                 "--settings", "--permission-mode", "--dangerously-skip-permissions", "--agent", "--add-dir", "--fork-session")
REMOTE = "https://claude.ai/code/{}"
CPUS = os.cpu_count() or 1


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _filetime(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def process_table() -> list[dict]:
    """[{pid, ppid, exe}] for every process (Windows); [] elsewhere."""
    if sys.platform != "win32":
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(dict(pid=entry.th32ProcessID, ppid=entry.th32ParentProcessID, exe=entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def process_facts(pid: int) -> dict | None:
    """{created (FILETIME int), cpu (100 ns units), mem (bytes), path} or None when it is gone or closed to us."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        mem = PROCESS_MEMORY_COUNTERS()
        mem.cb = ctypes.sizeof(mem)
        working = mem.WorkingSetSize if psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb) else 0
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        path = buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        return dict(created=_filetime(created), cpu=_filetime(kernel) + _filetime(user), mem=working, path=path)
    finally:
        k32.CloseHandle(handle)


def command_lines(pids: list[int]) -> dict[int, str]:
    """{pid: command line} for the given pids, one PowerShell CIM query."""
    if not pids or sys.platform != "win32":
        return {}
    flt = " or ".join(f"ProcessId={int(p)}" for p in pids)
    script = (f"Get-CimInstance Win32_Process -Filter '{flt}' | Select-Object ProcessId, CommandLine | "
              "ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             timeout=20, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace").strip()
        doc = json.loads(out) if out else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    rows = doc if isinstance(doc, list) else [doc]
    return {int(r["ProcessId"]): r.get("CommandLine") or "" for r in rows if isinstance(r, dict) and r.get("ProcessId")}


def split_args(cmd: str) -> list[str]:
    """argv after the executable, Windows quoting understood well enough to read flags."""
    import shlex
    try:
        parts = shlex.split(cmd.replace("\\", "\\\\"), posix=True)
    except ValueError:
        parts = cmd.split()
    return [p.replace("\\\\", "\\") for p in parts[1:]]


def classify(path: str, cmd: str, record: dict | None) -> str:
    low_path = (path or "").lower()
    args = split_args(cmd or "")
    if "windowsapps" in low_path and "\\claude_" in low_path or any(a.startswith("--type=") for a in args):
        return "desktop-helper" if any(a.startswith("--type=") for a in args) else "desktop"
    if record:
        return "session"
    if args[:2] == ["daemon", "run"] or args[:1] == ["daemon"]:
        return "daemon"
    if "--bg-pty-host" in args:
        return "pty-host"
    if "-p" in args or "--print" in args:
        return "headless"
    if args[:1] == ["mcp"]:
        return "mcp"
    if not args or args[0] in SESSION_FLAGS or "--resume" in args:
        return "session"
    return "other"


def read_records(home: Path) -> dict[int, dict]:
    out = {}
    for path in (home / "sessions").glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("pid"), int):
            out[doc["pid"]] = doc
    return out


def short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tail_transcript(path: Path, limit: int = 40, window: int = 600_000) -> list[dict]:
    """The last `limit` readable events of a transcript: your prompts, Claude's words, its tool calls, results."""
    return read_tail(path, window)[0][-limit:]


def read_tail(path: Path, window: int = 600_000, keep: int | None = None) -> tuple[list[dict], dict]:
    """(events, meta) from the end of a transcript. meta = the latest main-thread {model, effort, last_ts}:
    every assistant record carries `message.model` and the session's `effort` at that turn."""
    meta = dict(model=None, effort=None, last_ts=None)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            raw = fh.read()
    except OSError:
        return [], meta
    lines = raw.split(b"\n")
    if size > window:
        lines = lines[1:]                                 # the first line is cut mid-record
    events: list[dict] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        kind, ts = rec.get("type"), rec.get("timestamp")
        content = (rec.get("message") or {}).get("content")
        if kind == "assistant":
            model = (rec.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                meta["model"] = model
            if rec.get("effort"):
                meta["effort"] = rec["effort"]
            meta["last_ts"] = ts or meta["last_ts"]
        if kind == "user":
            if isinstance(content, str):
                if not content.startswith("<"):           # skip harness wrappers like <command-name>
                    events.append(dict(ts=ts, kind="you", text=short(content, 600)))
                continue
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not str(block.get("text", "")).startswith("<"):
                    events.append(dict(ts=ts, kind="you", text=short(block.get("text"), 600)))
                elif block.get("type") == "tool_result":
                    body = block.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        b.get("text", "") for b in body or [] if isinstance(b, dict) and b.get("type") == "text")
                    events.append(dict(ts=ts, kind="error" if block.get("is_error") else "result",
                                       text=short(text, 160) or "(no text)"))
        elif kind == "assistant":
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append(dict(ts=ts, kind="claude", text=short(block["text"], 900)))
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    detail = (inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern")
                              or inp.get("url") or inp.get("prompt") or "")
                    events.append(dict(ts=ts, kind="tool", text=short(f"{block.get('name')}: {detail}", 200)))
    return events[-(keep or TAIL_KEEP):], meta


TAIL_KEEP = 120


class Fleet:
    """The scanner state: command lines and CPU samples cached between scans."""

    def __init__(self, home: Path | None = None, *, table=process_table, facts=process_facts, lines=command_lines):
        self.home = Path(home or Path.home() / ".claude")
        self.table, self.facts, self.lines = table, facts, lines
        self.cmd_cache: dict[tuple[int, int], str] = {}
        self.cpu_prev: dict[tuple[int, int], tuple[int, float]] = {}
        self.transcripts: dict[str, Path] = {}
        self.tails: dict[Path, tuple[tuple[int, int], list[dict], dict]] = {}
        self.lock = threading.Lock()
        self.last: dict = dict(at=0, processes=[], summary={})

    def transcript(self, session_id: str) -> Path | None:
        if not session_id:
            return None
        hit = self.transcripts.get(session_id)
        if hit and hit.exists():
            return hit
        for path in (self.home / "projects").glob(f"*/{session_id}.jsonl"):
            self.transcripts[session_id] = path
            return path
        return None

    def view(self, session_id: str) -> tuple[Path | None, list[dict], dict]:
        """(transcript path, events, meta) for a session, parsed again only when the file changed."""
        path = self.transcript(session_id)
        if path is None:
            return None, [], dict(model=None, effort=None, last_ts=None)
        try:
            st = path.stat()
        except OSError:
            return path, [], dict(model=None, effort=None, last_ts=None)
        stamp = (st.st_size, st.st_mtime_ns)
        hit = self.tails.get(path)
        if hit is None or hit[0] != stamp:
            events, meta = read_tail(path)
            hit = (stamp, events, meta)
            self.tails[path] = hit
        return path, hit[1], hit[2]

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            table = self.table()
            names = {p["pid"]: p["exe"] for p in table}
            mine = [p for p in table if p["exe"].lower() == "claude.exe"]
            facts = {p["pid"]: self.facts(p["pid"]) for p in mine}
            alive = {(pid, f["created"]) for pid, f in facts.items() if f}
            new = sorted({pid for pid, created in alive if (pid, created) not in self.cmd_cache})
            for pid, cmd in self.lines(new).items():
                if facts.get(pid):
                    self.cmd_cache[(pid, facts[pid]["created"])] = cmd
            self.cmd_cache = {k: v for k, v in self.cmd_cache.items() if k in alive}
            records = read_records(self.home)
            procs = []
            for p in mine:
                f = facts.get(p["pid"])
                if not f:
                    continue
                key = (p["pid"], f["created"])
                cmd = self.cmd_cache.get(key, "")
                record = records.get(p["pid"])
                role = classify(f["path"], cmd, record)
                prev = self.cpu_prev.get(key)
                cpu = None
                if prev and now > prev[1]:
                    cpu = round(max(0.0, (f["cpu"] - prev[0]) / 1e7 / (now - prev[1]) / CPUS * 100), 1)
                self.cpu_prev[key] = (f["cpu"], now)
                row = dict(pid=p["pid"], ppid=p["ppid"], parent=names.get(p["ppid"], ""), role=role,
                           started=round((f["created"] - 116444736000000000) / 1e7, 3), created=str(f["created"]),
                           cpu=cpu, mem_mb=round(f["mem"] / 1048576), cmd=short(cmd, 220), path=f["path"],
                           name=None, status=None, kind=None, cwd=None, session_id=None, version=None,
                           remote_url=None, updated=None, model=None, effort=None)
                if record:
                    row.update(name=record.get("name"), name_source=record.get("nameSource"), status=record.get("status"), kind=record.get("kind"),
                               cwd=record.get("cwd"), session_id=record.get("sessionId"), version=record.get("version"),
                               updated=(record.get("statusUpdatedAt") or record.get("updatedAt") or 0) / 1000 or None,
                               remote_url=REMOTE.format(record["bridgeSessionId"]) if record.get("bridgeSessionId") else None)
                    _, _, meta = self.view(row["session_id"] or "")
                    row.update(model=meta["model"], effort=meta["effort"])
                row["can_send"] = role == "session"
                row["can_kill"] = role in ("session", "headless", "desktop", "daemon", "pty-host", "mcp", "other")
                procs.append(row)
            self.cpu_prev = {k: v for k, v in self.cpu_prev.items() if k in alive}
            live_paths = {self.transcripts.get(r["session_id"]) for r in procs if r["session_id"]}
            self.tails = {p: v for p, v in self.tails.items() if p in live_paths}
            helpers = {}
            for r in procs:
                if r["role"] == "desktop-helper":
                    helpers[r["ppid"]] = helpers.get(r["ppid"], 0) + 1
            for r in procs:
                r["helpers"] = helpers.get(r["pid"], 0)
            order = {"session": 0, "headless": 1, "desktop": 2, "daemon": 3, "pty-host": 4, "mcp": 5, "other": 6, "desktop-helper": 7}
            procs.sort(key=lambda r: (order.get(r["role"], 9), -(r["updated"] or r["started"])))
            sessions = [r for r in procs if r["role"] == "session"]
            summary = dict(sessions=len(sessions), busy=sum(r["status"] not in (None, "idle") for r in sessions),
                           idle=sum(r["status"] == "idle" for r in sessions), processes=len(procs),
                           headless=sum(r["role"] == "headless" for r in procs),
                           daemon=any(r["role"] == "daemon" for r in procs), desktop=any(r["role"] == "desktop" for r in procs),
                           cpu=round(sum(r["cpu"] or 0 for r in procs), 1), mem_mb=sum(r["mem_mb"] for r in procs))
            self.last = dict(at=now, processes=procs, summary=summary)
            return self.last

    def find(self, pid: int, created: str) -> dict | None:
        """The process from a fresh scan, only when it is still the same one (pid reuse is refused)."""
        for row in self.snapshot()["processes"]:
            if row["pid"] == pid and row["created"] == str(created):
                return row
        return None

    def kill(self, pid: int, created: str) -> tuple[bool, str, dict | None]:
        row = self.find(pid, created)
        if row is None:
            return False, f"pid {pid} is gone or is now a different process; refresh", None
        if not row["can_kill"]:
            return False, f"pid {pid} is a {row['role']}; end its parent instead", row
        proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip().splitlines()
        return proc.returncode == 0, (text[-1] if text else f"taskkill exited {proc.returncode}"), row

    def send(self, pid: int, created: str, text: str, python: str = sys.executable) -> tuple[bool, str, dict | None]:
        row = self.find(pid, created)
        if row is None:
            return False, f"pid {pid} is gone or is now a different process; refresh", None
        if not row["can_send"]:
            return False, f"pid {pid} is a {row['role']}, not a session with a console", row
        ok, message = console.send(pid, text, python=python)
        return ok, message, row

    def detail(self, pid: int, limit: int = 40) -> dict | None:
        row = next((r for r in self.last.get("processes", []) if r["pid"] == pid), None)
        if row is None:
            return None
        with self.lock:
            path, events, meta = self.view(row.get("session_id") or "")
        return dict(process=row, transcript=str(path) if path else None, events=events[-limit:], meta=meta)
