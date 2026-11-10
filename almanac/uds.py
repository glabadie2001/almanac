"""Claude Code's `uds-messaging` protocol over Windows Named Pipes (`\\\\.\\pipe\\LOCAL\\cc-msg-<hex>`).

Every interactive Claude Code session advertises itself in `~/.claude/sessions/<pid>.json` alongside an
authentication token in `<pid>.<sha256(lower(pipe))>.key`. Speaking this exact wire format lets:
  * Almanac's own `SAY` and squad broadcasts deliver multiline markdown straight into a session's queue
    (`priority: "next"`) without attaching to its console or flattening newlines;
  * Gemini agents (`almanac.gemini_host`) register in `~/.claude/sessions/` so Claude's native `SendMessage`
    discovers and wakes them by name with zero Claude-side plugins;
  * Almanac itself listen as `almanac-hub` so any Claude or Gemini agent can message the operator or hub back.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_UNLIMITED_INSTANCES = 255
PIPE_BUFFER = 65536
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CROSS_SESSION_RE = re.compile(
    r"<cross-session-message\s+([^>]*)>([\s\S]*?)</cross-session-message>"
)
ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
CHANNEL_PREFIX_RE = re.compile(r"^\[channel:\s*([^\]]+)\]\r?\n")

# In-process pipe registry used when `in_memory=True` (unit tests) or as a fast loopback fallback in tests.
_MEM_LOCK = threading.Lock()
_MEM_PIPES: dict[str, "PeerServer"] = {}


def sha256_pipe(pipe_path: str) -> str:
    """Claude Code lowercases `messagingSocketPath` before computing the `.key` filename hash."""
    return hashlib.sha256(pipe_path.lower().encode("utf-8")).hexdigest()


def default_pipe_path(hex_id: str | None = None) -> str:
    ident = hex_id or secrets.token_hex(16)
    if sys.platform == "win32":
        return rf"\\.\pipe\LOCAL\cc-msg-{ident}"
    return f"/tmp/cc-msg-{ident}.sock"


def pid_domain() -> str:
    prefix = "win32" if sys.platform == "win32" else sys.platform
    return f"{prefix}:{platform.node().lower()}"


def proc_start_filetime(pid: int | None = None) -> str:
    """Windows process creation FILETIME (100ns ticks since 1601-01-01), matching Claude's `procStart`."""
    target = os.getpid() if pid is None else int(pid)
    if sys.platform == "win32":
        from almanac.fleet import process_facts
        facts = process_facts(target)
        if facts and facts.get("created"):
            return str(facts["created"])
    epoch_ms = int(time.time() * 1000)
    return str((epoch_ms + 11644473600000) * 10000)


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        from almanac.fleet import process_facts
        return process_facts(pid) is not None
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _xml_escape_attr(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_cross_session(
    body: str,
    *,
    from_pipe: str,
    from_name: str,
    from_mode: str = "prompting",
    channel: str | None = None,
) -> str:
    raw_pipe = from_pipe.removeprefix("uds:")
    attrs = (
        f'from="uds:{_xml_escape_attr(raw_pipe)}" '
        f'from-name="{_xml_escape_attr(from_name)}" '
        f'from-mode="{_xml_escape_attr(from_mode)}"'
    )
    if channel:
        attrs += f' from-channel="{_xml_escape_attr(channel)}"'
    payload = f"[channel: {channel}]\n{body}" if channel else str(body)
    return f"<cross-session-message {attrs}>\n{payload}\n</cross-session-message>"


def parse_cross_session(raw: str) -> dict | None:
    """Decode a `<cross-session-message ...>` wrapper if `raw` contains one; otherwise None."""
    if not isinstance(raw, str) or "<cross-session-message" not in raw:
        return None
    match = CROSS_SESSION_RE.search(raw)
    if not match:
        return None
    attrs = dict(ATTR_RE.findall(match.group(1)))
    body = match.group(2)
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    if body.endswith("\r\n"):
        body = body[:-2]
    elif body.endswith("\n"):
        body = body[:-1]
    channel = attrs.get("from-channel")
    ch_match = CHANNEL_PREFIX_RE.match(body)
    if ch_match:
        channel = channel or ch_match.group(1).strip()
        body = body[ch_match.end():]
    return dict(
        from_pipe=(attrs.get("from") or "").removeprefix("uds:") or None,
        from_name=attrs.get("from-name") or None,
        from_mode=attrs.get("from-mode") or None,
        channel=channel or None,
        body=body,
    )


def discover_peers(sessions_dir: Path, *, alive_fn=None) -> list[dict]:
    """Every session record in `sessions_dir` (`~/.claude/sessions`), annotated with liveness and key presence."""
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return []
    check_alive = alive_fn or pid_alive
    out: list[dict] = []
    for path in sorted(sessions_dir.glob("*.json")):
        if not path.stem.isdigit():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("pid"), int):
            continue
        pid = doc["pid"]
        pipe = doc.get("messagingSocketPath") or None
        key_path = sessions_dir / f"{pid}.{sha256_pipe(pipe)}.key" if pipe else None
        out.append(
            dict(
                pid=pid,
                session_id=doc.get("sessionId"),
                name=doc.get("name"),
                cwd=doc.get("cwd"),
                provider=doc.get("provider") or "claude",
                model=doc.get("model"),
                effort=doc.get("effort"),
                role_name=doc.get("roleName"),
                squad_id=doc.get("squadId"),
                channels=doc.get("channels") if isinstance(doc.get("channels"), list) else [],
                status=doc.get("status"),
                kind=doc.get("kind"),
                entrypoint=doc.get("entrypoint"),
                proc_start=str(doc.get("procStart") or ""),
                pipe=pipe,
                has_key=bool(key_path and key_path.exists()),
                alive=bool(check_alive(pid)),
                transcript_path=doc.get("transcriptPath"),
                updated_at=doc.get("statusUpdatedAt") or doc.get("updatedAt"),
            )
        )
    return out


def resolve_peer(sessions_dir: Path, target: str, *, alive_fn=None) -> dict | None:
    """Find one live peer with a messaging pipe by name, sessionId, pid, or pipe path."""
    if not target:
        return None
    needle = str(target).strip()
    raw_pipe = needle.removeprefix("uds:")
    peers = [p for p in discover_peers(sessions_dir, alive_fn=alive_fn) if p["alive"] and p["pipe"]]
    # Exact name match wins (most recent updated_at if duplicate names exist)
    by_name = [p for p in peers if p.get("name") == needle]
    if by_name:
        by_name.sort(key=lambda p: p.get("updated_at") or 0, reverse=True)
        return by_name[0]
    for p in peers:
        if (
            p.get("session_id") == needle
            or str(p["pid"]) == needle
            or (p["pipe"] and p["pipe"].lower() == raw_pipe.lower())
        ):
            return p
    return None


def read_peer_token(sessions_dir: Path, pid: int, pipe_path: str) -> str:
    key_path = Path(sessions_dir) / f"{pid}.{sha256_pipe(pipe_path)}.key"
    if not key_path.exists():
        raise FileNotFoundError(f"missing uds key file for pid {pid}: {key_path.name}")
    doc = json.loads(key_path.read_text(encoding="utf-8"))
    token = doc.get("peerToken") or doc.get("token")
    if not token or not isinstance(token, str):
        raise ValueError(f"key file {key_path.name} has no peerToken")
    return token


def write_named_pipe(pipe_path: str, data: bytes, *, timeout: float = 5.0) -> None:
    """Write `data` to a Windows Named Pipe (or registered in-memory test pipe)."""
    with _MEM_LOCK:
        mem_srv = _MEM_PIPES.get(pipe_path.lower())
    if mem_srv is not None:
        mem_srv.accept_bytes(data)
        return
    if sys.platform != "win32":
        raise OSError(f"named pipe transport unavailable on {sys.platform} for {pipe_path}")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    deadline = time.monotonic() + max(0.1, timeout)
    handle = INVALID_HANDLE
    while True:
        handle = k32.CreateFileW(
            pipe_path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if handle and handle != INVALID_HANDLE:
            break
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_BUSY and time.monotonic() < deadline:
            wait_ms = max(10, int((deadline - time.monotonic()) * 1000))
            k32.WaitNamedPipeW(pipe_path, min(wait_ms, 500))
            continue
        raise OSError(f"cannot connect to pipe {pipe_path} (Win32 error {err})")
    try:
        view = memoryview(data)
        sent = 0
        while sent < len(data):
            chunk = bytes(view[sent:])
            written = wintypes.DWORD(0)
            ok = k32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None)
            if not ok:
                raise OSError(f"WriteFile failed on {pipe_path} (Win32 error {ctypes.get_last_error()})")
            sent += written.value
        k32.FlushFileBuffers(handle)
    finally:
        k32.CloseHandle(handle)


def send_frame(
    pipe_path: str,
    token: str,
    *,
    body: str,
    from_pipe: str,
    from_name: str,
    from_mode: str = "prompting",
    channel: str | None = None,
    priority: str = "next",
    write_fn=None,
) -> dict:
    msg_uuid = str(uuid.uuid4())
    msg_id = secrets.token_hex(8)
    sender_pipe = from_pipe.removeprefix("uds:")
    wrapped = format_cross_session(
        body,
        from_pipe=sender_pipe,
        from_name=from_name,
        from_mode=from_mode,
        channel=channel,
    )
    auth_frame = json.dumps({"type": "auth", "token": token})
    user_frame = json.dumps(
        {
            "type": "user",
            "uuid": msg_uuid,
            "msg_id": msg_id,
            "priority": priority,
            "from": f"uds:{sender_pipe}",
            "message": {"role": "user", "content": wrapped},
        }
    )
    wire = (auth_frame + "\n" + user_frame + "\n").encode("utf-8")
    (write_fn or write_named_pipe)(pipe_path, wire)
    return dict(
        ok=True,
        uuid=msg_uuid,
        msg_id=msg_id,
        from_name=from_name,
        from_pipe=sender_pipe,
        target_pipe=pipe_path,
        channel=channel,
    )


def send_peer(
    sessions_dir: Path,
    target: str,
    body: str,
    *,
    from_name: str = "operator",
    from_pipe: str | None = None,
    channel: str | None = None,
    alive_fn=None,
    write_fn=None,
) -> dict:
    """Resolve `target` in `sessions_dir`, authenticate with its `.key`, and deliver a `<cross-session-message>`."""
    peer = resolve_peer(sessions_dir, target, alive_fn=alive_fn)
    if peer is None:
        live = [p.get("name") or str(p["pid"]) for p in discover_peers(sessions_dir, alive_fn=alive_fn) if p["alive"] and p["pipe"]]
        raise LookupError(f"peer {target!r} not found among live uds peers ({', '.join(live) or 'none'})")
    token = read_peer_token(sessions_dir, peer["pid"], peer["pipe"])
    sender_pipe = from_pipe or default_pipe_path("almanac0000000000000000000000hub")
    res = send_frame(
        peer["pipe"],
        token,
        body=body,
        from_pipe=sender_pipe,
        from_name=from_name,
        channel=channel,
        write_fn=write_fn,
    )
    res.update(
        target_name=peer.get("name") or str(peer["pid"]),
        target_pid=peer["pid"],
        target_session=peer.get("session_id"),
        target_provider=peer.get("provider") or "claude",
    )
    return res


class PeerServer:
    """A live `uds-messaging` listener + `~/.claude/sessions/<pid>.json` registration."""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        name: str,
        pid: int | None = None,
        session_id: str | None = None,
        cwd: str | None = None,
        provider: str = "almanac",
        model: str | None = None,
        effort: str | None = None,
        role_name: str | None = None,
        squad_id: str | None = None,
        channels: list[str] | None = None,
        transcript_path: str | None = None,
        inbox_path: Path | None = None,
        pipe_path: str | None = None,
        on_message=None,
        in_memory: bool = False,
    ):
        self.sessions_dir = Path(sessions_dir)
        self.name = name
        self.pid = os.getpid() if pid is None else int(pid)
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd or os.getcwd()
        self.provider = provider
        self.model = model
        self.effort = effort
        self.role_name = role_name
        self.squad_id = squad_id
        self.channels = list(channels or [])
        self.transcript_path = transcript_path
        self.inbox_path = Path(inbox_path) if inbox_path else None
        self.pipe_path = pipe_path or default_pipe_path()
        self.token = secrets.token_hex(16)
        self.proc_start = proc_start_filetime(self.pid)
        self.domain = pid_domain()
        self.on_message = on_message
        self.in_memory = in_memory or (sys.platform != "win32")
        self.status = "idle"
        self.started_ms = int(time.time() * 1000)
        self.stop_event = threading.Event()
        self._meta_lock = threading.Lock()
        self._inbox_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def session_file(self) -> Path:
        return self.sessions_dir / f"{self.pid}.json"

    @property
    def key_file(self) -> Path:
        return self.sessions_dir / f"{self.pid}.{sha256_pipe(self.pipe_path)}.key"

    def _write_registration(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        now_ms = int(time.time() * 1000)
        meta = {
            "pid": self.pid,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "startedAt": self.started_ms,
            "procStart": self.proc_start,
            "version": "2.1.322",
            "peerProtocol": 1,
            "peerFeatures": ["notify_idle", "artifact_yield"],
            "kind": "interactive",
            "entrypoint": "cli",
            "pidDomain": self.domain,
            "messagingSocketPath": self.pipe_path,
            "name": self.name,
            "nameSource": "user",
            "nameSince": self.started_ms,
            "status": self.status,
            "updatedAt": now_ms,
            "statusUpdatedAt": now_ms,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "roleName": self.role_name,
            "squadId": self.squad_id,
            "channels": self.channels,
            "transcriptPath": self.transcript_path,
        }
        key = {
            "peerToken": self.token,
            "procStartFt": self.proc_start,
            "pidDomain": self.domain,
        }
        with self._meta_lock:
            tmp_meta = self.session_file.with_suffix(".json.tmp")
            tmp_key = self.key_file.with_suffix(".key.tmp")
            tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
            tmp_meta.replace(self.session_file)
            tmp_key.write_text(json.dumps(key), encoding="utf-8")
            tmp_key.replace(self.key_file)

    def set_status(self, status: str, *, model: str | None = None, effort: str | None = None) -> None:
        self.status = status
        if model:
            self.model = model
        if effort:
            self.effort = effort
        if not self.stop_event.is_set():
            try:
                self._write_registration()
            except OSError:
                pass

    def start(self) -> "PeerServer":
        if self.in_memory:
            with _MEM_LOCK:
                _MEM_PIPES[self.pipe_path.lower()] = self
            self._write_registration()
            return self
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve_win32, args=(ready,), name=f"uds-{self.name}", daemon=True
        )
        self._thread.start()
        ready.wait(timeout=3.0)
        self._write_registration()
        return self

    def accept_bytes(self, raw_bytes: bytes) -> int:
        """Authenticate and dispatch one connection's newline-delimited frames. Returns user messages accepted."""
        authed = False
        accepted = 0
        for raw_line in raw_bytes.decode("utf-8", "replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype == "auth":
                given = str(frame.get("token") or "")
                if hmac.compare_digest(given.encode("utf-8"), self.token.encode("utf-8")):
                    authed = True
                continue
            if not authed or ftype != "user":
                continue
            content = (frame.get("message") or {}).get("content") or ""
            parsed = parse_cross_session(content) or dict(
                from_pipe=(frame.get("from") or "").removeprefix("uds:") or None,
                from_name=None,
                from_mode=None,
                channel=None,
                body=str(content),
            )
            now_iso = datetime.now(timezone.utc).isoformat()
            msg = dict(
                ts=time.time(),
                ts_iso=now_iso,
                uuid=frame.get("uuid"),
                msg_id=frame.get("msg_id") or secrets.token_hex(8),
                priority=frame.get("priority") or "next",
                from_pipe=parsed["from_pipe"] or (frame.get("from") or "").removeprefix("uds:") or None,
                from_name=parsed["from_name"] or "peer",
                from_mode=parsed["from_mode"] or "prompting",
                channel=parsed.get("channel"),
                to_name=self.name,
                to_session=self.session_id,
                body=parsed["body"],
            )
            if self.inbox_path is not None:
                self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
                with self._inbox_lock:
                    with self.inbox_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": now_iso, "frame": frame, "decoded": msg}) + "\n")
            accepted += 1
            if self.on_message is not None:
                try:
                    self.on_message(msg)
                except Exception:
                    pass
        return accepted

    def _serve_win32(self, ready: threading.Event) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateNamedPipeW.restype = wintypes.HANDLE
        first = True
        while not self.stop_event.is_set():
            handle = k32.CreateNamedPipeW(
                self.pipe_path,
                PIPE_ACCESS_DUPLEX,
                0,
                PIPE_UNLIMITED_INSTANCES,
                PIPE_BUFFER,
                PIPE_BUFFER,
                0,
                None,
            )
            if first:
                first = False
                ready.set()
            if not handle or handle == INVALID_HANDLE:
                time.sleep(0.05)
                continue
            connected = k32.ConnectNamedPipe(handle, None)
            err = ctypes.get_last_error() if not connected else 0
            if not connected and err != ERROR_PIPE_CONNECTED:
                k32.CloseHandle(handle)
                continue
            if self.stop_event.is_set():
                k32.DisconnectNamedPipe(handle)
                k32.CloseHandle(handle)
                break
            threading.Thread(
                target=self._read_client_win32, args=(k32, handle), daemon=True
            ).start()

    def _read_client_win32(self, k32, handle) -> None:
        chunks: list[bytes] = []
        buf = ctypes.create_string_buffer(PIPE_BUFFER)
        try:
            while True:
                read_n = wintypes.DWORD(0)
                ok = k32.ReadFile(handle, buf, PIPE_BUFFER, ctypes.byref(read_n), None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA):
                        break
                    break
                if read_n.value == 0:
                    break
                chunks.append(buf.raw[: read_n.value])
        finally:
            k32.DisconnectNamedPipe(handle)
            k32.CloseHandle(handle)
        if chunks:
            self.accept_bytes(b"".join(chunks))

    def close(self) -> None:
        self.stop_event.set()
        with _MEM_LOCK:
            if _MEM_PIPES.get(self.pipe_path.lower()) is self:
                _MEM_PIPES.pop(self.pipe_path.lower(), None)
        if not self.in_memory and sys.platform == "win32" and self._thread and self._thread.is_alive():
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateFileW.restype = wintypes.HANDLE
                h = k32.CreateFileW(self.pipe_path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
                if h and h != INVALID_HANDLE:
                    k32.CloseHandle(h)
            except OSError:
                pass
            self._thread.join(timeout=1.0)
        for path in (self.session_file, self.key_file):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

