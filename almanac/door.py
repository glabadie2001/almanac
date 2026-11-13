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
GET  /api/mesh/branches?squad= the squad's member branches holding commits not in its folder's HEAD, each with
                             `ahead` (its commit count past HEAD) or the `error` git gave
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
POST /api/mesh/object/respawn {ref, model?, effort?}: end a task owner's session and start a fresh one of the same
                             handle and worktree on that task, its log as the kickoff (the operator, or a squadmate
                             whose role may spawn; never on itself)
POST /api/mesh/object/amend  {ref, title?, body?, files?}: replace a task's title, body and files; the `amend` log
                             entry keeps the old ones (the operator; a squad's task: a member whose role may spawn;
                             a backlog task: any agent of its project)
POST /api/mesh/spawn         {task, name?, role?, ref?}: an agent whose role may_spawn adds one member of the squad's
                             spawn_role (or of `role` when spawn_role is `*`), within spawn_limit (403 or 400 with
                             the reason otherwise). `ref`: put it on an existing queued, unowned task of the squad
                             instead of filing a new one (`task` is then optional extra instructions)
POST /api/squads/assign      {ref, squad_id}: move a queued backlog task into a running squad of its project (the
                             operator's only); the squad's lead is told (409 with the reason otherwise)
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


RESTART_CODE = 75            # Almanac's exit code for "start me again": the launchers loop on it (EX_TEMPFAIL)
LAUNCHER_ENV = "ALMANAC_LAUNCHER"     # set by run.ps1 / run.sh: something will bring Almanac back after it exits
PREFLIGHT_TIMEOUT = 180
SPAWN_ROW_WAIT = 5.0          # seconds the door waits for a just-launched agent's spawn row (spawner's watch is < 1 s)


class Service:
    """The store, the fleet scanner, the uds-messaging hub, and the background ingest loop."""

    def __init__(self, store, transcripts, interval=300, fleet=None, state=None, claude="claude", gemini="agy",
                 prices=None, namer=None, probe=None):
        self.store, self.transcripts, self.interval = store, Path(transcripts), interval
        self.fleet = fleet
        self.context_warn = 300_000       # tokens re-read per step at which the UI warns (config `context_warn`)
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
        self.exit_code = 0                            # what `python -m almanac` returns when the door stops
        self.on_exit = None                           # set by __main__: stops the server; tests inject their own
        self.launcher = False                         # set by __main__: a launcher will start Almanac again
        self.config_path = None                       # set by __main__: the config this Almanac started with
        self.restart_lock = threading.Lock()          # one restart at a time
        self.restarting = False                       # latched once a restart is under way

    def exit(self, code: int) -> None:
        """Stop the door, so that `python -m almanac` returns `code` (RESTART_CODE: the launcher starts it again)."""
        self.exit_code = code
        if self.on_exit is not None:
            self.on_exit()

    def preflight(self) -> tuple[bool, str]:
        """Run `python -m almanac --check` on the code now on disk, in a subprocess: (passed, its output)."""
        import subprocess as _sp
        import sys as _sys
        root = Path(__file__).resolve().parents[1]
        try:
            argv = [_sys.executable, "-m", "almanac", "--check", "--state", str(self.state)]
            if self.config_path:
                argv += ["--config", str(self.config_path)]       # the config the relaunch will read
            proc = _sp.run(argv, cwd=str(root),
                           capture_output=True, timeout=PREFLIGHT_TIMEOUT, creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        except (OSError, _sp.TimeoutExpired) as exc:
            return False, f"preflight could not run: {exc}"
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        return proc.returncode == 0, out[-4000:]

    def headless_live(self) -> list[str]:
        """Handles of the headless Claude agents still running: their stdin is a pipe Almanac holds, so they end
        when Almanac exits."""
        procs = self.__dict__.get("headless_procs") or {}
        live = [sid for sid, proc in procs.items() if proc.poll() is None]
        return sorted((self.store.spawn_by_session(sid) or {}).get("agent_name") or sid[:8] for sid in live)

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
            for p in self.fleet.last.get("processes", []):
                if p.get("session_id") and not p.get("context"):     # a tail with no usage: what the last ingest stored
                    p["context"] = self.store.session_context(p["session_id"])
            self.fleet.last["context_warn"] = self.context_warn
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
                    context=p.get("context"),
                    context_total=bool(p.get("context_total")),
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

    def caller(self, name: str | None, session: str | None = None) -> dict | None:
        """The spawn row of the agent calling the door: by the session its token carries, or for an old two-part
        token by its handle (the newest row). Handles are reused across squads, so a token with a session never falls
        back to the handle: that could bind it to another squad's agent of the same name. None: no such spawn."""
        if not name:
            return None
        if session is None:
            return self.store.spawn_of(name)
        row = self.store.spawn_by_session(session)
        # a spawn's row is written after its launch watch: a session still being spawned is waited for, briefly
        deadline = time.monotonic() + SPAWN_ROW_WAIT
        while row is None and session in getattr(self, "spawning", ()) and time.monotonic() < deadline:
            time.sleep(0.05)
            row = self.store.spawn_by_session(session)
        return row if row and row["agent_name"] == name else None

    def mesh_send(
        self,
        *,
        to: str,
        text: str,
        from_name: str = "operator",
        from_session: str | None = None,
        channel: str | None = None,
        squad_id: str | None = None,
        needs: str | None = None,
        write_fn=None,
        exclude=(),
        answers: bool = True,
    ) -> dict:
        """Deliver a message over Claude's `uds-messaging` protocol (with broadcast fan-out and SQLite journaling).
        `needs` ('reply' or 'blocking') flags it as wanting an answer: the delivered text opens with a marker line,
        the journal keeps the sender's words and the flag, and the flag stays open until the recipient answers.
        `from_session`: the sender's session when the door knows it (two squads may share a handle). `answers`:
        False for the hub's own messages about work objects, which must not close anyone's open flag."""
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
        sender_peer = (next((p for p in all_peers if from_session and p.get("session_id") == from_session), None)
                       or next((p for p in all_peers if p["name"] == sender or p.get("session_id") == sender), None))
        # a sender that is not a live session is still known by its spawn record
        known = sender_peer or self.caller(sender, from_session) or {}
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
                answers=answers,
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
                answers=answers,
            )
            return dict(ok=True, delivered=delivered, failed=failed, message=entry)

        # Direct peer target: resolve in Fleet first, then directly in ~/.claude/sessions. A handle two squads share
        # means the sender's squad's member.
        matches = [p for p in all_peers if p["name"] == target or p.get("session_id") == target or str(p["pid"]) == target]
        recipient = next((p for p in matches if eff_squad and p.get("squad_id") == eff_squad), None) or next(iter(matches), None)
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
                answers=answers,
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
            answers=answers,
        )
        return dict(ok=True, delivered=[res["target_name"]], transport="uds", message=entry)

    def may_spawn(self, agent_name: str, role_id: str | None = None, session: str | None = None
                  ) -> tuple[bool, str, dict | None, dict | None, int]:
        """(ok, reason, squad, role to add, http status): may this agent add a member to its squad through
        mesh_spawn? Its newest spawn row must name a squad that is active; its role must have may_spawn; the squad's
        spawn_role must resolve: a fixed role (an agent naming another is refused) or `*`, where the agent must name
        a defined role; slots must be left (spawn_used < spawn_limit, whatever roles were added); that role's runner
        must be found. Role mistakes are 400 (the request is wrong), the rest 403. The caller holds `spawn_lock`
        across this check and the spawn, so two calls cannot both take the last slot.
        Advisory, like agent tokens: an agent runs as this user and could reach the door another way."""
        from almanac.store import ANY_ROLE
        spawn = self.caller(agent_name, session)
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

    def may_page_operator(self, agent_name: str, session: str | None = None) -> tuple[bool, str]:
        """(allowed, hint): may this agent address the operator? Yes when its newest spawn has no role (or the role
        is gone), when its role has `may_page_operator`, or when the newest direct message between it and the
        operator came from the operator: answering is not paging, and it buys one reply per operator message.
        Advisory only: the hub's named pipe does not check senders, and an agent can read the door's secret."""
        from almanac.store import OPERATOR_NAMES
        spawn = self.caller(agent_name, session)
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


# ---------------------------------------------------------------------------------------- work objects (stage 1)
TITLE_MAX, BODY_MAX, FILES_MAX = 120, 8000, 50
LOG_KINDS = ("note", "learned", "dead", "parked", "next")       # what an agent may write; create/status/assign/message
                                                                 # are the hub's own
LOG_FIELDS = ("id", "ts", "agent_name", "role_id", "provider", "kind", "body", "status_from", "status_to", "owner_to",
              "message_id", "sha")


WHY_MAX = 200


def _why(raw) -> str | None:
    """A spawner's one-line reason for its model and effort: whitespace folded, capped."""
    text = " ".join(str(raw or "").split())
    return text[:WHY_MAX] or None


def elastic_choice(role: dict, body: dict) -> dict:
    """The model and effort an agent picked for a member of `role`, each checked against what the role allows: its own
    default plus its `models` / `efforts` (additional choices). An agent must pick when the role offers more than one;
    with a single option, leaving it out means that one. 400 naming what is allowed. (The operator's own paths never
    come here: they fall back to the role's model and effort.)"""
    from almanac.spawner import split_model_effort
    raw_m, raw_e = str(body.get("model") or "").strip(), str(body.get("effort") or "").strip()
    # an effort written into the model (`claude-haiku-5-5-low`, `sonnet-max`) is a pick too, checked like `effort`
    # (as everywhere else, an explicit effort wins)
    model, embedded = split_model_effort(role["provider"], raw_m) if raw_m else (None, None)
    effort = (split_model_effort(role["provider"], None, raw_e)[1] or raw_e) if raw_e else embedded
    out = {}
    for key, raw, value in (("model", raw_m, model or raw_m), ("effort", raw_e or raw_m, effort)):
        allowed = list(dict.fromkeys(([role[key]] if role.get(key) else []) + list(role.get(key + "s") or [])))
        if not value:
            if len(allowed) > 1:
                raise ValueError(f"pick {'a model' if key == 'model' else 'an effort'}: role {role['id']} allows "
                                 + ", ".join(allowed) + f" (mesh_spawn(..., {key}=...))")
            if allowed:
                out[key] = allowed[0]           # the one option, even when the role's own default is blank
            continue
        if value not in allowed:
            raise ValueError(f"{key} {raw!r}: role {role['id']} allows "
                             + (", ".join(allowed) if allowed else f"no {key} choice (it runs the runner's default)"))
        out[key] = value
    return out


def _refused(status, message):
    from almanac import web
    return web.Refused(status, message)


def ref_of(row: dict) -> str:
    from almanac.store import OBJECT_KINDS
    letter = next(k for k, v in OBJECT_KINDS.items() if v == row["kind"])
    return f"{letter}-{row['id']}"


def parse_ref(ref) -> tuple[int, str | None]:
    """(id, kind or None): `T-12`, `t-12`, `12` or 12."""
    from almanac.store import OBJECT_KINDS
    m = re.fullmatch(r"(?:([A-Za-z])-)?(\d+)", str(ref).strip())
    if not m or (m.group(1) and m.group(1).upper() not in OBJECT_KINDS):
        raise ValueError(f"{ref!r} is not a ref: send T-12 or 12")
    return int(m.group(2)), OBJECT_KINDS.get((m.group(1) or "").upper())


def object_squad(service, agent: str | None, caller: dict | None, asked: str | None) -> str:
    """The squad a request acts in. An agent: its own (the one its session was spawned into); naming another is 403.
    The operator: the one named, or the only live squad."""
    asked = str(asked or "").strip() or None
    if agent:
        own = (caller or {}).get("squad_id")
        if not own:
            raise _refused(403, f"{agent} is not in a squad")
        if asked and asked != own:
            raise _refused(403, f"squad {asked} is not yours")
        return own
    if asked:
        if service.store.squad(asked) is None:
            raise _refused(404, f"no squad {asked}")
        return asked
    live = {p["squad_id"] for p in service.mesh_peers() if p.get("squad_id")}
    if len(live) == 1:
        return live.pop()
    raise ValueError("squad is required: there is not exactly one live squad")


def load_object(service, agent, caller, ref, *, bad_ref=404) -> dict:
    """The object `ref` names. 404 when there is none (`bad_ref`: the status for that, 400 for `mesh_send(re=)`);
    403 when it belongs to another squad than the calling agent's."""
    oid, kind = parse_ref(ref)
    row = service.store.object_row(oid)
    if row is None or (kind and row["kind"] != kind):
        raise _refused(bad_ref, f"no object {ref}")
    if agent and row["squad_id"] != (caller or {}).get("squad_id"):
        # a backlog task (no squad yet) is any squad's of the same project to read and log to
        if row["squad_id"] or not caller or row["project"] != caller_project(service, caller):
            raise _refused(403 if bad_ref == 404 else bad_ref, f"{ref} belongs to another squad")
    return row


def caller_project(service, caller: dict) -> str:
    """The project a squad works in: the checkout root of its folder."""
    from almanac.spawner import project_root
    squad = service.store.squad(caller.get("squad_id") or "") or {}
    if not squad.get("cwd"):
        raise _refused(403, "not in a squad")
    return project_root(squad["cwd"])


def log_identity(agent, caller) -> dict:
    """Who a log row is by: the agent's session, handle, role and provider, or the operator."""
    if not agent:
        return dict(agent_name="operator")
    c = caller or {}
    return dict(session=c.get("session_id"), agent_name=agent, role_id=c.get("role_id"), provider=c.get("provider"))


def object_docs(service, rows: list[dict], *, body: bool = False) -> list[dict]:
    """The API shape of `rows`: names, the owner's state from the live roster, child counts and the last log line."""
    peers = [p for p in service.mesh_peers() if p.get("session_id")]
    states = {p["session_id"]: ("idle" if p.get("status") in (None, "idle") else "busy") for p in peers}
    contexts = {p["session_id"]: p.get("context") for p in peers}
    totals = {p["session_id"]: bool(p.get("context_total")) for p in peers}
    names = {}
    for r in rows:
        sid = r["owner_session"]
        if sid and sid not in names:
            names[sid] = (service.store.spawn_by_session(sid) or {}).get("agent_name")
    stats = service.store.object_stats([r["id"] for r in rows])
    merged = merge_state(service, rows)
    out = []
    for r in rows:
        st, sid = stats[r["id"]], r["owner_session"]
        last = st["last_log"]
        doc = dict(ref=ref_of(r), id=r["id"], kind=r["kind"], title=r["title"], status=r["status"],
                   squad_id=r["squad_id"], parent=f"T-{r['parent_id']}" if r["parent_id"] else None,
                   owner_name=names.get(sid), owner_session=sid,
                   owner_state=(states.get(sid, "ended") if sid else None), owner_context=contexts[sid] if sid in contexts else (service.store.session_context(sid) if sid else None),
                   owner_context_total=totals.get(sid, False), files=r["files"],
                   created_by_name=r["created_by_name"], created_ts=r["created_ts"], updated_ts=r["updated_ts"],
                   project=r["project"], data=r["data"],
                   sha=r["sha"], branch=r["branch"], base=r["base"], merged=merged.get(r["id"]),
                   children=dict(total=st["children"][0], accepted=st["children"][1]), log_count=st["log_count"],
                   last_log=last and dict(ts=last["ts"], agent_name=last["agent_name"], kind=last["kind"],
                                          body=last["body"][:200], status_to=last["status_to"]))
        if body:
            doc["body"] = r["body"]
        out.append(doc)
    return out


def merge_state(service, rows: list[dict]) -> dict:
    """{id: merged} for the rows that carry a sha: True when the sha is in its squad folder's HEAD. One
    `git rev-list <sha>... --not HEAD` per squad (a sha it prints is not merged). Computed on each read, never stored;
    a call that fails leaves its rows out (merged None)."""
    from almanac.spawner import git_try
    by_squad: dict = {}
    for r in rows:
        if r["sha"] and r["squad_id"]:
            by_squad.setdefault(r["squad_id"], []).append(r)
    out = {}
    for squad_id, group in by_squad.items():
        cwd = (service.store.squad(squad_id) or {}).get("cwd")
        shas = sorted({r["sha"] for r in group})
        ok, text = git_try(cwd, "rev-list", *shas, "--not", "HEAD", "--") if cwd else (False, "")
        if ok:
            unmerged = set(text.split())
            out.update({r["id"]: r["sha"] not in unmerged for r in group})
    return out


def handin_head(service, row: dict) -> tuple[dict | None, str]:
    """What a task going `in review` shipped: ({sha, branch, base}, a note for its log row). The owner's worktree
    HEAD with its branch and base; an owner without a worktree (or no owner): the squad folder's HEAD, no branch.
    A git call that fails or times out records nothing and says so: it never refuses the hand-in."""
    from almanac.spawner import branch_and_base, git_try
    sp = service.store.spawn_by_session(row["owner_session"]) if row["owner_session"] else None
    wt = (sp or {}).get("worktree_path")
    where = wt or (service.store.squad(row["squad_id"]) or {}).get("cwd")
    if not where:
        return None, "almanac-hub: no commit recorded: the task has no folder to read"
    ok, sha = git_try(where, "rev-parse", "--verify", "HEAD")
    if not ok and not wt and "not a git repository" in sha:
        return None, ""                       # a squad folder outside git: nothing to record, nothing gone wrong
    if not ok:
        return None, f"almanac-hub: no commit recorded: {sha}"
    branch, base = (sp.get("branch"), sp.get("base")) if wt else (None, None)
    if wt and not branch:                     # spawned before T-46: its row has neither
        branch, base = branch_and_base(wt)
    head = dict(sha=sha, branch=branch, base=base)
    ok, dirty = git_try(where, "status", "--porcelain")
    if not ok:
        return head, f"almanac-hub: could not check {where} for uncommitted changes: {dirty}"
    if dirty:
        return head, (f"almanac-hub: {len(dirty.splitlines())} uncommitted path(s) in {where} are not in "
                      f"{sha[:7]}")
    return head, ""


def squad_branches(service, agent, caller, query: dict) -> dict:
    """GET /api/mesh/branches?squad=: the squad's member branches holding commits its folder's HEAD lacks (what
    ending a squad must name), newest member first, one `rev-list --count` each. A branch git cannot count is listed
    with its error, never left out."""
    from almanac.spawner import branch_and_base, git_try
    squad_id = object_squad(service, agent, caller, query.get("squad"))
    cwd = (service.store.squad(squad_id) or {}).get("cwd")
    out, seen = [], set()
    for sp in service.store.spawned(squad_id=squad_id, limit=10_000):
        wt = sp.get("worktree_path")
        if not wt or wt in seen:
            continue
        seen.add(wt)
        branch = sp.get("branch") or branch_and_base(wt)[0]
        doc = dict(agent_name=sp["agent_name"], branch=branch, worktree=wt, ahead=None)
        ok, n = (git_try(cwd, "rev-list", "--count", f"HEAD..refs/heads/{branch}", "--") if branch
                 else (False, "its branch is unknown"))
        if not ok:
            out.append(dict(doc, error=n))
        elif int(n):
            out.append(dict(doc, ahead=int(n)))
    return dict(squad=squad_id, branches=out)


def log_doc(entry: dict) -> dict:
    return {k: entry[k] for k in LOG_FIELDS}


def tree_order(rows: list[dict]) -> list[dict]:
    """Parents before their children, siblings by id. A row whose parent is not in `rows` is a root here."""
    ids = {r["id"] for r in rows}
    kids: dict = {}
    for r in rows:                       # rows come by id, so each sibling list is already in order
        kids.setdefault(r["parent_id"] if r["parent_id"] in ids else None, []).append(r)
    out, stack = [], list(reversed(kids.get(None, [])))
    while stack:
        r = stack.pop()
        out.append(r)
        stack.extend(reversed(kids.get(r["id"], [])))
    return out


def list_objects(service, agent, caller, query: dict) -> dict:
    """A squad's tasks, or with `backlog=1` the backlog: an agent's own project's, the operator's of `project`
    (a folder) or of every project."""
    from almanac.store import TASK_STATUSES
    project = None
    if _flag(query.get("backlog")):
        squad = None
        if agent:
            if not caller:
                raise _refused(403, f"{agent} is not in a squad")
            project = caller_project(service, caller)
        elif query.get("project"):
            project = _project(query["project"])
    else:
        squad = object_squad(service, agent, caller, query.get("squad"))
    statuses = [x.strip() for x in (query.get("status") or "").split(",") if x.strip()]
    bad = [x for x in statuses if x not in TASK_STATUSES]
    if bad:
        raise ValueError(f"status {bad[0]!r}: one of {', '.join(TASK_STATUSES)}")
    limit = max(1, min(500, int(query.get("limit", "200"))))
    rows = service.store.objects(squad_id=squad, kind=query.get("kind") or None, statuses=statuses, project=project,
                                 owner=(query.get("owner") or "").strip() or None, q=query.get("q") or None)
    return dict(objects=object_docs(service, tree_order(rows)[:limit]), rev=service.store.objects_rev())


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = (value or "").strip()
    if len(value) > BODY_MAX:
        raise ValueError(f"{key} is {len(value)} characters; trim it to {BODY_MAX}")
    return value


def _files(raw, project: str) -> list[str]:
    """Repo-relative paths or globs with forward slashes, from a list or one per line."""
    items = raw.splitlines() if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if items is None:
        raise ValueError("files: a list of repo-relative paths, or one per line")
    out = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("files: each one is a path")
        f = item.strip().replace("\\", "/")
        if not f:
            continue
        if f.startswith("/") or re.match(r"[A-Za-z]:/", f):
            root = project.replace("\\", "/").rstrip("/") + "/"
            if not project or not f.lower().startswith(root.lower()):
                raise ValueError(f"files are repo-relative: {f}")
            f = f[len(root):]
        while f.startswith("./"):
            f = f[2:]
        if ".." in f.split("/") or f.startswith("/") or re.match(r"[A-Za-z]:", f):
            raise ValueError(f"files are inside the repo: {item}")
        if f and f not in out:
            out.append(f)
    if len(out) > FILES_MAX:
        raise ValueError(f"{len(out)} files; at most {FILES_MAX}")
    return out


def _owner(service, squad: str, handle) -> dict:
    """The newest spawn of `handle` in `squad`: a live or ended member."""
    handle = str(handle or "").strip()
    found = service.store.spawned(squad_id=squad, agent_name=handle, limit=1) if handle else []
    if not found:
        raise ValueError(f"owner {handle!r}: no member of squad {squad} by that name")
    if found[0].get("retired_ts"):
        raise _refused(409, f"owner {handle!r} was retired when its task was accepted (its role is fresh-per-task): "
                            "put a fresh member on the task with mesh_spawn(ref=..., task=...)")
    return found[0]


def _status(value) -> str:
    from almanac.store import TASK_STATUSES
    if value not in TASK_STATUSES:
        raise ValueError(f"status {value!r}: one of {', '.join(TASK_STATUSES)}")
    return value


def _hub_tell(service, *, to_name, to_session, text, agent, caller, squad) -> tuple[bool, int]:
    """Send a work-object message as the caller and journal it; a recipient that has ended still gets the journal
    row, undelivered. It never closes an open flag. (delivered, message id)"""
    from_name, from_session = agent or "operator", (caller or {}).get("session_id") if agent else None
    try:
        res = service.mesh_send(to=to_session or to_name, text=text, from_name=from_name, from_session=from_session,
                                squad_id=squad, answers=False)
    except (ValueError, OSError) as exc:
        res = dict(ok=False, error=str(exc))
    if res.get("ok"):
        return bool(res.get("delivered")), res["message"]["id"]
    entry = service.store.record_message(from_name=from_name, from_session=from_session, to_name=to_name,
                                         to_session=to_session, squad_id=squad, body=text, transport="undelivered",
                                         answers=False)
    return False, entry["id"]


def _assign(service, row: dict, owner: dict, agent, caller) -> tuple[bool | None, int | None]:
    """Send the owner its assignment message: (delivered, message id). An agent assigning a task to itself is told
    nothing (None, None): the assign log row is still written by the caller."""
    if agent and owner["session_id"] == (caller or {}).get("session_id"):
        return None, None
    ref = ref_of(row)
    files = ", ".join(row["files"]) or "(none named)"
    text = (f"[{ref} assigned to you] {row['title']}\n\n{row['body']}\n\nFiles: {files}\n"
            f"Hand in with mesh_log(ref=\"{ref}\", status=\"in review\")")
    return _hub_tell(service, to_name=owner["agent_name"], to_session=owner["session_id"], text=text, agent=agent,
                     caller=caller, squad=row["squad_id"])


def _flag(value) -> bool:
    return value is True or str(value or "").strip().lower() in ("1", "true", "yes")


def _project(folder) -> str:
    from almanac.spawner import project_root
    if not isinstance(folder, str) or not folder.strip() or not Path(folder).expanduser().is_dir():
        raise ValueError(f"project: no folder {folder!r}")
    return project_root(folder)


DEFAULT_ROSTER = ({"role": "orchestrator-claude"}, {"role": "reviewer-gemini"})   # a dispatch's squad when none is set
DEFAULT_SPAWN = ("*", 5)                                                           # and what its lead may add


def _roster(service, raw) -> list[dict]:
    """A backlog task's squad: [{role, name?}], lead first; each role must exist. A bare string is a role."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise ValueError("roster: a list of 1 to 20 members, {role, name?}, lead first")
    out = []
    for m in raw:
        m = {"role": m} if isinstance(m, str) else m
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise ValueError("roster: each member is {role, name?}")
        role, name = m["role"].strip(), m.get("name")
        if service.store.role(role) is None:
            raise ValueError(f"roster: no role {role!r}")
        if name is not None:
            from almanac import spawner, web
            if not isinstance(name, str) or not spawner.AGENT_NAME_RE.match(name.strip()):
                raise ValueError(f"roster: name {name!r}: letters, digits, '-' or '_', at most 48")
            if name.strip().lower() in web.RESERVED_AGENT_NAMES:
                raise ValueError(f"roster: {name.strip()!r} is reserved")
        out.append(dict(role=role, **({"name": name.strip()} if name else {})))
    return out


def _backlog_data(service, body: dict) -> dict:
    """The squad a backlog task will dispatch: `roster`, `spawn_role`, `spawn_limit`, each checked now."""
    data = {}
    if body.get("roster") is not None:
        data["roster"] = _roster(service, body["roster"])
    if body.get("spawn_role"):
        data["spawn_role"] = service.store._spawn_settings(body["spawn_role"], 0)[0]
    if body.get("spawn_limit") not in (None, ""):
        data["spawn_limit"] = service.store._spawn_settings(None, body["spawn_limit"])[1]
    return data


def _parent_row(service, ref, squad: str | None, project: str, child: int | None = None) -> dict:
    """The task `ref` as a parent of a task in `squad` (None: the backlog of `project`): it must be a task of that same
    squad, or of the same project's backlog. `child`: the task being re-parented, which may not be, or sit under, its
    own new parent. (PLAN has no depth limit.)"""
    pid, _ = parse_ref(ref)
    parent = service.store.object_row(pid)
    where = f"squad {squad}" if squad else "the backlog of this project"
    if (parent is None or parent["kind"] != "task" or parent["squad_id"] != squad
            or (not squad and parent["project"] != project)):
        raise ValueError(f"parent {ref}: no task of {where}")
    walk, seen = parent, set()
    while child is not None and walk is not None and walk["id"] not in seen:
        if walk["id"] == child:
            raise ValueError(f"parent {ref}: it is, or sits under, T-{child} itself")
        seen.add(walk["id"])
        walk = service.store.object_row(walk["parent_id"]) if walk["parent_id"] else None
    return parent


def file_object(service, agent, caller, body: dict) -> dict:
    """POST /api/mesh/object: file a task; with an owner, that is its assignment. `backlog`: no squad yet, in the
    caller's project (the operator's: `project`, a folder, or the squad named), queued until dispatched."""
    kind = str(body.get("kind") or "task").strip()
    if kind != "task":
        raise ValueError(f"kind {kind}: not yet")
    backlog = _flag(body.get("backlog"))
    if backlog:
        if body.get("owner"):
            raise ValueError("a backlog task has no owner until it is dispatched")
        if body.get("status") not in (None, "", "queued"):
            raise ValueError("a backlog task is queued until it is dispatched")
    elif any(body.get(k) not in (None, "", []) for k in ("roster", "spawn_role", "spawn_limit")):
        raise ValueError("roster, spawn_role and spawn_limit are for a backlog task")
    by_folder = backlog and not agent and body.get("project")
    squad = None if by_folder else object_squad(service, agent, caller, body.get("squad"))
    title = _text(body, "title")
    if not 1 <= len(title) <= TITLE_MAX:
        raise ValueError(f"title is required, 1 to {TITLE_MAX} characters")
    text = _text(body, "body")
    if backlog:
        project = _project(body["project"]) if by_folder else caller_project(service, dict(squad_id=squad))
        data, squad = _backlog_data(service, body), None
    else:
        project, data = (service.store.squad(squad) or {}).get("cwd") or "", None
    files = _files(body.get("files") or [], project)
    parent_id = _parent_row(service, body["parent"], squad, project)["id"] if body.get("parent") else None
    owner = _owner(service, squad, body["owner"]) if body.get("owner") else None
    status = _status(body["status"]) if body.get("status") else ("in progress" if owner else "queued")
    who = log_identity(agent, caller)
    row = service.store.create_object(
        kind=kind, project=project, squad_id=squad, title=title, body=text, status=status,
        owner_session=owner and owner["session_id"], parent_id=parent_id, files=files,
        created_by=who.get("session"), created_by_name=who["agent_name"], who=who, data=data)
    delivered = None
    if owner:
        delivered, mid = _assign(service, row, owner, agent, caller)
        service.store.log_object(row["id"], who=who, kind="assign", owner_name=owner["agent_name"], message_id=mid)
    return dict(object=object_docs(service, [row], body=True)[0], delivered=delivered)


def _new_parent(service, row: dict, raw) -> dict | None:
    """mesh_log(parent=): the new parent of backlog task `row`, None for '' (no parent). Not for a task that is in a
    squad, nor a parent it already has."""
    ref = ref_of(row)
    if row["squad_id"]:
        raise ValueError(f"{ref} is in squad {row['squad_id']}: only a backlog task changes its parent")
    if not isinstance(raw, str):
        raise ValueError("parent must be a ref, or '' to clear it")
    new = _parent_row(service, raw.strip(), None, row["project"], child=row["id"]) if raw.strip() else None
    if (new and new["id"]) == row["parent_id"]:
        raise ValueError(f"{ref} already has " + (f"parent T-{row['parent_id']}" if new else "no parent"))
    return new


def amend_task(service, agent, caller, body: dict) -> dict:
    """POST /api/mesh/object/amend {ref, title?, body?, files?}: replace a task's title, body and files (the ones sent).
    The `amend` log entry keeps all three as they were, so nothing is lost. The operator may; for a task in a squad a
    member of it whose role may spawn (its lead); for a backlog task any agent of its project. The same length and
    file checks as filing."""
    row = load_object(service, agent, caller, body.get("ref") or "")
    ref = ref_of(row)
    if agent and row["squad_id"]:
        mine = service.store.role((caller or {}).get("role_id") or "") if (caller or {}).get("role_id") else None
        if not mine or not mine.get("may_spawn"):
            raise _refused(403, f"role {(caller or {}).get('role_id') or '(none)'} may not amend a squad's tasks: "
                                "ask your lead or the operator")
    if all(body.get(k) is None for k in ("title", "body", "files")):
        raise ValueError("amend needs a title, a body or files")
    title, text, files = row["title"], row["body"], row["files"]
    if body.get("title") is not None:
        title = _text(body, "title")
        if not 1 <= len(title) <= TITLE_MAX:
            raise ValueError(f"title is required, 1 to {TITLE_MAX} characters")
    if body.get("body") is not None:
        text = _text(body, "body")
    if body.get("files") is not None:
        files = _files(body["files"], row["project"])
    changed = [k for k, new, old in (("title", title, row["title"]), ("body", text, row["body"]),
                                     ("files", files, row["files"])) if new != old]
    if not changed:
        raise ValueError(f"{ref} already reads that way: nothing to amend")
    entry = service.store.amend_object(
        row["id"], title=title, body=text, files=files, who=log_identity(agent, caller),
        log_body=(f"amended {', '.join(changed)}. Before:\ntitle: {row['title']}\n"
                  f"files: {', '.join(row['files']) or '(none)'}\nbody:\n{row['body']}"))
    return dict(object=object_docs(service, [service.store.object_row(row["id"])], body=True)[0], entry=log_doc(entry))


def _notify(service, row: dict, who: dict, status, handin: bool, head: dict | None, text: str, agent, caller) -> None:
    """One notice per agent for a status change or a hand-in: the task's creator (unless it made the change), and on a
    hand-in each live reviewer of the squad (role_class of its role is `reviewer`) with the branch and commit. Never
    the agent that logged it; a reviewer who is also the creator hears once."""
    move = f"{row['status']} → {status}" if status else "in review again"
    notice = f"[{ref_of(row)} {move} by {who['agent_name']}] {row['title']}"
    if handin:
        notice += (f"\nBranch: {(head or {}).get('branch') or '(none)'}, commit: "
                   f"{head['sha'][:7] if head and head.get('sha') else 'no commit recorded'}")
    notice += f"\n\n{text}" if text else ""
    by_creator = (row["created_by"] == who.get("session")) if row["created_by"] else row["created_by_name"] == who["agent_name"]
    to = []                              # (name, session)
    if not by_creator:
        to_name, to_session = row["created_by_name"], row["created_by"]
        if to_session and (service.store.spawn_by_session(to_session) or {}).get("squad_id") != row["squad_id"]:
            to_name, to_session = "operator", None     # filed in the backlog by another squad, dispatched since
        to.append((to_name, to_session))
    if handin:
        roles = {r["id"]: r["title"] for r in service.store.roles()}
        skip = {who.get("session"), row["created_by"]} - {None}
        to += [(p["name"], p["session_id"]) for p in service.mesh_peers(squad_id=row["squad_id"])
               if p.get("session_id") and p["session_id"] not in skip
               and role_class(p.get("agent_role"), roles.get(p.get("agent_role"))) == "reviewer"]
    for to_name, to_session in dict.fromkeys(to):
        _hub_tell(service, to_name=to_name, to_session=to_session, text=notice, agent=agent, caller=caller,
                  squad=row["squad_id"])


def log_to_object(service, agent, caller, body: dict, *, after: list | None = None) -> dict:
    """POST /api/mesh/object/log: append to a task's log, optionally changing its status or owner. A changed owner
    gets the assignment message; a changed status sends its creator one notice; a task becoming accepted is a context
    boundary (context_boundary). `after`: the route's list of steps to run once the response is written; without
    one they run before this returns."""
    row = load_object(service, agent, caller, body.get("ref") or "")
    kind = str(body.get("kind") or "note").strip()
    if kind not in LOG_KINDS:
        raise ValueError(f"kind {kind!r}: one of {', '.join(LOG_KINDS)}")
    text = _text(body, "body")
    asked = _status(body["status"]) if body.get("status") else None
    status = asked if asked != row["status"] else None
    handin = asked == "in review" and bool(row["squad_id"])     # again while in review: a fix round moves the sha
    if body.get("owner") and not row["squad_id"]:
        raise ValueError(f"{ref_of(row)} is in the backlog: it has no owner until it is dispatched")
    if agent and body.get("status") and not row["squad_id"] and body["status"] != row["status"]:
        raise _refused(403, f"{ref_of(row)} is in the backlog: only the operator changes its status")
    owner = _owner(service, row["squad_id"], body["owner"]) if body.get("owner") else None
    owner = owner if owner and owner["session_id"] != row["owner_session"] else None
    reparent = body.get("parent") is not None
    new_parent = _new_parent(service, row, body["parent"]) if reparent else None
    if not (text or status or owner or reparent or handin):
        raise ValueError("body is required unless status, owner or parent changes")
    # an owner set in the same call is the one whose worktree shipped it
    head, note = (handin_head(service, dict(row, owner_session=owner["session_id"]) if owner else row) if handin
                  else (None, ""))
    if status == "accepted" and merge_state(service, [row]).get(row["id"]) is False:     # said, never refused
        note = (f"almanac-hub: accepted with {row['sha'][:7]} not in "
                f"{(service.store.squad(row['squad_id']) or {}).get('cwd')}'s HEAD")
    text = "\n\n".join(x for x in (text, note) if x)
    who = log_identity(agent, caller)
    delivered = mid = entry = None
    if reparent:
        with _DISPATCH_LOCK:             # the lock dispatch and assign hold: checked again and written as one step, so
            fresh = service.store.object_row(row["id"])       # two crossing re-parents cannot both pass the cycle check
            new_parent = _new_parent(service, fresh, body["parent"])      # and a task that just joined a squad is refused
            old = f"T-{fresh['parent_id']}" if fresh["parent_id"] else "(none)"
            entry = service.store.reparent_object(
                row["id"], new_parent and new_parent["id"], who=who,
                body=f"parent {old} → {ref_of(new_parent) if new_parent else '(none)'}")
    if text or status or owner or handin:
        if owner:
            delivered, mid = _assign(service, dict(row, owner_session=owner["session_id"]), owner, agent, caller)
        entry = service.store.log_object(
            row["id"], who=who, kind="assign" if owner else "status" if status else kind, body=text, status=status,
            owner_session=owner and owner["session_id"], owner_name=owner and owner["agent_name"], message_id=mid,
            head=head)
    if status or handin:                 # a fix round (in review again) notifies like the first hand-in
        _notify(service, row, who, status, handin, head, text, agent, caller)
    later = [] if after is None else after
    if status == "accepted":
        context_boundary(service, service.store.object_row(row["id"]), caller if agent else None, after=later)
    doc = dict(object=object_docs(service, [service.store.object_row(row["id"])], body=True)[0],
               entry=log_doc(entry), delivered=delivered)
    if after is None:
        for step in later:
            step()
    return doc


HUB = dict(agent_name="almanac-hub")      # who the context_boundary log entries are by


def context_boundary(service, row: dict, caller: dict | None, *, after: list) -> None:
    """`row` just became accepted: apply its members' context_policy, here in the hub, on this one event only.
    fresh-per-task: the owner is retired (_retire). compact-at-N: the owner, and the creator when it is a member of
    the squad (the lead's boundary), get `/compact` when their context is at or above N (_compact). `caller`: the
    agent that made the change (None: the operator); ending it waits in `after` until the response is written."""
    from almanac.store import parse_context_policy
    squad = row["squad_id"]
    if not squad:
        return
    members = []
    owner = service.store.spawn_by_session(row["owner_session"]) if row["owner_session"] else None
    if owner and owner["squad_id"] == squad:
        members.append(owner)
    # the creator by its session, so another squad's member of the same name is never touched; then that handle's
    # newest session here: a respawned lead is still the lead
    first = service.store.spawn_by_session(row["created_by"]) if row["created_by"] else None
    creator = (service.store.spawned(squad_id=squad, agent_name=first["agent_name"], limit=1)
               if first and first["squad_id"] == squad else [])
    if creator and creator[0]["session_id"] not in {m["session_id"] for m in members}:
        members.append(creator[0])
    for sp in members:
        if sp.get("retired_ts"):
            continue
        role = service.store.role(sp["role_id"]) if sp.get("role_id") else None
        try:
            kind, n = parse_context_policy((role or {}).get("context_policy"))
        except ValueError:
            continue
        if kind == "fresh" and owner is not None and sp["session_id"] == owner["session_id"]:
            _retire(service, row, sp, caller, after)
        elif kind == "compact":
            _compact(service, row, sp, n)


def _live_proc(service, sid: str) -> dict | None:
    """The fleet row of session `sid`'s running process (just its pid when the scan has no row yet), or None."""
    pid = _live_sessions(service).get(sid)
    if pid is None:
        return None
    return next((p for p in service.fleet_now().get("processes", []) if p["pid"] == pid), None) or dict(pid=pid)


def _retire(service, row: dict, sp: dict, caller: dict | None, after: list) -> None:
    """fresh-per-task: end the owner's session, mark its spawn row retired (its slot comes back) and say so on the
    task. Not while it owns another open task of the squad. Its worktree and branch are left for the lead to merge.
    A caller retiring itself is ended only once its response is written."""
    from almanac.store import TASK_STATUSES
    name, sid, ref = sp["agent_name"], sp["session_id"], ref_of(row)
    still = [ref_of(o) for o in service.store.objects(squad_id=row["squad_id"], owner=name,
                                                       statuses=[s for s in TASK_STATUSES if s != "accepted"])
             if o["id"] != row["id"]]
    if still:
        service.store.log_object(row["id"], who=HUB, kind="retired", body=(
            f"{name} is not retired (fresh-per-task): it still owns {', '.join(still)}"))
        return
    if service.fleet is not None:
        with service.fleet_lock:
            service.fleet.last["at"] = 0                  # a fresh scan: an owner that just exited is not "live"
    proc = _live_proc(service, sid)
    context = (proc or {}).get("context") or (service.store.chat(sid) or {}).get("context")
    size = f"; its last context was {context:,} tokens" if context else ""
    keep = ". Its worktree and branch are left as they are for the lead to merge"

    def end() -> tuple[bool, str]:
        if proc is None:
            return True, "was not running"
        if service.fleet is None or not proc.get("created"):
            return False, "could not be ended: Almanac has no process scan to end it by"
        ok, message, found = service.fleet.kill(proc["pid"], proc["created"])
        if not ok and found is not None:
            return False, f"could not be ended (pid {proc['pid']}): {message}"
        service.launched.pop(sid, None)
        with service.fleet_lock:
            service.fleet.last["at"] = 0
        return True, "was ended" if ok else "was not running"

    # the deferred end only when it can work; otherwise the ordinary path below says it was not retired
    if (proc is not None and caller and caller.get("session_id") == sid and service.fleet is not None
            and proc.get("created")):
        service.store.retire_spawn(sid)
        service.store.log_object(row["id"], who=HUB, kind="retired", body=(
            f"{name} retired on {ref} (fresh-per-task): its session {sid} ends once this reply is written{size}{keep}"))

        def end_self():
            ok, what = end()
            if not ok:                  # still running after all: it gets its row and its slot back
                logging.getLogger("almanac").warning("retiring %s (session %s): %s", name, sid, what)
                service.store.retire_spawn(sid, retired=False)
                service.store.log_object(row["id"], who=HUB, kind="retired", body=(
                    f"{name} was not retired (fresh-per-task): its session {sid} {what}{size}"))
        after.append(end_self)
        return
    ok, what = end()
    if not ok:                  # still running: it keeps its row and its slot, and the lead is told on the task
        service.store.log_object(row["id"], who=HUB, kind="retired", body=(
            f"{name} was not retired (fresh-per-task): its session {sid} {what}{size}"))
        return
    service.store.retire_spawn(sid)
    service.store.log_object(row["id"], who=HUB, kind="retired", body=(
        f"{name} retired on {ref} (fresh-per-task): its session {sid} {what}{size}{keep}"))


# No second /compact to a session within this many seconds of the last. Time, not a watched reading: an agent typed
# at mid-turn keeps taking steps, so its reading moves before the queued /compact runs, and two accepts in one turn
# would otherwise queue it twice.
COMPACT_COOLDOWN = 600


def _compact(service, row: dict, sp: dict, n: int) -> None:
    """compact-at-N: type `/compact` into a Claude agent's console (console.send, never fleet.send: that delivers a
    peer message, not keys; typed mid-turn it queues), or have a Gemini agent's host send agy a raw `/compact` turn.
    Only on a context reading at or above N that is not a running total, and not twice on the same reading."""
    from almanac import console, uds
    name, sid = sp["agent_name"], sp["session_id"]
    proc = _live_proc(service, sid)
    context = (proc or {}).get("context")
    if not proc or not context or proc.get("context_total") or context < n:
        return
    sent = service.__dict__.setdefault("compacted", {})   # {session: (its context, time) when /compact was last sent}
    last, at = sent.get(sid, (None, float("-inf")))
    if last == context or time.monotonic() - at < COMPACT_COOLDOWN:
        return
    before = f"{name} at {context:,} tokens (compact-at-{n})"
    provider = proc.get("provider") or sp.get("provider") or "claude"
    if provider == "gemini":
        if not proc.get("has_uds") or service.fleet is None:
            ok, what = False, "its host has no uds pipe to tell"
        else:
            try:
                uds.send_peer(service.fleet.home / "sessions", str(proc["pid"]), json.dumps({"op": "compact"}),
                              from_name="almanac-hub", channel="__almanac_control__", alive_fn=lambda _: True,
                              from_pipe=service.hub_peer.pipe_path if service.hub_peer else "almanac-hub")
                ok, what = True, "its host sends agy a /compact turn"
            except Exception as exc:
                ok, what = False, f"its host could not be told: {exc}"
    elif proc.get("role") == "headless" or sid in (service.__dict__.get("headless_procs") or {}):
        sent[sid] = (context, time.monotonic())        # nothing will change that: say it once per reading
        service.store.log_object(row["id"], who=HUB, kind="compact", body=(
            f"{before} not compacted: a headless Claude has no console to type /compact into"))
        return
    else:
        ok, what = console.send(proc["pid"], "/compact")
    if ok:
        sent[sid] = (context, time.monotonic())
    service.store.log_object(row["id"], who=HUB, kind="compact", body=(
        f"{before}: /compact sent ({what})" if ok else f"{before}: /compact not sent: {what}"))


_DISPATCH_LOCK, _DISPATCHING = threading.Lock(), set()


def _move_children(service, row: dict, squad: dict | None, *, dry: bool = False) -> dict:
    """Under _DISPATCH_LOCK, `row` (a backlog parent) just joined `squad`: bring its queued backlog children (and theirs,
    the same way) into the squad too, queued and unowned as they are. A child that is not queued, is being
    dispatched, is already in a squad or is of another project stays where it is. {moved: [ref], left: [{ref, why}]};
    each moved child's log says so, and the parent's log says which. `dry`: only say what would move (no squad yet)."""
    moved, left, stack = [], [], [row["id"]]
    while stack:
        for kid in service.store.children(stack.pop()):
            kref = ref_of(kid)
            if kid["squad_id"]:
                why = f"already in squad {kid['squad_id']}"
            elif kid["id"] in _DISPATCHING:
                why = "being dispatched"
            elif kid["project"] != row["project"]:
                why = "another project"
            elif kid["status"] != "queued":
                why = kid["status"]
            else:
                if not dry:
                    service.store.log_object(
                        kid["id"], who=dict(agent_name="operator"), kind="assign", squad_id=squad["id"],
                        body=f"moved from the backlog into squad {squad['name']} ({squad['id']}) with its parent "
                             f"T-{kid['parent_id']} by operator")
                moved.append(kref)
                stack.append(kid["id"])
                continue
            left.append(dict(ref=kref, why=why))
    if (moved or left) and not dry:
        service.store.log_object(row["id"], who=HUB, kind="note", body=(
            ("its slices moved with it: " + ", ".join(moved) if moved else "no slice moved with it")
            + ("; left where they are: " + ", ".join(f"{x['ref']} ({x['why']})" for x in left) if left else "")))
    return dict(moved=moved, left=left)


def dispatch_task(service, ref, *, almanac_url: str, token: str) -> dict:
    """POST /api/squads/dispatch (the operator's): launch a squad on a backlog task. It is the lead's task, so no
    second T is filed; it joins the squad only once the lead has started. A failed launch leaves it as it was."""
    from almanac import spawner
    row = load_object(service, None, None, ref)
    ref = ref_of(row)
    data = row["data"]
    roster = _roster(service, data.get("roster") or list(DEFAULT_ROSTER))   # checked again: a role may have gone
    spawn_role, spawn_limit = service.store._spawn_settings(data.get("spawn_role") or DEFAULT_SPAWN[0],
                                                            data.get("spawn_limit", DEFAULT_SPAWN[1]))
    members = [dict(role_id=m["role"], **({"agent_name": m["name"]} if m.get("name") else {})) for m in roster]
    slices = _move_children(service, row, None, dry=True)["moved"]        # what will join with it, as it reads now
    plan = (f"Its slices {', '.join(slices)} are already filed and join your plan with it, queued and unowned: read "
            "mesh_objects() first, put members on them with mesh_spawn(ref=...), and file only what is missing"
            if slices else "Plan it, file each slice with mesh_file")
    members[0].update(files=row["files"], task=(
        f"{row['title']}\n\nYou lead this squad on backlog task {ref}: the mission above is its body. {plan}, and "
        f"hand in with mesh_log(ref=\"{ref}\", status=\"in review\")."))
    with _DISPATCH_LOCK:                 # a double click launches one squad
        current = service.store.object_row(row["id"])
        if current["squad_id"] or row["id"] in _DISPATCHING:
            raise _refused(409, f"{ref} is already dispatched"
                           + (f" to squad {current['squad_id']}" if current["squad_id"] else ""))
        if current["status"] != "queued":
            raise _refused(409, f"{ref} is {current['status']}; only a queued task is dispatched")
        _DISPATCHING.add(row["id"])
    try:
        res = spawner.launch_squad(service, almanac_url=almanac_url, token=token, name=row["title"],
                                   cwd=row["project"], brief=f"{row['title']}\n\n{row['body']}".strip(),
                                   members=members, spawn_role=spawn_role, spawn_limit=spawn_limit, lead_task=row)
    finally:
        with _DISPATCH_LOCK:
            _DISPATCHING.discard(row["id"])
    after = service.store.object_row(row["id"])
    res["dispatched"] = after["squad_id"] == res["squad"]["id"]
    if res["dispatched"]:
        with _DISPATCH_LOCK:             # its queued slices go where it went
            res["children"] = _move_children(service, after, res["squad"])
        after = service.store.object_row(row["id"])                # read again: the move wrote to its log
    res["object"] = object_docs(service, [after], body=True)[0]
    return res


def assign_task(service, ref, squad_id) -> dict:
    """POST /api/squads/assign (the operator's): move a queued backlog task into a running squad of its project. It
    stays queued and unowned; the squad's lead (the member the squad was launched with first) is told, once, how to
    put someone on it. No live lead: the task still joins, and the answer says so."""
    row = load_object(service, None, None, ref)
    ref = ref_of(row)
    squad = service.store.squad(str(squad_id or "").strip())
    if squad is None:
        raise _refused(404, f"no squad {squad_id!r}")
    from almanac.spawner import project_root
    with _DISPATCH_LOCK:                 # a double click assigns once
        row, squad = service.store.object_row(row["id"]), service.store.squad(squad["id"]) or squad   # both read again
        if row["squad_id"] or row["id"] in _DISPATCHING:
            raise _refused(409, f"{ref} is not in the backlog: it belongs to squad {row['squad_id']}"
                           if row["squad_id"] else f"{ref} is being dispatched")
        if row["status"] != "queued":
            raise _refused(409, f"{ref} is {row['status']}; only a queued task is assigned")
        if squad["status"] != "active":
            raise _refused(409, f"squad {squad['id']} is {squad['status']}")
        if project_root(squad["cwd"]) != row["project"]:
            raise _refused(409, f"squad {squad['id']} works in {squad['cwd']}, not in {ref}'s project {row['project']}")
        service.store.log_object(row["id"], who=dict(agent_name="operator"), kind="assign", squad_id=squad["id"],
                                 body=f"moved from the backlog into squad {squad['name']} ({squad['id']}) by operator")
        children = _move_children(service, row, squad)
    lead = next((a for a in squad["agents"] if not a["spawned_by"]), None)        # oldest first: launched first
    live = {p.get("session_id") for p in service.mesh_peers(squad_id=squad["id"])}
    found = lead and service.store.spawned(squad_id=squad["id"], agent_name=lead["agent_name"], limit=1)
    lead = found[0] if found else lead                                             # its newest session; the move is done: never refuse now
    out = dict(ok=True, ref=ref, squad_id=squad["id"], lead=lead and lead["agent_name"], lead_live=False, delivered=False,
               children=children)
    if lead and lead["session_id"] in live:
        with_it = f"\n\nIts slices moved with it, queued and unowned too: {', '.join(children['moved'])}." if children["moved"] else ""
        out["delivered"], _ = _hub_tell(
            service, to_name=lead["agent_name"], to_session=lead["session_id"], agent=None, caller=None, squad=squad["id"],
            text=(f"[{ref} moved from the backlog into your squad by the operator] {row['title']}\n\nIt is queued and "
                  f"unowned. Put a fresh member on it with mesh_spawn(ref=\"{ref}\", ...), or hand it to a live member "
                  f"with mesh_log(ref=\"{ref}\", owner=...)." + with_it))
    out["lead_live"] = out["delivered"]
    if not out["delivered"]:
        out["note"] = (f"{ref} joined squad {squad['id']}, but its lead {out['lead'] or '(none)'} is not live: nobody was "
                       "told. Put someone on it from the plan.")
    out["object"] = object_docs(service, [service.store.object_row(row["id"])], body=True)[0]
    return out


def spawn_ref_row(service, ref, squad_id: str) -> dict:
    """mesh_spawn(ref): the task a new member is put on. It must be a task of `squad_id`, queued and unowned."""
    row = load_object(service, None, None, ref)
    name = ref_of(row)
    if row["squad_id"] != squad_id:
        raise _refused(403, f"{name} belongs to "
                       + (f"squad {row['squad_id']}" if row["squad_id"] else "the backlog (an operator assigns it to a squad first)")
                       + f", not to squad {squad_id}")
    if row["status"] != "queued":
        raise _refused(409, f"{name} is {row['status']}; only a queued task takes a new member")
    if row["owner_session"]:
        raise _refused(409, f"{name} already has an owner; hand it over with mesh_log(ref, owner=...)")
    return row


def respawn_kickoff(row: dict, log: list[dict], squad: dict, name: str, room: int) -> str:
    """A respawned owner's first prompt: the mission, its task, the task's log oldest first, and what to do, in at most
    `room` characters when it can (spawner.fit_prompt gives the room a Claude command line has left)."""
    ref = ref_of(row)
    tail = (f"You are a fresh session of `{name}`, started on {ref} in place of the previous one (its context had grown "
            "too large, or it stalled). Carry on from the log: `git status` and `git log` in your worktree show what is "
            f"already done. Hand in as usual: mesh_log(ref=\"{ref}\", status=\"in review\", body=<files touched, the "
            "test command and its result, what is left>).")
    return task_kickoff(row, log, squad, room, tail)


def task_kickoff(row: dict, log: list[dict], squad: dict, room: int, tail: str, *, extra: str = "",
                 files: list[str] | None = None) -> str:
    """A session's first prompt on an existing task: the mission, the task (`extra`: more instructions from whoever
    put the session on it; `files` replace the task's), the task's log oldest first, then `tail`. The oldest log
    entries go first, and the prompt says how to read them; the task and `tail` are never cut."""
    ref = ref_of(row)
    head = [f"## Squad Mission ({squad['name']})\n{squad['brief'].strip()}"] if (squad.get("brief") or "").strip() else []
    head.append(f"## Your task (`{ref}`): {row['title']}\n{row['body'] or '(no body)'}\n\n"
                f"Files: {', '.join(files or row['files']) or '(none named)'}")
    if extra:
        head.append(f"## More instructions\n{extra}")
    entries = []
    for e in log:                                         # oldest first
        line = f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))} {e['agent_name']} [{e['kind']}]"
        if e.get("status_to"):
            line += f" {e.get('status_from') or '–'} → {e['status_to']}"
        if e.get("owner_to"):
            line += f" owner → {e['owner_to']}"
        if e.get("body"):
            line += "\n  " + e["body"].strip().replace("\n", "\n  ")
        entries.append(line)
    room -= len("\n\n".join(head + [tail])) + 200         # 200: the log heading and the note below
    kept = []
    for line in reversed(entries):                        # the newest entries are the ones that say where it stands
        if len(line) + 1 > room:
            break
        kept.insert(0, line)
        room -= len(line) + 1
    dropped = len(entries) - len(kept)
    note = (f"({dropped} older entries left out; mesh_object(\"{ref}\", log_limit=500) reads them all)\n"
            if dropped else "")
    return "\n\n".join(head + [f"## Its log, oldest first\n{note}" + ("\n".join(kept) or "(empty)"), tail])


def respawn_task(service, agent, caller, body: dict, *, almanac_url: str, token: str) -> dict:
    """POST /api/mesh/object/respawn {ref, model?, effort?}: end a task owner's session and start a fresh one with the
    same handle, role, squad and worktree (as it stands, uncommitted files included), whose kickoff is the task and its
    log. The operator may respawn anyone; an agent of the task's squad whose role may spawn, anyone but itself; never
    on an accepted task. Everything is checked before the old session ends, which happens in spawn_agent's
    `before_launch`, once the command line (and the kickoff cut to fit it) is built. A fresh session that does not
    start leaves the task with its old owner, and says on the task when the old one was ended. The caller holds
    spawn_lock."""
    from almanac import config, spawner, web
    row = load_object(service, agent, caller, body.get("ref") or "")
    ref = ref_of(row)
    if not row["squad_id"]:
        raise ValueError(f"{ref} is in the backlog: it has no owner to respawn")
    if not row["owner_session"]:
        raise ValueError(f"{ref} has no owner to respawn")
    if row["status"] == "accepted":
        raise _refused(409, f"{ref} is accepted: there is nothing left to carry on. Set it back to in progress first "
                            "if it needs more work")
    old = service.store.spawn_by_session(row["owner_session"])
    if old is None or old["squad_id"] != row["squad_id"]:
        raise ValueError(f"{ref}'s owner was not spawned into squad {row['squad_id']}: there is nothing to respawn it as")
    name = old["agent_name"]
    if agent:
        mine = service.store.role((caller or {}).get("role_id") or "") if (caller or {}).get("role_id") else None
        if not mine or not mine.get("may_spawn"):
            raise _refused(403, f"role {(caller or {}).get('role_id') or '(none)'} may not respawn squad members")
        if name == agent:
            raise _refused(403, "an agent may not respawn itself: ask your lead or the operator")
    squad = service.store.squad(row["squad_id"])
    if not squad or squad.get("status") != "active":
        raise _refused(409, f"squad {row['squad_id']} is {(squad or {}).get('status') or 'gone'}")
    role = service.store.role(old["role_id"]) if old.get("role_id") else None
    if role is None:
        raise _refused(409, f"{name}'s role {old.get('role_id')!r} no longer exists")
    model, effort = old["model"], old["effort"]
    if body.get("model") or body.get("effort"):
        picks = elastic_choice(role, dict(model=body.get("model") or model, effort=body.get("effort") or effort))
        model, effort = picks.get("model") or model, picks.get("effort") or effort
    runner = service.runners().get(old["provider"]) or {}
    if not runner.get("ok") or runner.get("reason") or (old["provider"] == "claude" and config.is_shim(service.claude)):
        raise _refused(409, runner.get("reason") or f"{old['provider']} runner not found or not launchable: "
                                                     f"{runner.get('path') or '(none)'}")
    wmode = "shared"
    if old["worktree_path"]:
        wt = Path(old["worktree_path"])
        expected = Path(service.state).resolve() / "worktrees" / row["squad_id"] / name
        if not (wt / ".git").exists() or wt.resolve() != expected:
            raise _refused(409, f"{name}'s worktree {wt} is gone or not where Almanac keeps it; a respawn never makes "
                                "a new one. Reassign the task instead")
        wmode = "worktree"
    log = list(reversed(service.store.object_log(row["id"], 100_000)))
    sid = old["session_id"]
    if service.fleet is not None:
        with service.fleet_lock:
            service.fleet.last["at"] = 0              # a fresh scan: an owner that just exited is not "live"
    pid = _live_sessions(service).get(sid)
    proc = None
    if pid is not None:
        if service.fleet is None:
            raise _refused(503, f"{name} is running and process scanning is off: Almanac cannot end it")
        proc = next((p for p in service.fleet_now().get("processes", []) if p["pid"] == pid), None)
        if proc is None:
            raise _refused(409, f"{name}'s process {pid} is not in the fleet scan yet; try again in a moment")
    context = (proc or {}).get("context") or (service.store.chat(sid) or {}).get("context")
    who = log_identity(agent, caller)
    ended = dict(now=False)

    def end_old():
        """spawn_agent's last step before it starts the fresh session: nothing was changed until here."""
        if proc is None:
            return
        ok, message, found = service.fleet.kill(proc["pid"], proc["created"])
        if not ok and found is not None:              # still there and would not end: stop, nothing changed
            raise _refused(409, f"could not end {name} (pid {proc['pid']}): {message}; nothing was changed")
        ended["now"] = ok                             # not ok and not found: it had already exited, go on
        service.launched.pop(sid, None)
        with service.fleet_lock:
            service.fleet.last["at"] = 0

    try:
        res = spawner.spawn_agent(
            service, almanac_url=almanac_url, token=token, cwd=squad["cwd"], agent_name=name, role_id=role["id"],
            provider=old["provider"], model=model or "", effort=effort or "", workspace_mode=wmode,
            initial_prompt=lambda room: respawn_kickoff(row, log, squad, name, room), channels=old["channels"],
            squad_id=squad["id"], squad_name=squad["name"], spawned_by=old["spawned_by"], before_launch=end_old,
            teammates=[t for t in spawner.squad_roster(service, squad) if t["agent_name"] != name])
    except web.Refused:
        raise
    except (RuntimeError, OSError, ValueError) as exc:
        res = dict(ok=False, error=str(exc))
    if not res.get("ok"):
        error = res.get("error") or "the fresh session did not start"
        if ended["now"]:          # the state to avoid: say it where the lead and the operator look
            service.store.log_object(row["id"], who=who, kind="respawn", body=(
                f"respawn failed: {name} (session {sid}) was ended, and the fresh session did not start: {error}. "
                f"{ref} still names the ended session: respawn again, or reassign it."))
        return dict(ok=False, ref=ref, agent_name=name, ended=ended["now"], error=error)
    entry = service.store.log_object(
        row["id"], who=who, kind="respawn", owner_session=res["session_id"], owner_name=name,
        body=(f"{who['agent_name']} respawned {name}: session {sid} → {res['session_id']}"
              + (f" on {model or 'the default model'} at {effort or 'the default effort'}"
                 if body.get("model") or body.get("effort") else "")
              + (f"; the old session's context was {context:,} tokens" if context else "")
              + ("" if ended["now"] else "; the old session was not running")))
    return dict(res, ref=ref, old_session=sid, ended=ended["now"], context=context or None,
                entry=log_doc(entry), object=object_docs(service, [service.store.object_row(row["id"])], body=True)[0])


def make_handler(service, token, *, hosts=()):
    from almanac import web

    class Handler(web.JsonHandler):
        protocol_version = "HTTP/1.1"
        pages = PAGES
        static_dir = STATIC
        log = logging.getLogger("almanac")

        def _who(self):
            return self.agent or "operator"

        def _squad_q(self, query) -> tuple[str | None, bool]:
            """(squad, unscoped) for a read: the one asked for; for an agent that names none, its own squad, plus
            what was posted in no squad (the operator's @all). Same-handle agents of two squads stay apart."""
            asked = query.get("squad") or None
            if asked or not self.agent:
                return asked, False
            own = (service.caller(self.agent, self.agent_session) or {}).get("squad_id")
            return own, bool(own)

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
                    from almanac.spawner import project_root     # `project`: what [ASSIGN] matches a backlog task on
                    return self._send(200, dict(squads=[dict(s, project=project_root(s["cwd"]))
                                                        for s in service.store.squads(int(query.get("limit", "50")))]))
                if path == "/api/mesh":
                    squad_q, unscoped = self._squad_q(query)
                    msg_limit = max(0, min(300, int(query.get("limit", "80"))))
                    return self._send(
                        200,
                        dict(
                            peers=service.mesh_peers(squad_id=squad_q),
                            messages=service.store.messages(squad_id=squad_q, limit=msg_limit, unscoped=unscoped)
                            if msg_limit > 0 else [],
                            latest_id=service.store.latest_message_id(),
                            squads=service.store.squads(limit=20),
                            roles=service.store.roles(),
                            hub_pipe=service.hub_peer.pipe_path if service.hub_peer else None,
                            open=service.store.open_needs(squad_id=squad_q),
                            objects_rev=service.store.objects_rev(),
                            context_warn=service.context_warn,
                        ),
                    )
                if path == "/api/mesh/objects":
                    caller = service.caller(self.agent, self.agent_session)
                    return self._send(200, list_objects(service, self.agent, caller, query))
                if path == "/api/mesh/branches":
                    caller = service.caller(self.agent, self.agent_session)
                    return self._send(200, squad_branches(service, self.agent, caller, query))
                if path == "/api/mesh/object":
                    caller = service.caller(self.agent, self.agent_session)
                    row = load_object(service, self.agent, caller, query.get("ref") or "")
                    lim = max(1, min(500, int(query.get("log_limit", "20"))))
                    return self._send(200, dict(object=object_docs(service, [row], body=True)[0],
                                                log=[log_doc(e) for e in service.store.object_log(row["id"], lim)]))
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
                    squad_q, unscoped = self._squad_q(query)
                    msgs = service.store.messages(
                        peer=query.get("peer") or None,
                        channel=query.get("channel") or None,
                        squad_id=squad_q,
                        unscoped=unscoped,
                        since_id=int(query.get("since_id", "0")),
                        limit=int(query.get("limit", "100")),
                    )
                    return self._send(200, dict(messages=msgs, latest_id=service.store.latest_message_id()))
                if path == "/api/mesh/wait":
                    since_id = int(query.get("since_id", "0"))
                    timeout_s = max(0.0, min(300.0, float(query.get("timeout", "30"))))
                    squad_q, unscoped = self._squad_q(query)
                    msgs = service.store.wait_messages(
                        peer=query.get("peer") or None,
                        channel=query.get("channel") or None,
                        squad_id=squad_q,
                        unscoped=unscoped,
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
                    own, _ = self._squad_q({})
                    if own:                      # an agent's handle means its own squad's member first
                        mate = service.store.spawned(squad_id=own, agent_name=peer, limit=1)
                        peer = mate[0]["session_id"] if mate else peer
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
                    allowed, hint = service.may_page_operator(self.agent, self.agent_session)
                    if not allowed:
                        return self._send(403, dict(ok=False, error=hint))
                caller = service.caller(self.agent, self.agent_session)
                task = None
                if body.get("re"):                       # checked before anything is sent
                    task = load_object(service, self.agent, caller, body["re"], bad_ref=400)
                res = service.mesh_send(
                    to=target,
                    text=body.get("text") or body.get("message") or "",
                    from_name=self.agent or body.get("from_name") or "operator",   # an agent speaks only as itself
                    from_session=(caller or {}).get("session_id") if self.agent else None,
                    channel=body.get("channel"),
                    # an agent's squad is its own: the one its session was spawned into
                    squad_id=(caller or {}).get("squad_id") if self.agent else body.get("squad_id"),
                    needs=body.get("needs"),
                )
                if task and res.get("ok"):
                    msg = res["message"]
                    service.store.log_object(task["id"], who=log_identity(self.agent, caller), kind="message",
                                             body=msg["body"][:200], message_id=msg["id"])
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/mesh/restart":
                return self._restart()
            if method == "POST" and path == "/api/mesh/object":
                return self._send(200, file_object(service, self.agent, service.caller(self.agent, self.agent_session),
                                                   self._body()))
            if method == "POST" and path == "/api/mesh/object/log":
                after = []          # what must wait for the response: ending the caller itself (context_boundary)
                self._send(200, log_to_object(service, self.agent, service.caller(self.agent, self.agent_session),
                                              self._body(), after=after))
                if after:
                    self.close_connection = True
                    self.wfile.flush()
                for step in after:
                    step()
                return None
            if method == "POST" and path == "/api/mesh/object/amend":
                return self._send(200, amend_task(service, self.agent, service.caller(self.agent, self.agent_session),
                                                  self._body()))
            if method == "POST" and path == "/api/mesh/object/respawn":
                body = self._body()
                port = getattr(self.server, "server_port", 8812)
                with service.spawn_lock:          # one respawn at a time: a double click ends one session, not two
                    res = respawn_task(service, self.agent, service.caller(self.agent, self.agent_session), body,
                                       almanac_url=f"http://127.0.0.1:{port}", token=token)
                service.store.log_action(
                    "respawn", res.get("pid") or 0, dict(session_id=res.get("session_id"), name=res.get("agent_name")),
                    bool(res.get("ok")),
                    f"{res['agent_name']} respawned on {res['ref']} (session {res['old_session'][:8]} → "
                    f"{res['session_id'][:8]})" if res.get("ok") else f"respawn {res.get('ref')}: {res.get('error')}",
                    self._who())
                return self._send(200 if res.get("ok") else 409, res)
            if method == "POST" and path == "/api/mesh/spawn":
                # elastic squads: an agent adds one member of the role the operator chose, within the operator's limit;
                # everything but the task and the name comes from that role, never from the agent's body
                from almanac import spawner
                body = self._body()
                task = str(body.get("task") or "").strip()
                if len(task) < 20 and not body.get("ref"):
                    raise ValueError("task is required: at least 20 characters saying what the new member is to do "
                                     "(or ref: an existing queued, unowned task of the squad)")
                task_row = None
                port = getattr(self.server, "server_port", 8812)
                overrides = {}
                with service.spawn_lock:
                    if self.agent:
                        ok, reason, squad, role, code = service.may_spawn(self.agent, body.get("role") or body.get("role_id"),
                                                                          self.agent_session)
                        if not ok:
                            service.store.log_action("spawn", 0, dict(name=body.get("name")), False,
                                                     f"mesh_spawn refused: {reason}", self._who())
                            return self._send(code, dict(ok=False, error=reason))
                        squad_id, role_id, by = squad["id"], role["id"], self.agent
                        by_session = (service.caller(self.agent, self.agent_session) or {}).get("session_id")
                        overrides = elastic_choice(role, body)     # model and effort from the role's allowed sets
                    else:
                        squad_id, role_id, by = body.get("squad_id"), body.get("role_id") or body.get("role"), None
                        by_session = None
                        if not squad_id or not role_id:
                            raise ValueError("the operator names squad_id and role_id")
                        overrides = {k: body[k] for k in ("model", "effort", "workspace_mode", "permission_mode") if body.get(k)}
                    if body.get("ref"):          # checked under the lock: two calls cannot both take the task
                        task_row = spawn_ref_row(service, body["ref"], squad_id)
                    try:
                        res = spawner.join_squad(service, almanac_url=f"http://127.0.0.1:{port}", token=token,
                                                 squad_id=squad_id, role_id=role_id, task=task, agent_name=body.get("name"),
                                                 spawned_by=by, spawned_by_session=by_session, files=body.get("files"),
                                                 why=_why(body.get("why")), task_row=task_row, **overrides)
                    except (RuntimeError, OSError) as exc:
                        res = dict(ok=False, agent_name=body.get("name"), role_id=role_id, error=str(exc))
                choice = res.get("choice") or {}
                service.store.log_action(
                    "spawn", res.get("pid") or 0, dict(session_id=res.get("session_id"), name=res.get("agent_name")),
                    bool(res.get("ok")),
                    (f"{res.get('agent_name')} joined {squad_id} as {role_id} (mesh_spawn) on "
                     f"{choice.get('model') or 'the default model'} at {choice.get('effort') or 'the default effort'}"
                     + (f": {choice['why']}" if choice.get("why") else "")) if res.get("ok") else str(res.get("error")),
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
                                                 spawned_by=None, files=body.get("files"), **overrides)      # the operator's adds never use the limit
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
            if method == "POST" and path == "/api/squads/dispatch":     # not under /api/mesh: no agent token opens it
                port = getattr(self.server, "server_port", 8812)
                res = dispatch_task(service, self._body().get("ref") or "", almanac_url=f"http://127.0.0.1:{port}",
                                    token=token)
                service.store.log_action(
                    "squad", 0, dict(name=res["squad"]["name"], ref=res["object"]["ref"]), res["dispatched"],
                    f"dispatched {res['object']['ref']}: launched {res['launched']} agent(s) in {res['squad']['cwd']}"
                    + "".join(f"; {a.get('agent_name')} failed: {a.get('error')}"
                              for a in res["squad"]["agents"] if not a.get("ok")),
                    self._who())
                return self._send(200 if res["dispatched"] else 409, res)
            if method == "POST" and path == "/api/squads/assign":      # the operator's only, like dispatch
                body = self._body()
                res = assign_task(service, body.get("ref") or "", body.get("squad_id") or "")
                service.store.log_action("squad", 0, dict(name=res["squad_id"], ref=res["ref"]), True,
                                         f"assigned {res['ref']} to squad {res['squad_id']}"
                                         + ("" if res["lead_live"] else "; its lead is not live"), self._who())
                return self._send(200, res)
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

        def _restart(self):
            """POST /api/mesh/restart: the operator, or an agent whose role may_restart. Refused unless a launcher will
            bring Almanac back, no headless Claude agent would die with it, and the code on disk passes a preflight.
            Then logged, told to every live squad, answered, and only then does the door stop."""
            who = self._who()
            if self.agent:
                caller = service.caller(self.agent, self.agent_session)
                role = service.store.role(caller["role_id"]) if caller and caller.get("role_id") else None
                if not role or not role.get("may_restart"):
                    return self._send(403, dict(ok=False, error=f"{self.agent}'s role may not restart Almanac"))
            if not service.launcher:
                return self._send(409, dict(ok=False, error="Almanac was not started by run.ps1, run.cmd or run.sh, so "
                                                            "nothing would start it again; restart it by hand"))
            if service.restarting or not service.restart_lock.acquire(blocking=False):
                return self._send(409, dict(ok=False, error="a restart is already under way"))
            try:
                return self._restart_locked(who)
            finally:
                service.restart_lock.release()

        def _restart_locked(self, who):
            headless = service.headless_live()
            if headless:
                return self._send(409, dict(ok=False, error="headless Claude agents would end with Almanac: "
                                                            + ", ".join(headless)))
            ok, output = service.preflight()
            headless = service.headless_live()         # the preflight takes seconds: one may have started meanwhile
            if ok and headless:
                return self._send(409, dict(ok=False, error="headless Claude agents would end with Almanac: "
                                                            + ", ".join(headless)))
            if not ok:
                service.store.log_action("restart", 0, None, False, "preflight failed", who)
                return self._send(409, dict(ok=False, error="preflight failed: nothing restarted", output=output))
            service.restarting = True                   # latched: nothing restarts twice
            service.store.log_action("restart", os.getpid(), None, True, f"restart asked by {who}", who)
            for squad in sorted({p["squad_id"] for p in service.mesh_peers() if p.get("squad_id")}):
                try:
                    service.mesh_send(to="@squad", squad_id=squad, from_name="almanac-hub", answers=False,
                                      text=f"Almanac is restarting, asked by {who}; back in a few seconds. "
                                           "Mesh tools retry while it is down.")
                except (ValueError, OSError):
                    pass
            self._send(200, dict(ok=True, restarting=True, code=RESTART_CODE, preflight=output))
            self.close_connection = True
            service.exit(RESTART_CODE)
            return None

        def _handle(self):
            try:
                self._dispatch()
            except web.Refused as exc:
                self._send(exc.status, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc) if isinstance(exc, ValueError) and str(exc) else "Invalid request"})
            except Exception as exc:
                self._fail(exc)

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _handle

    return web.protect(Handler, token=token, hosts=hosts, timeout=60)


def make_door(service, port, token, hosts=()):
    from almanac import web
    return web.serve(make_handler(service, token, hosts=hosts), port)
