"""Almanac's HTTP routes, behind its own local door (`almanac.web`): mission control for every Claude process on
this PC, plus the efficiency report and probes.

GET  /api/fleet              every claude.exe, classified, joined to its session record (cached ~1.5 s)
GET  /api/session?pid=N      one process with its transcript tail, model/effort and token totals
GET  /api/sessions?pids=a,b  the same for many at once (the gallery), `limit` events each
POST /api/kill  {pid, created}         end the process tree
POST /api/send  {pid, created, text}   type one line + Enter into a session's console
GET  /api/actions            the kill/send log
GET  /api/report?days=&bucket=         the efficiency report
POST /api/ingest             read the transcripts now

Every route but /ident and /healthz needs the door token (bearer) or the signed-in browser cookie.
"""
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


STATIC = Path(__file__).resolve().parent / "static"
PAGES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"),
         "/app.css": ("app.css", "text/css")}
FLEET_TTL = 1.5


class Service:
    """The store, the fleet scanner and the background ingest loop."""

    def __init__(self, store, transcripts, interval=300, fleet=None, state=None, claude="claude", prices=None, namer=None):
        self.store, self.transcripts, self.interval = store, Path(transcripts), interval
        self.fleet = fleet
        # absolute: paths built from it are handed to a Claude that runs in the chat's folder, not ours
        self.state = Path(state).resolve() if state else None
        self.archive_dir = self.state / "archive" / "projects" if self.state else None
        self.claude, self.prices = claude, prices
        self.launched: dict[str, int] = {}          # session id -> pid of a resume Almanac started
        self.namer = namer
        self.naming = threading.Lock()              # one naming run at a time
        self.names: dict = dict(running=False, done=0, failed=0, usd=0.0, last=None)
        self.last_archive: dict = {}
        self.stop = threading.Event()
        self.ingesting = threading.Lock()
        self.fleet_lock = threading.Lock()
        self.error = ""

    def ingest(self):
        if not self.ingesting.acquire(blocking=False):
            return dict(busy=True)
        try:
            result = self.store.ingest(self.transcripts)
            self.error = result.get("error", "")
            if self.archive_dir is not None:
                from almanac import archive
                self.last_archive = dict(archive.sync(self.transcripts, self.archive_dir), at=time.time())
                result = dict(result, archive=self.last_archive)
            return result
        except Exception as exc:       # keep serving the history already kept
            self.error = f"{type(exc).__name__}: {exc}"
            logging.getLogger("almanac").exception("ingest failed")
            return dict(error=self.error)
        finally:
            self.ingesting.release()

    def fleet_now(self):
        if self.fleet is None:
            return dict(at=time.time(), processes=[], summary={}, error="process scanning is off")
        with self.fleet_lock:
            if time.time() - self.fleet.last.get("at", 0) > FLEET_TTL:
                self.fleet.snapshot()
            return self.fleet.last

    # -------------------------------------------------------------- naming
    def name_one(self, session_id: str) -> dict:
        """Name one ended chat with Haiku and write it back into its transcript. Never touches a live session's file
        or a chat that already has a custom title."""
        from almanac import archive, namer as nm
        meta = self.store.chat(session_id)
        if meta is None:
            return dict(id=session_id, ok=False, error="unknown chat")
        if meta.get("custom_title"):
            return dict(id=session_id, ok=True, skipped="already named", name=meta["custom_title"])
        if session_id in _live_sessions(self):
            return dict(id=session_id, ok=False, error="still running; named when it ends")
        source = self.transcripts / meta["project"] / f"{session_id}.jsonl"
        path = source if source.exists() else (archive.find(session_id, self.archive_dir) if self.archive_dir else None)
        if path is None:
            return dict(id=session_id, ok=False, error="no transcript")
        try:
            name, usd = self.namer.name(nm.digest(path))
        except Exception as exc:
            self.store.set_name(session_id, None, 0.0, f"{type(exc).__name__}: {exc}"[:300])
            return dict(id=session_id, ok=False, error=str(exc)[:300])
        written = source.exists() and nm.write_back(source, session_id, name)
        self.store.set_name(session_id, name, usd, None)
        return dict(id=session_id, ok=True, name=name, usd=usd, written_back=written)

    def name_many(self, ids: list[str]) -> None:
        if not self.naming.acquire(blocking=False):
            return
        try:
            self.names.update(running=True, queued=len(ids))
            for sid in ids:
                if self.stop.is_set():
                    break
                r = self.name_one(sid)
                key = "done" if r.get("ok") and r.get("name") and not r.get("skipped") else "failed" if not r.get("ok") else None
                if key:
                    self.names[key] += 1
                self.names["usd"] = round(self.names["usd"] + float(r.get("usd") or 0), 4)
                self.names["last"] = r
        finally:
            self.names.update(running=False, queued=0)
            self.naming.release()

    def name_backlog(self, quiet_minutes: float = 0) -> int:
        ids = [c["id"] for c in self.store.unnamed(time.time() - quiet_minutes * 60, limit=5000)]
        live = _live_sessions(self)
        ids = [i for i in ids if i not in live]
        if ids and self.namer is not None:
            threading.Thread(target=self.name_many, args=(ids,), name="almanac-names", daemon=True).start()
        return len(ids)

    def name_when_ended(self, session_id: str, wait: float = 300) -> None:
        """The SessionEnd hook fires while Claude is still exiting: wait for the process to go, then name it."""
        def run():
            deadline = time.time() + wait
            while time.time() < deadline and not self.stop.is_set():
                with self.fleet_lock:
                    if self.fleet:
                        self.fleet.last["at"] = 0
                if session_id not in _live_sessions(self):
                    self.ingest()            # read its last lines first
                    self.name_many([session_id])
                    return
                time.sleep(2)
        threading.Thread(target=run, name=f"almanac-name-{session_id[:8]}", daemon=True).start()

    def loop(self):
        while not self.stop.is_set():
            self.ingest()
            if self.namer is not None:
                self.name_backlog(quiet_minutes=10)       # anything the hook missed (crash, kill, older Claude)
            self.stop.wait(self.interval)

    def start(self):
        threading.Thread(target=self.loop, name="almanac-ingest", daemon=True).start()


def _live_sessions(service) -> dict:
    """{session id: pid} of the sessions running now. A session Almanac just launched counts while its process
    lives, because Claude Code can take a minute to write the record that names its session."""
    fleet = service.fleet_now()
    live = {p["session_id"]: p["pid"] for p in fleet.get("processes", []) if p.get("session_id")}
    pids = {p["pid"] for p in fleet.get("processes", [])}
    for sid, pid in list(service.launched.items()):
        if pid in pids or service.fleet is None:       # no scanner: nothing says it ended
            live.setdefault(sid, pid)
        elif fleet.get("processes") is not None and service.fleet is not None:
            service.launched.pop(sid, None)
    return live


def chats(service, query="", limit=300):
    from almanac import archive
    live = _live_sessions(service)
    out = []
    for c in service.store.chats(query, max(1, min(2000, limit))):
        archived = archive.find(c["id"], service.archive_dir) if service.archive_dir else None
        source = service.transcripts / c["project"] / f"{c['id']}.jsonl"
        c.update(live_pid=live.get(c["id"]), archived=bool(archived), on_disk=source.exists(),
                 resume_usd=service.prices.estimate(c["model"], c["context"]) if service.prices else None)
        c["resumable"] = (c["archived"] or c["on_disk"]) and not c["live_pid"]
        out.append(c)
    return dict(chats=out, archive=service.last_archive)


def chat_detail(service, session_id, limit=300):
    from almanac import archive
    from almanac.fleet import read_tail
    meta = service.store.chat(session_id)
    if meta is None:
        return None
    source = service.transcripts / meta["project"] / f"{session_id}.jsonl"
    path = source if source.exists() else (archive.find(session_id, service.archive_dir) if service.archive_dir else None)
    events, tail = read_tail(path, window=4_000_000, keep=max(1, min(1000, limit))) if path else ([], {})
    meta.update(live_pid=_live_sessions(service).get(session_id), on_disk=source.exists(),
                archived=bool(service.archive_dir and archive.find(session_id, service.archive_dir)),
                effort=tail.get("effort"), resume_usd=service.prices.estimate(meta["model"], meta["context"]) if service.prices else None)
    return dict(chat=meta, events=events, read_from="transcript" if path == source else "archive" if path else None)


def resume_chat(service, session_id, remote=True):
    """(ok, message, extra) — restore the transcript if Claude Code pruned it, then start `claude --resume`."""
    from almanac import archive, resume
    if not isinstance(session_id, str) or not resume.SESSION_ID.match(session_id):
        raise ValueError("id must be a session id")
    meta = service.store.chat(session_id)
    if meta is None:
        return False, "Almanac has never seen that chat", {}
    extra = dict(title=meta["title"])
    pid = _live_sessions(service).get(session_id)
    if pid:
        return False, f"already running as pid {pid}; open it in the fleet", dict(extra, pid=pid)
    restored = []
    source = service.transcripts / meta["project"] / f"{session_id}.jsonl"
    if service.archive_dir and archive.find(session_id, service.archive_dir):
        restored = archive.restore(session_id, service.archive_dir, service.transcripts)["restored"]
    if not source.exists():
        return False, "its transcript is neither on disk nor in the archive", extra
    try:
        argv = resume.command(service.claude, session_id, resume.settings_file(service.state), meta["title"], remote)
        log = service.state / "resume-logs" / f"{session_id}.log"
        pid, died = resume.launch(argv, meta["cwd"], log=log, env=resume.clean_env(dict(os.environ)))
    except (OSError, ValueError) as exc:
        return False, str(exc), extra
    if died:
        return False, died, dict(extra, pid=pid, log=str(log))
    service.launched[session_id] = pid
    with service.fleet_lock:
        if service.fleet:
            service.fleet.last["at"] = 0
    note = f"; restored {len(restored)} file(s) from the archive" if restored else ""
    return True, f"claude --resume started as pid {pid} in {meta['cwd']}{' with Remote Control' if remote else ''}{note}", \
        dict(extra, pid=pid, restored=restored)


def _pid(body):
    pid = body.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    created = body.get("created")
    if not isinstance(created, str) or not created.isdigit():
        raise ValueError("created must be the process start stamp from /api/fleet")
    return pid, created


def make_handler(service, token, *, hosts=()):
    from almanac import web

    class Handler(web.JsonHandler):
        protocol_version = "HTTP/1.1"
        pages = PAGES
        static_dir = STATIC
        log = logging.getLogger("almanac")

        def _who(self):
            return "operator"

        def _dispatch(self):
            url = urlsplit(self.path)
            path = unquote(url.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            method = "GET" if self.command == "HEAD" else self.command
            if method == "GET":
                if path == "/ident":
                    return self._send(200, dict(kind="almanac", pid=os.getpid()))
                if path == "/healthz":
                    return self._send(200, dict(ok=not service.error, last_ingest=service.store.last_ingest))
                if path == "/api/report":
                    days = int(query.get("days", "30"))
                    if not 0 <= days <= 3650:
                        raise ValueError("days is 0..3650")
                    return self._send(200, service.store.report(days, query.get("bucket", "day")))
                if path == "/api/fleet":
                    doc = service.fleet_now()
                    titles = service.store.titles([p["session_id"] for p in doc.get("processes", []) if p.get("session_id")])
                    for p in doc.get("processes", []):
                        p["title"] = titles.get(p.get("session_id"))
                    return self._send(200, doc)
                if path == "/api/names":
                    return self._send(200, service.names)
                if path == "/api/session":
                    pid = int(query.get("pid", "0"))
                    service.fleet_now()
                    found = service.fleet.detail(pid) if service.fleet else None
                    if found is None:
                        return self._send(404, {"error": f"no Claude process {pid}"})
                    sid = found["process"].get("session_id")
                    found["usage"] = service.store.session_usage(sid) if sid else None
                    return self._send(200, found)
                if path == "/api/sessions":
                    pids = [int(p) for p in query.get("pids", "").split(",") if p.strip()][:40]
                    limit = max(1, min(120, int(query.get("limit", "40"))))
                    service.fleet_now()
                    out = []
                    for pid in pids:
                        found = service.fleet.detail(pid, limit) if service.fleet else None
                        if found is None:
                            out.append(dict(pid=pid, gone=True))
                            continue
                        sid = found["process"].get("session_id")
                        found["usage"] = service.store.session_usage(sid) if sid else None
                        out.append(found)
                    return self._send(200, dict(sessions=out))
                if path == "/api/chats":
                    return self._send(200, chats(service, query.get("q", ""), int(query.get("limit", "300"))))
                if path == "/api/chat":
                    found = chat_detail(service, query.get("id", ""), int(query.get("limit", "300")))
                    if found is None:
                        return self._send(404, {"error": "no such chat"})
                    return self._send(200, found)
                if path == "/api/actions":
                    return self._send(200, dict(actions=service.store.actions(int(query.get("limit", "50")))))
                if self._page(path):
                    return None
                return self._send(404, {"error": "Not found"})
            if method == "POST" and path == "/api/ingest":
                return self._send(200, service.ingest())
            if method == "POST" and path == "/api/name":
                if service.namer is None:
                    return self._send(503, {"error": "naming is off"})
                body = self._body()
                if body.get("all"):
                    return self._send(202, dict(queued=service.name_backlog(0), names=service.names))
                sid = body.get("id")
                if not isinstance(sid, str) or not re.match(r"^[0-9a-f-]{36}$", sid):
                    raise ValueError("id must be a session id, or send {\"all\": true}")
                if body.get("after_exit"):
                    service.name_when_ended(sid)
                    return self._send(202, dict(queued=1))
                return self._send(200, service.name_one(sid))
            if method == "POST" and path == "/api/resume":
                body = self._body()
                ok, message, extra = resume_chat(service, body.get("id"), bool(body.get("remote", True)))
                entry = service.store.log_action("resume", extra.get("pid") or 0, dict(session_id=body.get("id"), name=extra.get("title")),
                                                 ok, message, self._who())
                with service.fleet_lock:
                    if service.fleet:
                        service.fleet.last["at"] = 0
                return self._send(200 if ok else 409, dict(entry, **extra, error=None if ok else message))
            if method == "POST" and path in ("/api/kill", "/api/send"):
                if service.fleet is None:
                    return self._send(503, {"error": "process scanning is off"})
                body = self._body()
                pid, created = _pid(body)
                if path == "/api/kill":
                    ok, message, row = service.fleet.kill(pid, created)
                    action = "kill"
                else:
                    text = body.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("text is required")
                    ok, message, row = service.fleet.send(pid, created, text)
                    action = "send"
                    message = f"{message}: {text.strip()[:200]}" if ok else message
                entry = service.store.log_action(action, pid, row, ok, message, self._who())
                with service.fleet_lock:
                    service.fleet.last["at"] = 0        # the next look rescans
                return self._send(200 if ok else 409, dict(entry, error=None if ok else message))
            return self._send(405, {"error": "Method not allowed"})

        def _handle(self):
            try:
                self._dispatch()
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc) if isinstance(exc, ValueError) and str(exc) else "Invalid request"})
            except Exception as exc:
                self._fail(exc)

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _handle

    return web.protect(Handler, token=token, hosts=hosts, timeout=60)


def make_door(service, port, token, hosts=()):
    from almanac import web
    return web.serve(make_handler(service, token, hosts=hosts), port)
