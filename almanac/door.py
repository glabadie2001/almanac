"""Almanac's HTTP routes, behind its own local door (`almanac.web`): cross-model mission control for Claude and
Gemini agent sessions on this PC, unified `uds-messaging` orchestration, plus the efficiency report and probes.

GET  /api/fleet              every Claude/Gemini session, classified, joined to its session record (cached ~1.5 s)
GET  /api/session?pid=N      one process with its transcript tail, model/effort and token totals (`kinds=` filters
                             the feed by event kind before `limit` cuts it)
GET  /api/sessions?pids=a,b  the same for many at once (the gallery), `limit` events each, `kinds=` likewise
GET  /api/mesh               live cross-model peer roster, contact stream messages, squads, and roles
GET  /api/mesh/messages      contact stream messages (`peer`, `channel`, `squad`, `since_id`, `limit`)
GET  /api/mesh/wait          long-poll wakeup for incoming contact stream messages
GET  /api/mesh/context       normalized context stream of any peer (`peer`, `limit`, `kinds`)
GET  /api/mesh/graph?squad=  topology: the squad's agents (ended ones too), operator and channels; message edges with
                             counts and open flags
POST /api/mesh/send          deliver over Claude's `uds-messaging` protocol (direct or `@squad`/`@all`/`#channel`);
                             `needs` reply|blocking flags it open until answered; an agent reaches `operator` only
                             if its role may page the operator, or to answer the operator's last message
GET  /api/worktime           per agent: work / wait / idle seconds and per-bucket heat, plus how the agents overlapped
                             (`squad=` or `sessions=a,b,c`, optional `bucket=` seconds); `messages` and `marks`
                             (errors, edits) for the swimlane
GET  /api/health             runners found or missing (and where Almanac looked), hub up, last ingest error
GET  /api/operator/inbox     messages to and from the operator, unread and open-flag counts (`limit=0`: counts only)
POST /api/operator/read      {ids:[..]} or {upto:id}: mark messages to the operator read
POST /api/operator/resolve   {id}: close an open flagged message by hand
GET  /api/roles              list agent role definitions (POST upserts, DELETE `?id=` deletes)
POST /api/spawn              launch a Claude or Gemini agent with role, workspace mode, and mesh wiring
GET  /api/squads             list multi-agent squads (POST launches one; `spawn_role` + `spawn_limit` let its
                             architect add members later)
POST /api/squad              {id, spawn_role?, spawn_limit?}: what agents may add to a running squad (operator only)
POST /api/squad/member       {squad_id, role_id, agent_name?, task?, model?, ...}: the operator adds a member (no limit)
POST /api/mesh/spawn         {task, name?, role?}: an agent whose role may_spawn adds one member of the squad's
                             spawn_role (or of `role` when spawn_role is `*`), within spawn_limit (403 or 400 with
                             the reason otherwise)
GET  /api/models             known & CLI-discovered Claude and Gemini model identifiers
POST /api/model {pid, created, model}  set or switch the model on a live Claude or Gemini session
POST /api/kill  {pid, created}         end the process tree
POST /api/send  {pid, created, text}   send via `uds-messaging` when available, else console keystrokes
GET  /api/actions            the kill/send/spawn/squad log
GET  /api/report?days=&bucket=&provider=  the efficiency report (provider: claude | gemini; omit for all)
POST /api/ingest             read the transcripts now

Every route but /ident and /healthz needs the door token (bearer) or the signed-in browser cookie. An agent's own
token opens /api/mesh and below only, so /api/operator stays the operator's.
"""
import json
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
LIVE_INTERVAL = 30       # seconds between transcript reads while a spawned agent runs
RUNNER_TTL = 10          # seconds a runner probe is trusted; then it looks again
_CLAUDE_BIN_CACHE: dict[tuple[str, int], list[str]] = {}
_CLAUDE_BIN_MODEL_RE = re.compile(rb"\bclaude-(?:fable|mythos|opus|sonnet|haiku)-\d+(?:-\d+)?\b")


def _discover_claude_bin_models(claude_bin_str: str | None) -> list[str]:
    """Extract canonical `claude-<family>-<ver>` model IDs from `claude.exe` once per binary mtime."""
    if not claude_bin_str:
        return []
    p = Path(claude_bin_str).expanduser()
    try:
        if not p.is_file():
            return []
        st = p.stat()
        if st.st_size > 400_000_000:
            return []
        key = (str(p.resolve()), st.st_mtime_ns)
        cached = _CLAUDE_BIN_CACHE.get(key)
        if cached is not None:
            return cached
        found = sorted({m.decode("ascii") for m in _CLAUDE_BIN_MODEL_RE.findall(p.read_bytes())}, reverse=True)
        clean = [m for m in found if not m.endswith("-3-55")]
        _CLAUDE_BIN_CACHE[key] = clean
        return clean
    except OSError:
        return []


class Service:
    """The store, the fleet scanner, the uds-messaging hub, and the background ingest loop."""

    def __init__(self, store, transcripts, interval=300, fleet=None, state=None, claude="claude", gemini="agy",
                 prices=None, namer=None, probe=None):
        self.store, self.transcripts, self.interval = store, Path(transcripts), interval
        self.fleet = fleet
        # absolute: paths built from it are handed to an agent that runs in the chat's folder, not ours
        self.state = Path(state).resolve() if state else None
        self.archive_dir = self.state / "archive" / "projects" if self.state else None
        self.gemini_transcripts = self.state / "gemini" / "projects" if self.state else None
        if self.fleet is not None and self.gemini_transcripts is not None and self.gemini_transcripts not in self.fleet.extra_projects:
            self.fleet.extra_projects.append(self.gemini_transcripts)
        # the configured runners; `claude` / `gemini` resolve them on use (`runners`), so an install made while
        # Almanac runs is found on the next spawn. `probe` passes home/env/which to the finders (tests).
        self.claude_configured, self.gemini_configured, self.prices = claude, gemini, prices
        self.probe = dict(probe or {})
        self._runners: tuple[float, dict] | None = None
        self._runners_lock = threading.Lock()
        self.launched: dict[str, int] = {}          # session id -> pid of a session Almanac started
        self.model_overrides: dict[str, dict] = {}  # "pid:created" -> {"model": str, "effort": str|None, "ts": float}
        self._models_cache: tuple[float, dict] | None = None
        self.hub_peer = None
        self.namer = namer
        self.naming = threading.Lock()              # one naming run at a time
        self.names: dict = dict(running=False, done=0, failed=0, usd=0.0, last=None)
        self.last_archive: dict = {}
        self.stop = threading.Event()
        self.wake = threading.Event()               # set by a spawn: read again now, then on the short interval
        self.ingesting = threading.Lock()
        self.fleet_lock = threading.Lock()
        self.spawn_lock = threading.Lock()            # an agent's limit check and its spawn are one step
        self.error = ""

    def runners(self, fresh: bool = False) -> dict:
        """{claude|gemini: {path, ok, searched}}, probed at most every RUNNER_TTL seconds. `path` is the configured
        value when nothing was found, so callers that only need a string keep working."""
        from almanac import config
        with self._runners_lock:
            if not fresh and self._runners and time.monotonic() - self._runners[0] < RUNNER_TTL:
                return self._runners[1]
            doc = {}
            shims: list[str] = []
            found, searched = config.find_claude(self.claude_configured, shims=shims, **self.probe)
            doc["claude"] = dict(path=found or str(self.claude_configured or ""), ok=bool(found), searched=searched,
                                 reason=None if found or not shims else
                                 f"found {shims[0]}, a .cmd shim: Almanac will not launch it because cmd.exe would "
                                 "re-parse prompts; point \"claude\" at claude.exe")
            found, searched = config.find_gemini(self.gemini_configured, **self.probe)
            doc["gemini"] = dict(path=found or str(self.gemini_configured or ""), ok=bool(found), searched=searched,
                                 reason=None)
            self._runners = (time.monotonic(), doc)
            return doc

    @property
    def claude(self) -> str:
        return self.runners()["claude"]["path"]

    @property
    def gemini(self) -> str:
        return self.runners()["gemini"]["path"]

    def health(self) -> dict:
        last = self.store.last_ingest or {}
        return dict(runners=self.runners(), hub=self.hub_peer is not None,
                    ingest_error=self.error or last.get("error") or None)

    def ingest(self):
        if not self.ingesting.acquire(blocking=False):
            return dict(busy=True)
        try:
            result = self.store.ingest(self.transcripts, provider="claude")
            self.error = result.get("error", "")
            if self.gemini_transcripts is not None and self.gemini_transcripts.is_dir():
                g_res = self.store.ingest(self.gemini_transcripts, provider="gemini")
                for k in ("files", "read", "calls", "results", "messages", "bytes"):
                    result[k] = int(result.get(k) or 0) + int(g_res.get(k) or 0)
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

    def _enrich_process_models(self, procs: list[dict], *, prune: bool = False) -> None:
        """Fill model, effort, role and squad on process rows from the overrides and spawn records. `prune` drops
        the overrides of processes not in `procs`, so only a caller holding the whole fleet may pass it: a
        single-row caller (/api/session, /api/sessions) would wipe every other live session's override."""
        if not procs:
            return
        spawned_rows = self.store.spawned(limit=200)
        by_sid = {s["session_id"]: s for s in spawned_rows if s.get("session_id")}
        by_name = {s["agent_name"]: s for s in reversed(spawned_rows) if s.get("agent_name")}   # newest wins
        if prune:
            live_keys = {f"{p['pid']}:{p['created']}" for p in procs}
            for k in list(self.model_overrides):
                if k not in live_keys:
                    self.model_overrides.pop(k, None)
        for p in procs:
            if p.get("role") != "session":
                continue
            sp = by_sid.get(p.get("session_id") or "") or by_name.get(p.get("name") or "") or {}
            ov = self.model_overrides.get(f"{p['pid']}:{p['created']}")
            if ov:
                if ov.get("model"):
                    p["model"] = ov["model"]
                if ov.get("effort"):
                    p["effort"] = ov["effort"]
            else:
                if not p.get("model") and sp.get("model"):
                    p["model"] = sp["model"]
                if not p.get("effort") and sp.get("effort"):
                    p["effort"] = sp["effort"]
            if not p.get("agent_role") and sp.get("role_id"):
                p["agent_role"] = sp["role_id"]
            if not p.get("squad_id") and sp.get("squad_id"):
                p["squad_id"] = sp["squad_id"]
            if not p.get("worktree_path") and sp.get("worktree_path"):
                p["worktree_path"] = sp["worktree_path"]
            if p.get("worktree_path") and "worktree_of" not in p:
                # the repo this worktree belongs to, read once from its `.git` file ("gitdir: <repo>/.git/worktrees/<name>")
                wt = p["worktree_path"]
                cache = self.__dict__.setdefault("_worktree_of", {})
                if wt not in cache:
                    repo = None
                    try:
                        line = (Path(wt) / ".git").read_text(encoding="utf-8", errors="replace").strip()
                        if line.startswith("gitdir:"):
                            gitdir = Path(line[len("gitdir:"):].strip())
                            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                                repo = str(gitdir.parent.parent.parent)
                    except OSError:
                        pass
                    cache[wt] = repo
                p["worktree_of"] = cache[wt]

    def fleet_now(self):
        if self.fleet is None:
            return dict(at=time.time(), processes=[], summary={}, error="process scanning is off")
        with self.fleet_lock:
            if time.time() - self.fleet.last.get("at", 0) > FLEET_TTL:
                self.fleet.snapshot()
            self._enrich_process_models(self.fleet.last.get("processes", []), prune=True)
            return self.fleet.last

    def available_models(self) -> dict:
        now = time.time()
        if self._models_cache and now - self._models_cache[0] < 600:
            return self._models_cache[1]
        claude_list = [
            "fable",
            "mythos",
            "opus",
            "sonnet",
            "haiku",
            "opusplan",
            "claude-fable-5-2",
            "claude-fable-5-1",
            "fable-5-2",
            "fable-5-1",
            "claude-mythos-5-2",
            "claude-mythos-5-1",
            "mythos-5-2",
            "mythos-5-1",
            "claude-opus-5-5",
            "opus-5-5",
            "claude-sonnet-5-5",
            "sonnet-5-5",
            "claude-haiku-5-5",
            "haiku-5-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ]
        for bin_m in _discover_claude_bin_models(self.claude):
            if bin_m not in claude_list:
                claude_list.append(bin_m)
        from almanac import spawner
        gemini_list: list[str] = []
        gemini_efforts: dict[str, list[str]] = {"*": ["low", "medium", "high"]}
        raw_gemini_ids: list[str] = []
        gem_bin = Path(self.gemini).expanduser() if self.gemini else None
        if gem_bin and gem_bin.is_file():
            import subprocess as _sp
            try:
                proc = _sp.run(
                    [str(gem_bin), "models"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode == 0:
                    for raw_line in (proc.stdout or "").splitlines():
                        line = raw_line.strip()
                        if not line or line.lower().startswith("fetching"):
                            continue
                        mid = line.split()[0].strip()
                        if mid:
                            raw_gemini_ids.append(mid)
            except Exception:
                pass
        raw_gemini_ids.extend([
            "gemini-4-carbon-high",
            "gemini-4-carbon-medium",
            "gemini-4-carbon-low",
            "gemini-4-argon-high",
            "gemini-4-argon-medium",
            "gemini-4-argon-low",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low",
            "claude-opus-5-5-high",
            "claude-opus-5-5-medium",
            "claude-opus-5-5-low",
            "claude-sonnet-5-5-high",
            "claude-sonnet-5-5-medium",
            "claude-sonnet-5-5-low",
            "gpt-oss-120b-medium",
            "gemini-2.5-pro",
        ])
        for mid in raw_gemini_ids:
            base, eff = spawner.split_model_effort("gemini", mid)
            if not base:
                continue
            if base not in gemini_list:
                gemini_list.append(base)
            if eff:
                bucket = gemini_efforts.setdefault(base, [])
                if eff not in bucket:
                    bucket.append(eff)
        order_idx = {e: i for i, e in enumerate(spawner.EFFORT_LEVELS)}
        for base, bucket in list(gemini_efforts.items()):
            gemini_efforts[base] = sorted(bucket, key=lambda x: order_idx.get(x, 99))
        known = self.store.known_models()
        for m in known.get("claude", []):
            base_c, _ = spawner.split_model_effort("claude", m)
            if base_c and base_c not in claude_list:
                claude_list.append(base_c)
        for m in known.get("gemini", []):
            base_g, _ = spawner.split_model_effort("gemini", m)
            if base_g and base_g not in gemini_list:
                gemini_list.append(base_g)
        all_models: list[str] = []
        for m in [*claude_list, *gemini_list]:
            if m not in all_models:
                all_models.append(m)
        efforts_doc = dict(
            claude={"*": ["low", "medium", "high", "xhigh", "max"]},
            gemini=gemini_efforts,
        )
        doc = dict(claude=claude_list, gemini=gemini_list, all=all_models, efforts=efforts_doc)
        self._models_cache = (now, doc)
        return doc

    def set_session_model(
        self,
        pid: int,
        created: str,
        raw_model: str,
        *,
        effort: str | None = None,
        write_fn=None,
    ) -> tuple[bool, str, dict | None, str | None, str | None]:
        """Set or switch the model and/or effort on a live Fleet session."""
        from almanac import console, spawner, uds
        if self.fleet is None:
            return False, "process scanning is off", None, None, None
        doc = self.fleet.snapshot()
        self._enrich_process_models(doc.get("processes", []), prune=True)
        row = next((p for p in doc.get("processes", []) if p["pid"] == pid and p["created"] == str(created)), None)
        if row is None:
            return False, f"process {pid} is no longer running (or PID was reused)", None, None, None
        if row.get("role") != "session":
            return False, f"pid {pid} is a {row.get('role')} process, not a session", row, None, None

        prov = (row.get("provider") or "claude").lower()
        req_model_str = str(raw_model or "").strip()
        req_effort = spawner.normalize_effort(effort)
        if not req_model_str and not req_effort:
            raise ValueError("model or effort is required")
        norm_model, embedded_eff = spawner.split_model_effort(
            prov,
            req_model_str or row.get("model") or "",
            req_effort,
        )
        eff = req_effort or embedded_eff or row.get("effort")
        if prov == "gemini" and norm_model and eff:
            from almanac import gemini_host
            _, eff = gemini_host.resolve_agy_wire_model(norm_model, eff)
        model_changed = bool(req_model_str) and norm_model != row.get("model")
        effort_changed = bool(req_effort or embedded_eff) and eff != row.get("effort")
        sid = row.get("session_id")
        transport_note = "recorded"

        if prov == "gemini" and row.get("has_uds"):
            sessions_dir = self.fleet.home / "sessions"
            try:
                uds.send_peer(
                    sessions_dir,
                    str(pid),
                    json.dumps({"op": "set_model", "model": norm_model, "effort": eff}),
                    from_name="almanac-hub",
                    from_pipe=self.hub_peer.pipe_path if self.hub_peer else "almanac-hub",
                    channel="__almanac_control__",
                    alive_fn=lambda _: True,
                    write_fn=write_fn,
                )
                transport_note = "gemini uds control"
            except Exception as exc:
                return False, f"gemini uds control failed: {exc}", row, norm_model, eff
        elif prov == "claude":
            hprocs = self.__dict__.get("headless_procs") or {}
            hproc = hprocs.get(sid) if sid else None
            if hproc is not None and hproc.poll() is None and getattr(hproc, "stdin", None) is not None:
                if norm_model and (model_changed or req_model_str):
                    try:
                        hproc.stdin.write(
                            json.dumps(
                                {
                                    "type": "control_request",
                                    "request_id": f"set_model_{int(time.time() * 1000)}",
                                    "request": {"subtype": "set_model", "model": norm_model},
                                }
                            )
                            + "\n"
                        )
                        hproc.stdin.flush()
                        transport_note = "claude stream-json control"
                    except OSError:
                        transport_note = "recorded"
            else:
                notes: list[str] = []
                if norm_model and (model_changed or req_model_str):
                    try:
                        ok_con, _ = console.send(pid, f"/model {norm_model}")
                        if ok_con:
                            notes.append("/model")
                    except Exception:
                        pass
                if eff and (effort_changed or (req_effort and not req_model_str)):
                    try:
                        ok_eff, _ = console.send(pid, f"/effort {eff}")
                        if ok_eff:
                            notes.append("/effort")
                    except Exception:
                        pass
                if notes:
                    transport_note = f"claude {'+'.join(notes)} console"

        key = f"{pid}:{created}"
        self.model_overrides[key] = dict(model=norm_model or row.get("model"), effort=eff, ts=time.time())
        if norm_model:
            row["model"] = norm_model
        if eff:
            row["effort"] = eff
        self.store.update_spawn_model(session_id=sid, agent_name=row.get("name"), model=norm_model, effort=eff)
        parts = []
        if norm_model and (req_model_str or not req_effort):
            parts.append(f"model -> {norm_model}")
        if eff and (req_effort or embedded_eff):
            parts.append(f"effort -> {eff}")
        summary_msg = f"{', '.join(parts) or f'model -> {norm_model}'} ({transport_note})"
        return True, summary_msg, row, norm_model, eff

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
        from almanac import recover
        while not self.stop.is_set():
            self.ingest()
            recover.tick(self)                            # stalled squad agents: auto-continue, then escalate
            if self.namer is not None:
                self.name_backlog(quiet_minutes=10)       # anything the hook missed (crash, kill, older Claude)
            self.wake.wait(min(LIVE_INTERVAL, self.interval) if self.agents_live() else self.interval)
            self.wake.clear()

    def agents_live(self) -> bool:
        """An agent Almanac spawned is running. Messages that bypass the hub (native SendMessage) only reach the
        contact stream through its transcript, so while a squad works they should not wait a full interval."""
        if self.fleet is None:
            return False
        procs = self.fleet_now().get("processes", [])
        live, pids = {p.get("session_id") for p in procs if p.get("session_id")}, {p["pid"] for p in procs}
        # a session Almanac just launched counts while its process lives: Claude Code names it a minute later
        return (any(s["session_id"] in live for s in self.store.spawned(limit=200))
                or any(pid in pids for pid in self.launched.values()))

    def _on_hub_message(self, msg: dict) -> None:
        """Record any `<cross-session-message>` sent directly to `almanac-hub` by a Claude or Gemini peer: the
        sender's words (the `[needs: ...]` line the PeerServer keeps on `body` comes off) with its flag."""
        from almanac import uds
        body = msg.get("body") or ""
        if msg.get("needs"):
            _, body = uds.split_needs(body)          # the same rule the transcript read applies
        sender = msg.get("from_name") or "peer"
        spawn = self.store.spawn_of(sender) or {}
        self.store.record_message(
            msg_id=msg.get("msg_id"),
            ts=msg.get("ts"),
            from_name=sender,
            to_name="almanac-hub",
            channel=msg.get("channel"),
            squad_id=spawn.get("squad_id"),
            body=body,
            transport="uds",
            needs=msg.get("needs"),
        )

    def start(self, *, enable_uds_hub: bool = True, in_memory_uds: bool = False):
        if enable_uds_hub and self.fleet is not None and self.hub_peer is None:
            from almanac import uds
            inbox = (self.state / "inboxes" / "almanac-hub.jsonl") if self.state else None
            self.hub_peer = uds.PeerServer(
                self.fleet.home / "sessions",
                name="almanac-hub",
                provider="almanac",
                inbox_path=inbox,
                on_message=self._on_hub_message,
                in_memory=in_memory_uds,
            ).start()
        threading.Thread(target=self.loop, name="almanac-ingest", daemon=True).start()

    def close_hub(self) -> None:
        if self.hub_peer is not None:
            try:
                self.hub_peer.close()
            finally:
                self.hub_peer = None
        for proc in list((self.__dict__.get("headless_procs") or {}).values()):
            try:
                if getattr(proc, "stdin", None) is not None:
                    proc.stdin.close()
            except Exception:
                pass

    # -------------------------------------------------------------- mesh orchestration
    def mesh_peers(self, squad_id: str | None = None) -> list[dict]:
        """Unified live roster across Claude and Gemini sessions, enriched with spawn/role metadata and last activity."""
        from almanac import uds
        doc = self.fleet_now()
        spawn_by_sid = {s["session_id"]: s for s in self.store.spawned(limit=200) if s.get("session_id")}
        spawn_by_name = {s["agent_name"]: s for s in reversed(self.store.spawned(limit=200)) if s.get("agent_name")}
        sessions_dir = (self.fleet.home / "sessions") if self.fleet else None
        uds_by_pid = {p["pid"]: p for p in uds.discover_peers(sessions_dir, alive_fn=lambda _: True)} if sessions_dir else {}
        titles = self.store.titles([p["session_id"] for p in doc.get("processes", []) if p.get("session_id")])

        peers: list[dict] = []
        for p in doc.get("processes", []):
            if p.get("role") != "session":
                continue
            sp = spawn_by_sid.get(p.get("session_id") or "") or spawn_by_name.get(p.get("name") or "") or {}
            urec = uds_by_pid.get(p["pid"]) or {}
            eff_squad = p.get("squad_id") or sp.get("squad_id") or urec.get("squad_id")
            if squad_id and eff_squad != squad_id:
                continue
            last_ev = None
            if self.fleet is not None and p.get("session_id"):
                _, events, _ = self.fleet.view(p["session_id"])
                if events:
                    last_ev = dict(events[-1], text=events[-1]["text"][:200])     # a roster line, not the reply
            peers.append(
                dict(
                    pid=p["pid"],
                    created=p["created"],
                    session_id=p.get("session_id"),
                    name=p.get("name") or sp.get("agent_name") or titles.get(p.get("session_id")) or f"pid-{p['pid']}",
                    title=titles.get(p.get("session_id")),
                    provider=p.get("provider") or sp.get("provider") or "claude",
                    model=p.get("model") or sp.get("model"),
                    effort=p.get("effort") or sp.get("effort"),
                    agent_role=p.get("agent_role") or sp.get("role_id"),
                    squad_id=eff_squad,
                    channels=urec.get("channels") or sp.get("channels") or [],
                    status=p.get("status") or "idle",
                    cwd=p.get("cwd") or sp.get("cwd"),
                    worktree_path=sp.get("worktree_path"),
                    spawned_by=sp.get("spawned_by"),
                    mode_note=urec.get("mode_note"),       # e.g. agy fell back from `--mode auto`
                    has_uds=bool(p.get("has_uds")),
                    pipe=p.get("pipe"),
                    cpu=p.get("cpu"),
                    mem_mb=p.get("mem_mb"),
                    started=p.get("started"),
                    last_event=last_ev,
                    stall=p.get("stall"),
                )
            )
        return peers

    def mesh_context(self, peer: str, limit: int = 30, kinds: list[str] | None = None) -> dict | None:
        """Read the normalized Context Stream of any peer (live or recently ended) by name, session_id, or pid."""
        lim = max(1, min(200, int(limit)))
        kind_set = {k.strip() for k in (kinds or []) if k and k.strip()}
        if self.fleet is not None:
            row = self.fleet.resolve_session(peer)
            if row is not None:
                # resolve_session is not enriched: take role and squad from the spawn row, as the roster does
                # (kept off _enrich_process_models: this needs only role and squad, not the model overrides)
                spawns = self.store.spawned(limit=500)
                sp = next((s for s in spawns if row.get("session_id") and s.get("session_id") == row["session_id"]), None) \
                    or next((s for s in spawns if s["agent_name"] == row.get("name")), {})
                row = dict(row, agent_role=row.get("agent_role") or sp.get("role_id"),
                           squad_id=row.get("squad_id") or sp.get("squad_id"))
                det = self.fleet.detail(row["pid"], limit=FEED_WINDOW if kind_set else lim)
                if det is not None:
                    evs = [e for e in det["events"] if e["kind"] in kind_set][-lim:] if kind_set else det["events"][-lim:]
                    sid = row.get("session_id")
                    return dict(
                        live=True,
                        peer=dict(
                            name=row.get("name") or f"pid-{row['pid']}",
                            pid=row["pid"],
                            session_id=sid,
                            provider=row.get("provider") or "claude",
                            model=det["meta"].get("model") or row.get("model"),
                            effort=det["meta"].get("effort") or row.get("effort"),
                            agent_role=row.get("agent_role"),
                            squad_id=row.get("squad_id"),
                            status=row.get("status"),
                            cwd=row.get("cwd"),
                        ),
                        events=evs,
                        meta=det["meta"],
                        usage=self.store.session_usage(sid) if sid else None,
                    )
        # Fallback: check spawned_agents by name -> session_id, or ended chats by session_id
        sp = next((s for s in self.store.spawned(limit=200) if s["agent_name"] == peer or s.get("session_id") == peer), None)
        sid = (sp or {}).get("session_id") or peer
        cdet = chat_detail(self, sid, limit=max(lim * 3, 60) if kind_set else lim)
        if cdet is not None:
            evs = [e for e in cdet["events"] if e["kind"] in kind_set][-lim:] if kind_set else cdet["events"][-lim:]
            c = cdet["chat"]
            return dict(
                live=False,
                peer=dict(
                    name=(sp or {}).get("agent_name") or c.get("title") or sid[:8],
                    pid=None,
                    session_id=sid,
                    provider=(sp or {}).get("provider") or c.get("provider") or "claude",
                    model=c.get("model"),
                    effort=c.get("effort"),
                    agent_role=(sp or {}).get("role_id"),
                    squad_id=(sp or {}).get("squad_id"),
                    status="ended",
                    cwd=c.get("cwd"),
                ),
                events=evs,
                meta=dict(model=c.get("model"), effort=c.get("effort"), last_ts=c.get("last")),
                usage=self.store.session_usage(sid),
            )
        return None

    def mesh_send(
        self,
        *,
        to: str,
        text: str,
        from_name: str = "operator",
        channel: str | None = None,
        squad_id: str | None = None,
        needs: str | None = None,
        write_fn=None,
        exclude=(),
    ) -> dict:
        """Deliver a message over Claude's `uds-messaging` protocol (with broadcast fan-out and SQLite journaling).
        `needs` ('reply' or 'blocking') flags it as wanting an answer: the delivered text opens with a marker line,
        the journal keeps the sender's words and the flag, and the flag stays open until the recipient answers."""
        from almanac import uds
        from almanac.store import NEEDS
        target = str(to or "").strip()
        body = str(text or "").strip()
        if not channel:                                # a channel line typed into the body is the channel
            channel, body = uds.split_channel(body)
        typed, body = uds.split_needs(body)             # a marker typed into the body is the flag
        body = body.strip()
        if not target or not body:
            raise ValueError("'to' and 'text' are required")
        needs = str(needs).strip().lower() if needs else typed   # an explicit needs wins
        if needs not in NEEDS:
            raise ValueError("needs must be 'reply', 'blocking' or empty")
        sender = str(from_name or "operator").strip()
        wire = uds.needs_marker(needs, sender) + body       # what the recipient sees; the journal keeps `body`
        hub_pipe = self.hub_peer.pipe_path if self.hub_peer else None

        # Lookup sender metadata if sender is a known peer
        all_peers = self.mesh_peers()
        sender_peer = next((p for p in all_peers if p["name"] == sender or p.get("session_id") == sender), None)
        # a sender that is not a live session is still known by its newest spawn record
        known = sender_peer or self.store.spawn_of(sender) or {}
        eff_squad = squad_id or known.get("squad_id")
        if sender == "operator" and target in ("@squad", "#squad") and not eff_squad:
            live = {p["squad_id"] for p in all_peers if p.get("squad_id")}
            eff_squad = live.pop() if len(live) == 1 else None     # one live squad is the only reading of either

        if target in ("operator", "almanac-hub", "hub"):
            entry = self.store.record_message(
                from_name=sender,
                from_session=(sender_peer or {}).get("session_id"),
                from_provider=(sender_peer or {}).get("provider"),
                to_name="operator",
                channel=channel,
                squad_id=eff_squad,
                body=body,
                transport="hub",
                needs=needs,
            )
            return dict(ok=True, delivered=["operator"], failed=[], message=entry)

        # Broadcast targets: @all, @squad, or #channel
        if target in ("@all", "@squad") or target.startswith("#"):
            eff_channel = channel or (target if target.startswith("#") else "#squad" if target == "@squad" else None)
            if target in ("@squad", "#squad") and not eff_squad:          # #squad and @squad mean one thing
                raise ValueError(f"{target} needs a squad: the sender is not in one, no squad_id was given, "
                                 "and there is not exactly one live squad")
            # Only @all reaches everyone. A squad post stays in the squad; a channel post goes to its subscribers in
            # the sender's squad (an agent in no squad reaches only peers in none; the operator in none, any squad).
            # A peer in no channel hears no channel, except #squad, which every member of a squad hears.
            recipients = []
            for p in all_peers:
                if p["name"] == sender or (p.get("session_id") and p.get("session_id") == sender):
                    continue
                if p["name"] in exclude or (p.get("session_id") and p.get("session_id") in exclude):
                    continue                           # e.g. a newcomer is not told it joined (its kickoff says so)
                if target == "@squad" and p.get("squad_id") != eff_squad:
                    continue
                if target.startswith("#") and (
                    (target != "#squad" and target not in (p.get("channels") or []))   # #squad: the whole squad
                    or ((eff_squad or sender != "operator") and p.get("squad_id") != eff_squad)
                ):
                    continue
                recipients.append(p)
            delivered, failed = [], []
            for r in recipients:
                ok, msg_detail, _ = self.fleet.send(
                    r["pid"],
                    r["created"],
                    wire,
                    from_name=sender,
                    from_pipe=hub_pipe,
                    channel=eff_channel,
                    write_fn=write_fn,
                ) if self.fleet else (False, "fleet off", None)
                if ok:
                    delivered.append(r["name"])
                else:
                    failed.append(dict(name=r["name"], error=msg_detail))
            entry = self.store.record_message(
                from_name=sender,
                from_session=(sender_peer or {}).get("session_id"),
                from_provider=(sender_peer or {}).get("provider"),
                to_name=target,
                channel=eff_channel,
                squad_id=eff_squad,
                body=body,
                transport="uds-broadcast",
                needs=needs,
            )
            return dict(ok=True, delivered=delivered, failed=failed, message=entry)

        # Direct peer target: resolve in Fleet first, then directly in ~/.claude/sessions
        recipient = next(
            (p for p in all_peers if p["name"] == target or p.get("session_id") == target or str(p["pid"]) == target),
            None,
        )
        if recipient is not None and self.fleet is not None:
            ok, detail_msg, _ = self.fleet.send(
                recipient["pid"],
                recipient["created"],
                wire,
                from_name=sender,
                from_pipe=hub_pipe,
                channel=channel,
                write_fn=write_fn,
            )
            if not ok:
                return dict(ok=False, error=detail_msg, target=recipient["name"])
            entry = self.store.record_message(
                from_name=sender,
                from_session=(sender_peer or {}).get("session_id"),
                from_provider=(sender_peer or {}).get("provider"),
                to_name=recipient["name"],
                to_session=recipient.get("session_id"),
                to_provider=recipient.get("provider"),
                channel=channel,
                squad_id=eff_squad or recipient.get("squad_id"),
                body=body,
                transport="uds" if recipient.get("has_uds") else "console",
                needs=needs,
            )
            return dict(ok=True, delivered=[recipient["name"]], transport=entry["transport"], message=entry)

        # Direct `~/.claude/sessions` fallback (e.g. a peer registered in uds that is not in the process table mock)
        sessions_dir = (self.fleet.home / "sessions") if self.fleet else (Path.home() / ".claude" / "sessions")
        try:
            res = uds.send_peer(
                sessions_dir,
                target,
                wire,
                from_name=sender,
                from_pipe=hub_pipe,
                channel=channel,
                write_fn=write_fn,
            )
        except (LookupError, OSError, ValueError) as exc:
            return dict(ok=False, error=str(exc), target=target)
        entry = self.store.record_message(
            msg_id=res.get("msg_id"),
            from_name=sender,
            from_session=(sender_peer or {}).get("session_id"),
            from_provider=(sender_peer or {}).get("provider"),
            to_name=res["target_name"],
            to_session=res.get("target_session"),
            to_provider=res.get("target_provider"),
            channel=channel,
            squad_id=eff_squad,
            body=body,
            transport="uds",
            needs=needs,
        )
        return dict(ok=True, delivered=[res["target_name"]], transport="uds", message=entry)

    def may_spawn(self, agent_name: str, role_id: str | None = None) -> tuple[bool, str, dict | None, dict | None, int]:
        """(ok, reason, squad, role to add, http status): may this agent add a member to its squad through
        mesh_spawn? Its newest spawn row must name a squad that is active; its role must have may_spawn; the squad's
        spawn_role must resolve: a fixed role (an agent naming another is refused) or `*`, where the agent must name
        a defined role; slots must be left (spawn_used < spawn_limit, whatever roles were added); that role's runner
        must be found. Role mistakes are 400 (the request is wrong), the rest 403. The caller holds `spawn_lock`
        across this check and the spawn, so two calls cannot both take the last slot.
        Advisory, like agent tokens: an agent runs as this user and could reach the door another way."""
        from almanac.store import ANY_ROLE
        spawn = self.store.spawn_of(agent_name)
        if not spawn or not spawn.get("squad_id"):
            return False, f"{agent_name} is not in a squad", None, None, 403
        squad = self.store.squad(spawn["squad_id"])
        if squad is None:
            return False, f"squad {spawn['squad_id']} no longer exists", None, None, 403
        if squad.get("status") != "active":
            return False, f"squad {squad['id']} is {squad.get('status')}; nobody can join it", squad, None, 403
        mine = self.store.role(spawn["role_id"]) if spawn.get("role_id") else None
        if not mine or not mine.get("may_spawn"):
            return False, f"role {spawn.get('role_id') or '(none)'} may not add squad members", squad, None, 403
        allowed = squad.get("spawn_role")
        if not allowed:
            return False, f"the operator has not chosen a role agents may add to squad {squad['id']}", squad, None, 403
        wanted = str(role_id or "").strip() or None
        if allowed == ANY_ROLE:
            target = self.store.role(wanted) if wanted else None
            if target is None:
                ids = ", ".join(r["id"] for r in self.store.roles())
                what = f"no role {wanted!r}" if wanted else "name the role to add"
                return False, f"{what}: squad {squad['id']} may add any of {ids}", squad, None, 400
        else:
            if wanted and wanted != allowed:
                return False, f"squad {squad['id']} may add only {allowed}, not {wanted}", squad, None, 400
            target = self.store.role(allowed)
            if target is None:
                return False, f"squad {squad['id']}'s spawn role {allowed!r} no longer exists", squad, None, 403
        used, limit = squad.get("spawn_used") or 0, squad.get("spawn_limit") or 0
        if limit <= 0:
            return False, f"0/0: the operator allows no agent-added members in squad {squad['id']} (limit 0)", squad, target, 403
        if used >= limit:
            return False, f"{used}/{limit} used; ask the operator to raise the limit", squad, target, 403
        runner = self.runners().get(target["provider"]) or {}
        if not runner.get("ok"):
            return False, (f"{target['provider']} runner not found; looked in: {'; '.join(runner.get('searched') or [])}"), \
                squad, target, 403
        return True, f"{used}/{limit} used", squad, target, 200

    def may_page_operator(self, agent_name: str) -> tuple[bool, str]:
        """(allowed, hint): may this agent address the operator? Yes when its newest spawn has no role (or the role
        is gone), when its role has `may_page_operator`, or when the newest direct message between it and the
        operator came from the operator: answering is not paging, and it buys one reply per operator message.
        Advisory only: the hub's named pipe does not check senders, and an agent can read the door's secret."""
        from almanac.store import OPERATOR_NAMES
        spawn = self.store.spawn_of(agent_name)
        role = self.store.role(spawn["role_id"]) if spawn and spawn.get("role_id") else None
        if role is None or role.get("may_page_operator"):
            return True, ""
        squad = spawn.get("squad_id")
        last = self.store.last_operator_exchange(agent_name, squad)
        if last and last["from_name"] in OPERATOR_NAMES:
            return True, ""
        pagers = {r["id"] for r in self.store.roles() if r.get("may_page_operator")}
        relays = sorted(p["name"] for p in self.mesh_peers()
                        if p.get("name") != agent_name and squad and p.get("squad_id") == squad and p.get("agent_role") in pagers)
        via = (f"ask {' or '.join(relays)} to relay it" if relays
               else "no live squadmate's role may page the operator; post it to #squad")
        return False, (f"role {role['id']!r} does not page the operator: {via}. "
                       "You may always answer a message the operator sends you.")


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


def _transcript_source(service, meta: dict) -> Path:
    if meta.get("provider") == "gemini" and service.gemini_transcripts is not None:
        cand = service.gemini_transcripts / meta["project"] / f"{meta['id']}.jsonl"
        if cand.exists():
            return cand
    return service.transcripts / meta["project"] / f"{meta['id']}.jsonl"


def chats(service, query="", limit=300):
    from almanac import archive
    live = _live_sessions(service)
    out = []
    for c in service.store.chats(query, max(1, min(2000, limit))):
        archived = archive.find(c["id"], service.archive_dir) if service.archive_dir else None
        source = _transcript_source(service, c)
        c.update(live_pid=live.get(c["id"]), archived=bool(archived), on_disk=source.exists(),
                 resume_usd=service.prices.estimate(c["model"], c["context"]) if service.prices else None)
        c["resumable"] = (c.get("provider") != "gemini") and (c["archived"] or c["on_disk"]) and not c["live_pid"]
        out.append(c)
    return dict(chats=out, archive=service.last_archive)


def chat_detail(service, session_id, limit=300):
    from almanac import archive
    from almanac.fleet import read_tail
    meta = service.store.chat(session_id)
    if meta is None:
        return None
    source = _transcript_source(service, meta)
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
    from almanac import config
    shim = service.runners()["claude"].get("reason")
    if shim or config.is_shim(service.claude):          # cmd.exe would re-parse the title and settings path
        return False, shim or f"{service.claude} is a .cmd / .bat shim; point \"claude\" at claude.exe", {}
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


def worktime(service, query) -> dict | None:
    """GET /api/worktime: `squad=<id>` (its members, from its launch) or `sessions=a,b,c` (at most 20), optional
    `bucket=` seconds. Each agent is labelled from its spawn record and the fleet; a busy session's last turn is
    stretched to now, since ingest runs behind it. None for an unknown squad."""
    squad_id = (query.get("squad") or "").strip()
    start = None
    if squad_id:
        squad = service.store.squad(squad_id)
        if squad is None:
            return None
        spawns = squad["agents"]                        # in hand already, oldest first: no capped scan
        sessions = [s["session_id"] for s in spawns]
        start = squad["created_ts"]
    else:
        sessions = [s.strip() for s in query.get("sessions", "").split(",") if s.strip()]
        spawns = service.store.spawned(limit=1000)       # labels only
    sessions = list(dict.fromkeys(sessions))[:20]
    bucket = query.get("bucket")
    bucket = int(bucket) if bucket else None
    if bucket is not None and not 1 <= bucket <= 86400:
        raise ValueError("bucket is 1..86400 seconds")
    procs = {p["session_id"]: p for p in service.fleet_now().get("processes", []) if p.get("session_id")}
    live = {sid: pid for sid, pid in _live_sessions(service).items() if sid in sessions}
    now = time.time()
    busy = {sid: now for sid in live if (procs.get(sid) or {}).get("status") not in (None, "idle")}
    doc = service.store.worktime(sessions, start=start, end=now if live else None, bucket_s=bucket, open_until=busy)
    by_sid = {s["session_id"]: s for s in spawns}
    for a in doc["agents"]:
        sp = by_sid.get(a["session_id"]) or {}
        proc = procs.get(a["session_id"]) or {}
        a.update(agent_name=sp.get("agent_name") or proc.get("name") or a["session_id"][:8], role_id=sp.get("role_id"),
                 provider=sp.get("provider") or proc.get("provider") or "claude", model=sp.get("model") or proc.get("model"),
                 live=a["session_id"] in live, status=proc.get("status") if a["session_id"] in live else "ended")
    doc["squad_id"] = squad_id or None
    # the swimlane on the heatmap: messages between the agents, and each agent's tool errors and file edits
    rng = doc.get("range") or {}
    if rng.get("n"):
        doc["messages"] = service.store.timeline_messages(squad_id=squad_id or None, sessions=sessions,
                                                          start=rng["start"], end=rng["end"])
        doc["marks"] = service.store.timeline_marks(sessions, start=rng["start"], end=rng["end"])
    else:
        doc["messages"], doc["marks"] = [], []
    return doc


def role_class(role_id: str | None, title: str | None = "") -> str:
    """architect | implementer | reviewer | other, from the role's id and title: the topology's columns."""
    text = f"{role_id or ''} {title or ''}".lower()
    return next((c for c, word in (("architect", "architect"), ("implementer", "implement"), ("reviewer", "review"))
                 if word in text), "other")


def mesh_graph(service, squad_id: str | None) -> dict | None:
    """GET /api/mesh/graph: the topology. Nodes are the squad's agents in spawn order (one that exited stays, live
    false), the operator, and every channel or outside party its messages name; edges are message_graph's. With no
    squad, the live peers and the operator, and no edges. None for an unknown squad."""
    squad = service.store.squad(squad_id) if squad_id else None
    if squad_id and squad is None:
        return None
    peers = service.mesh_peers(squad_id=squad_id)
    live_by_sid = {p["session_id"]: p for p in peers if p.get("session_id")}
    titles = {r["id"]: r["title"] for r in service.store.roles()}
    newest: dict[str, dict] = {}
    for a in (squad or {}).get("agents", []):           # oldest first: the first spawn sets the order, the newest wins
        newest[a["agent_name"]] = a

    def node(name, *, kind="agent", sid=None, provider=None, rid=None, live=None, worktree=None, by=None):
        return dict(id=name, kind=kind, session_id=sid, provider=provider, role_id=rid,
                    role_class=role_class(rid, titles.get(rid)) if kind == "agent" else kind,
                    live=bool(live), status=(live or {}).get("status") or ("ended" if kind == "agent" else None),
                    worktree=worktree, last_event=(live or {}).get("last_event"), spawned_by=by)

    nodes = [node(n, sid=a["session_id"], provider=a["provider"], rid=a.get("role_id"), live=live_by_sid.get(a["session_id"]),
                  worktree=a.get("worktree_path"), by=a.get("spawned_by")) for n, a in newest.items()]
    known = {n["id"] for n in nodes}
    for p in peers:                                      # a live peer with no spawn row in this squad
        if p["name"] not in known:
            nodes.append(node(p["name"], sid=p.get("session_id"), provider=p.get("provider"), rid=p.get("agent_role"),
                              live=p, worktree=p.get("worktree_path")))
            known.add(p["name"])
    nodes.append(node("operator", kind="operator"))
    known.add("operator")
    edges = service.store.message_graph(squad_id) if squad_id else []
    for name in sorted({e["from"] for e in edges} | {e["to"] for e in edges}):
        if name not in known:
            nodes.append(node(name, kind="channel") if name[:1] in "@#" else node(name))
            known.add(name)
    return dict(squad_id=squad_id, nodes=nodes, edges=edges)


FEED_WINDOW = 100_000      # with a kind filter, filter everything in the transcript tail, then keep the newest `limit`


def _kinds(query) -> set[str] | None:
    """`kinds=you,claude,...` as a set; None when absent (every kind)."""
    found = {k.strip() for k in str(query.get("kinds", "")).split(",") if k.strip()}
    return found or None


def _cut_events(found: dict, limit: int, kinds: set[str] | None) -> None:
    events = found.get("events") or []
    if kinds:
        events = [e for e in events if e.get("kind") in kinds]
    found["events"] = events[-limit:]


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
            return self.agent or "operator"

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
                    provider = query.get("provider") or None
                    if provider not in ("claude", "gemini", None):
                        raise ValueError("provider is claude or gemini")
                    return self._send(200, service.store.report(days, query.get("bucket", "day"), provider=provider))
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
                    kinds = _kinds(query)
                    limit = max(1, min(400, int(query.get("limit", "40"))))
                    service.fleet_now()
                    found = service.fleet.detail(pid, FEED_WINDOW if kinds else limit) if service.fleet else None
                    if found is None:
                        return self._send(404, {"error": f"no Claude process {pid}"})
                    service._enrich_process_models([found["process"]])
                    ov = service.model_overrides.get(f"{found['process']['pid']}:{found['process']['created']}")
                    if ov and ov.get("model"):
                        found["meta"] = dict(found.get("meta") or {}, model=ov["model"], effort=ov.get("effort") or (found.get("meta") or {}).get("effort"))
                    sid = found["process"].get("session_id")
                    found["usage"] = service.store.session_usage(sid) if sid else None
                    _cut_events(found, limit, kinds)
                    return self._send(200, found)
                if path == "/api/sessions":
                    pids = [int(p) for p in query.get("pids", "").split(",") if p.strip()][:40]
                    limit = max(1, min(120, int(query.get("limit", "40"))))
                    kinds = _kinds(query)
                    service.fleet_now()
                    out = []
                    for pid in pids:
                        found = service.fleet.detail(pid, FEED_WINDOW if kinds else limit) if service.fleet else None
                        if found is None:
                            out.append(dict(pid=pid, gone=True))
                            continue
                        service._enrich_process_models([found["process"]])
                        ov = service.model_overrides.get(f"{found['process']['pid']}:{found['process']['created']}")
                        if ov and ov.get("model"):
                            found["meta"] = dict(found.get("meta") or {}, model=ov["model"], effort=ov.get("effort") or (found.get("meta") or {}).get("effort"))
                        sid = found["process"].get("session_id")
                        found["usage"] = service.store.session_usage(sid) if sid else None
                        _cut_events(found, limit, kinds)
                        # many tiles, polled every 2s: a tile shows the top of each event, the session view all of it
                        found["events"] = [dict(e, text=e["text"][:400]) for e in found["events"]]
                        out.append(found)
                    return self._send(200, dict(sessions=out))
                if path == "/api/models":
                    return self._send(200, service.available_models())
                if path == "/api/chats":
                    return self._send(200, chats(service, query.get("q", ""), int(query.get("limit", "300"))))
                if path == "/api/chat":
                    found = chat_detail(service, query.get("id", ""), int(query.get("limit", "300")))
                    if found is None:
                        return self._send(404, {"error": "no such chat"})
                    return self._send(200, found)
                if path == "/api/actions":
                    return self._send(200, dict(actions=service.store.actions(int(query.get("limit", "50")))))
                if path == "/api/roles":
                    return self._send(200, dict(roles=service.store.roles()))
                if path == "/api/squads":
                    return self._send(200, dict(squads=service.store.squads(int(query.get("limit", "50")))))
                if path == "/api/mesh":
                    squad_q = query.get("squad") or None
                    msg_limit = max(0, min(300, int(query.get("limit", "80"))))
                    return self._send(
                        200,
                        dict(
                            peers=service.mesh_peers(squad_id=squad_q),
                            messages=service.store.messages(squad_id=squad_q, limit=msg_limit) if msg_limit > 0 else [],
                            latest_id=service.store.latest_message_id(),
                            squads=service.store.squads(limit=20),
                            roles=service.store.roles(),
                            hub_pipe=service.hub_peer.pipe_path if service.hub_peer else None,
                            open=service.store.open_needs(squad_id=squad_q),
                        ),
                    )
                if path == "/api/health":
                    return self._send(200, service.health())
                if path == "/api/worktime":
                    doc = worktime(service, query)
                    return self._send(200 if doc is not None else 404, doc if doc is not None else {"error": "no such squad"})
                if path == "/api/operator/inbox":
                    return self._send(200, service.store.operator_inbox(
                        squad_id=query.get("squad") or None,
                        since_id=int(query.get("since_id", "0")),
                        limit=max(0, min(500, int(query.get("limit", "100"))))))
                if path == "/api/mesh/graph":
                    graph = mesh_graph(service, query.get("squad") or None)
                    return self._send(200 if graph is not None else 404, graph if graph is not None else {"error": "no such squad"})
                if path == "/api/mesh/messages":
                    msgs = service.store.messages(
                        peer=query.get("peer") or None,
                        channel=query.get("channel") or None,
                        squad_id=query.get("squad") or None,
                        since_id=int(query.get("since_id", "0")),
                        limit=int(query.get("limit", "100")),
                    )
                    return self._send(200, dict(messages=msgs, latest_id=service.store.latest_message_id()))
                if path == "/api/mesh/wait":
                    since_id = int(query.get("since_id", "0"))
                    timeout_s = max(0.0, min(300.0, float(query.get("timeout", "30"))))
                    msgs = service.store.wait_messages(
                        peer=query.get("peer") or None,
                        channel=query.get("channel") or None,
                        squad_id=query.get("squad") or None,
                        since_id=since_id,
                        timeout=timeout_s,
                        stop_event=service.stop,
                    )
                    return self._send(
                        200,
                        dict(messages=msgs, timed_out=len(msgs) == 0, latest_id=service.store.latest_message_id()),
                    )
                if path == "/api/mesh/context":
                    peer = query.get("peer", "").strip()
                    if not peer:
                        raise ValueError("peer query parameter is required")
                    kinds = [k.strip() for k in query.get("kinds", "").split(",") if k.strip()]
                    found = service.mesh_context(peer, int(query.get("limit", "30")), kinds or None)
                    if found is None:
                        return self._send(404, {"error": f"no live or archived session found for peer {peer!r}"})
                    return self._send(200, found)
                if self._page(path):
                    return None
                return self._send(404, {"error": "Not found"})
            if method == "DELETE" and path == "/api/roles":
                rid = query.get("id", "").strip()
                if not rid:
                    raise ValueError("id query parameter is required")
                deleted = service.store.delete_role(rid)
                return self._send(200 if deleted else 404, dict(ok=deleted, id=rid))
            if method == "POST" and path == "/api/roles":
                role_doc = service.store.upsert_role(self._body())
                return self._send(200, dict(ok=True, role=role_doc))
            if method == "POST" and path == "/api/mesh/send":
                body = self._body()
                target = str(body.get("to") or "").strip()
                if self.agent and target == "@all":
                    raise ValueError("@all is the operator's: an agent posts to a peer, @squad or a #channel")
                if self.agent and target in ("operator", "almanac-hub", "hub"):
                    allowed, hint = service.may_page_operator(self.agent)
                    if not allowed:
                        return self._send(403, dict(ok=False, error=hint))
                res = service.mesh_send(
                    to=target,
                    text=body.get("text") or body.get("message") or "",
                    from_name=self.agent or body.get("from_name") or "operator",   # an agent speaks only as itself
                    channel=body.get("channel"),
                    squad_id=None if self.agent else body.get("squad_id"),   # an agent's squad is its own
                    needs=body.get("needs"),
                )
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/mesh/spawn":
                # elastic squads: an agent adds one member of the role the operator chose, within the operator's limit;
                # everything but the task and the name comes from that role, never from the agent's body
                from almanac import spawner
                body = self._body()
                task = str(body.get("task") or "").strip()
                if len(task) < 20:
                    raise ValueError("task is required: at least 20 characters saying what the new member is to do")
                port = getattr(self.server, "server_port", 8812)
                overrides = {}
                with service.spawn_lock:
                    if self.agent:
                        ok, reason, squad, role, code = service.may_spawn(self.agent, body.get("role") or body.get("role_id"))
                        if not ok:
                            service.store.log_action("spawn", 0, dict(name=body.get("name")), False,
                                                     f"mesh_spawn refused: {reason}", self._who())
                            return self._send(code, dict(ok=False, error=reason))
                        squad_id, role_id, by = squad["id"], role["id"], self.agent
                    else:
                        squad_id, role_id, by = body.get("squad_id"), body.get("role_id") or body.get("role"), None
                        if not squad_id or not role_id:
                            raise ValueError("the operator names squad_id and role_id")
                        overrides = {k: body[k] for k in ("model", "effort", "workspace_mode", "permission_mode") if body.get(k)}
                    try:
                        res = spawner.join_squad(service, almanac_url=f"http://127.0.0.1:{port}", token=token,
                                                 squad_id=squad_id, role_id=role_id, task=task, agent_name=body.get("name"),
                                                 spawned_by=by, **overrides)
                    except (RuntimeError, OSError) as exc:
                        res = dict(ok=False, agent_name=body.get("name"), role_id=role_id, error=str(exc))
                service.store.log_action(
                    "spawn", res.get("pid") or 0, dict(session_id=res.get("session_id"), name=res.get("agent_name")),
                    bool(res.get("ok")),
                    f"{res.get('agent_name')} joined {squad_id} as {role_id} (mesh_spawn)" if res.get("ok") else str(res.get("error")),
                    self._who())
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/squad":
                body = self._body()
                sid = str(body.get("id") or body.get("squad_id") or "").strip()
                if not sid:
                    raise ValueError("id is required")
                doc = service.store.update_squad(sid, **{k: body[k] for k in ("spawn_role", "spawn_limit") if k in body})
                if doc is None:
                    return self._send(404, dict(ok=False, error=f"no squad {sid}"))
                service.store.log_action("squad", 0, dict(name=sid), True,
                                         f"agents may add {doc['spawn_role'] or 'nobody'} up to {doc['spawn_limit']} "
                                         f"({doc['spawn_used']} used)", self._who())
                return self._send(200, dict(ok=True, squad=doc))
            if method == "POST" and path == "/api/squad/member":
                from almanac import spawner
                body = self._body()
                port = getattr(self.server, "server_port", 8812)
                overrides = {k: body[k] for k in ("model", "effort", "workspace_mode", "permission_mode") if body.get(k)}
                # the same lock as mesh_spawn: an operator add and an agent add must not pick the same free handle
                with service.spawn_lock:
                    try:
                        res = spawner.join_squad(service, almanac_url=f"http://127.0.0.1:{port}", token=token,
                                                 squad_id=str(body.get("squad_id") or ""), role_id=str(body.get("role_id") or ""),
                                                 task=str(body.get("task") or ""), agent_name=body.get("agent_name") or body.get("name"),
                                                 spawned_by=None, **overrides)      # the operator's adds never use the limit
                    except (RuntimeError, OSError) as exc:
                        res = dict(ok=False, agent_name=body.get("agent_name"), role_id=body.get("role_id"), error=str(exc))
                service.store.log_action(
                    "spawn", res.get("pid") or 0, dict(session_id=res.get("session_id"), name=res.get("agent_name")),
                    bool(res.get("ok")),
                    f"{res.get('agent_name')} added to {body.get('squad_id')} as {body.get('role_id')}" if res.get("ok")
                    else str(res.get("error")), self._who())
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/operator/read":
                body = self._body()
                upto = body.get("upto")
                ids = body.get("ids")
                if upto is None and not isinstance(ids, list):
                    raise ValueError("send {\"ids\": [..]} or {\"upto\": id}")
                changed = service.store.mark_read(ids=ids, upto=int(upto) if upto is not None else None)
                return self._send(200, dict(service.store.operator_counts(), ok=True, marked=changed))
            if method == "POST" and path == "/api/operator/resolve":
                row_id = self._body().get("id")
                if not isinstance(row_id, int) or isinstance(row_id, bool):
                    raise ValueError("id must be a message id")
                if not service.store.resolve_message(row_id):
                    return self._send(404, dict(ok=False, error=f"no open flagged message {row_id}"))
                return self._send(200, dict(service.store.operator_counts(), ok=True, message=service.store.message(row_id)))
            if method == "POST" and path == "/api/spawn":
                from almanac import spawner
                body = self._body()
                port = getattr(self.server, "server_port", 8812)
                try:
                    res = spawner.spawn_agent(
                        service,
                        almanac_url=f"http://127.0.0.1:{port}",
                        token=token,
                        cwd=body.get("cwd") or "",
                        agent_name=body.get("agent_name") or body.get("name"),
                        role_id=body.get("role_id"),
                        provider=body.get("provider"),
                        model=body.get("model"),
                        effort=body.get("effort"),
                        workspace_mode=body.get("workspace_mode"),
                        permission_mode=body.get("permission_mode"),
                        system_prompt=body.get("system_prompt"),
                        initial_prompt=body.get("initial_prompt") or body.get("prompt"),
                        channels=body.get("channels"),
                        squad_id=body.get("squad_id"),
                        remote=bool(body.get("remote", False)),
                        headless=bool(body.get("headless", False)),
                    )
                except (RuntimeError, OSError) as exc:     # a worktree git refuses, a runner that will not start
                    res = dict(ok=False, agent_name=body.get("agent_name") or body.get("name"), provider=body.get("provider"),
                               role_id=body.get("role_id"), error=str(exc))
                service.store.log_action(
                    "spawn",
                    res.get("pid") or 0,
                    dict(session_id=res.get("session_id"), name=res.get("agent_name")),
                    bool(res.get("ok")),
                    f"{res.get('provider')} agent {res.get('agent_name')} ({res.get('role_id') or 'custom'})"
                    if res.get("ok")
                    else str(res.get("error")),
                    self._who(),
                )
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/squads":
                from almanac import spawner
                body = self._body()
                port = getattr(self.server, "server_port", 8812)
                res = spawner.launch_squad(
                    service,
                    almanac_url=f"http://127.0.0.1:{port}",
                    token=token,
                    name=body.get("name") or "squad",
                    cwd=body.get("cwd") or "",
                    brief=body.get("brief") or "",
                    members=body.get("members") or [],
                    spawn_role=body.get("spawn_role") or None,
                    spawn_limit=body.get("spawn_limit") or 0,
                )
                service.store.log_action(
                    "squad",
                    0,
                    dict(name=res["squad"]["name"]),
                    bool(res.get("ok")),
                    f"launched {res['launched']} agent(s) in {res['squad']['cwd']}"
                    + "".join(f"; {a.get('agent_name')} failed: {a.get('error')}" for a in res["squad"]["agents"] if not a.get("ok")),
                    self._who(),
                )
                return self._send(200 if res.get("ok") else 409, res)
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
            if method == "POST" and path == "/api/model":
                if service.fleet is None:
                    return self._send(503, {"error": "process scanning is off"})
                body = self._body()
                pid, created = _pid(body)
                ok, message, row, norm_model, norm_eff = service.set_session_model(
                    pid,
                    created,
                    body.get("model") or "",
                    effort=body.get("effort"),
                )
                entry = service.store.log_action("model", pid, row, ok, message, self._who())
                with service.fleet_lock:
                    service.fleet.last["at"] = 0
                return self._send(200 if ok else 409, dict(entry, model=norm_model, effort=norm_eff, error=None if ok else message))
            if method == "POST" and path in ("/api/kill", "/api/send"):
                if service.fleet is None:
                    return self._send(503, {"error": "process scanning is off"})
                body = self._body()
                pid, created = _pid(body)
                if path == "/api/kill":
                    ok, message, row = service.fleet.kill(pid, created)
                    action = "kill"
                else:
                    from almanac import uds
                    from almanac.store import NEEDS
                    text = body.get("text")
                    if not isinstance(text, str):
                        raise ValueError("text is required")
                    # one text for the wire and the journal, split the way the transcript read splits it, so the
                    # recipient's copy is recognised as this row: a typed channel line, then a typed flag
                    channel, text = uds.split_channel(text.strip())
                    typed, text = uds.split_needs(text)
                    text = text.strip()
                    if not text:
                        raise ValueError("text is required")
                    needs = str(body.get("needs") or "").strip().lower() or typed
                    if needs not in NEEDS:
                        raise ValueError("needs must be 'reply', 'blocking' or empty")
                    hub_pipe = service.hub_peer.pipe_path if service.hub_peer else None
                    ok, message, row = service.fleet.send(pid, created, uds.needs_marker(needs, self._who()) + text,
                                                          from_name=self._who(), from_pipe=hub_pipe, channel=channel)
                    action = "send"
                    if ok and row:
                        service.store.record_message(
                            from_name=self._who(),
                            to_name=row.get("name") or f"pid-{pid}",
                            to_session=row.get("session_id"),
                            to_provider=row.get("provider"),
                            squad_id=row.get("squad_id"),
                            channel=channel,
                            body=text,
                            transport="uds" if row.get("has_uds") else "console",
                            needs=needs,
                        )
                    message = f"{message}: {text[:200]}" if ok else message
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
