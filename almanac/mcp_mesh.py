"""Almanac Mesh MCP server & CLI: gives every Claude and Gemini agent live visibility into peer context streams,
inter-agent contact streams, and two-way `uds-messaging` communication.

Stdio MCP mode (configured automatically by `almanac.spawner`):
    python -m almanac.mcp_mesh --url http://127.0.0.1:8812 --token <token> [--self <agent_name>]

CLI mode (usable from any agent shell or script):
    python -m almanac.mcp_mesh --cli roster
    python -m almanac.mcp_mesh --cli context <peer> [--limit 30]
    python -m almanac.mcp_mesh --cli contacts [--peer <name>] [--channel <#chan>]
    python -m almanac.mcp_mesh --cli send <to> <message...> [--channel <#chan>]
    python -m almanac.mcp_mesh --cli wait [--timeout 120]      (the operator's scripts; an agent ends its turn instead)
    python -m almanac.mcp_mesh --cli objects [--status <s,..>] [--owner <name|none>] [--q <text>]
    python -m almanac.mcp_mesh --cli object <ref> [--log-limit 20]
    python -m almanac.mcp_mesh --cli diff <ref> [--stat] [--path <p>] [--commit <sha>]
    python -m almanac.mcp_mesh --cli file <title> [--body <text>] [--files <path> ...] [--owner <name>] [--parent <ref>]
    python -m almanac.mcp_mesh --cli log <ref> [<body...>] [--kind <k>] [--status <s>] [--owner <name>] [--parent <ref>|""]
    python -m almanac.mcp_mesh --cli amend <ref> [--title <t>] [--body <text>] [--files <path> ...]
    python -m almanac.mcp_mesh --cli respawn <ref> [--model <m>] [--effort <e>]
    python -m almanac.mcp_mesh --cli spawn [<task...>] [--ref <T-n>] [--role <r>] [--name <n>] [--model <m>] [--effort <e>]
    python -m almanac.mcp_mesh --cli restart
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PREFLIGHT_WAIT = 200.0  # the door's preflight may take up to 180 s

MCP_TOOLS = [
    {
        "name": "mesh_roster",
        "description": (
            "List all live Claude and Gemini agents on this machine, including peer name, provider, model, "
            "assigned role, squad, busy/idle state, working directory, and uds-messaging readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "squad_id": {
                    "type": "string",
                    "description": "Optional squad ID filter.",
                },
            },
        },
    },
    {
        "name": "mesh_context_stream",
        "description": (
            "Read the live normalized context stream (recent prompts, reasoning/replies, tool calls, errors, "
            "and peer messages) of any running Claude or Gemini peer agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer agent name (e.g. 'arch-gemini' or 'impl-claude'), session ID, or PID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return from the end of the peer's stream (default: 30, max: 120).",
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional filter of event kinds: you, claude, gemini, tool, result, error, peer_in, peer_out.",
                },
            },
            "required": ["peer"],
        },
    },
    {
        "name": "mesh_contact_stream",
        "description": (
            "Read the inter-agent contact/communication stream (cross-session messages exchanged over uds-messaging), "
            "optionally filtered by peer, channel (e.g. '#squad'), or squad_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Filter messages involving a specific peer name (defaults to all team messages).",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional channel filter such as '#squad' or '#review'.",
                },
                "squad_id": {
                    "type": "string",
                    "description": "Optional squad ID filter.",
                },
                "since_id": {
                    "type": "integer",
                    "description": "Only return messages with id > since_id.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum messages to return (default: 50, max: 200).",
                },
            },
        },
    },
    {
        "name": "mesh_send",
        "description": (
            "Send a message over the Claude uds-messaging Named Pipe protocol to a specific peer (Claude or Gemini), "
            "'operator' (the human running the squad, if your role may page them), '@squad' (your squad), "
            "or a '#channel' (its subscribers in your squad). '@all' is the operator's. Set `needs` when you want an answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Target peer name, session ID, PID, '@squad', '@all', or '#channel'.",
                },
                "message": {
                    "type": "string",
                    "description": "Markdown message body to deliver.",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional channel tag (e.g. '#architecture', '#review').",
                },
                "needs": {
                    "type": "string",
                    "enum": ["reply", "blocking"],
                    "description": (
                        "Flag the message as needing an answer: 'reply' = wants an answer, not urgent; 'blocking' = you "
                        "(or the squad) cannot continue until it is answered. It stays open until the recipient sends "
                        "you a direct message. Omit for an ordinary message."
                    ),
                },
                "re": {
                    "type": "string",
                    "description": "The task this message is about (e.g. 'T-12'): it is logged on that task.",
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "mesh_spawn",
        "description": (
            "Add one member to your squad, if your role may add members. The role it gets (with its workspace and "
            "permission mode) and how many may be added are the operator's choice, shown in mesh_roster as the squad's "
            "spawn_role, spawn_limit and spawn_used. You give the task, and the model and effort: both are required when "
            "the role offers more than one (its model plus `models`, its effort plus `efforts`, in mesh_roster's roles), "
            "optional when it offers one. Refused with the reason when the limit is used up or a pick is missing or "
            "not allowed. The squad is told who joined. With `ref` it puts the new member on an existing task of your "
            "squad that is queued and unowned (one the operator assigned from the backlog, say) instead of filing a "
            "new one: the task's body and log are its task and `task` is optional extra instructions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": ("The new member's slice: what to build, which files it may touch, how to test it, whom to "
                                    "report to. Required unless `ref` is given; then extra instructions."),
                },
                "ref": {
                    "type": "string",
                    "description": "An existing task of your squad, e.g. 'T-12', queued and unowned: the member takes it on, with its log.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional handle; by default the role's short form plus a number, e.g. impl-claude-2.",
                },
                "role": {
                    "type": "string",
                    "description": ("The role id to add. Required when the squad's spawn_role is '*' (any role; ids in mesh_roster's "
                                    "roles); when the squad allows one role, leave it out or name that role."),
                },
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Repo-relative paths or globs the new member's task may touch (filed on its T)."},
                "model": {"type": "string", "description": ("The role's model or one of its `models` (mesh_roster). Required "
                                                            "when the role offers more than one; else optional.")},
                "effort": {"type": "string", "description": ("The role's effort or one of its `efforts`. Required when the "
                                                             "role offers more than one; else optional.")},
                "why": {"type": "string", "description": "One line: why this model and effort fit the slice."},
            },
        },
    },
    {
        "name": "mesh_objects",
        "description": (
            "List your squad's tasks (the plan) in tree order: parents before their parts. Each row has its ref "
            "(T-12), title, status, owner, the last log line, and once handed in its `sha`, `branch` and `merged`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Comma list: queued, in progress, in review, accepted, blocked."},
                "owner": {"type": "string", "description": "A handle, or 'none' for unowned tasks."},
                "q": {"type": "string", "description": "Text to find in titles and bodies."},
                "kind": {"type": "string", "description": "Object kind (stage 1: task)."},
                "limit": {"type": "integer", "description": "Max rows (default 200, max 500)."},
                "squad": {"type": "string", "description": "The operator's: which squad. Agents always read their own."},
                "backlog": {"type": "boolean",
                            "description": "List the project's backlog instead: tasks no squad has taken yet."},
            },
        },
    },
    {
        "name": "mesh_object",
        "description": ("Read one task by ref (T-12): body, files, owner, status and its log, newest first. A task "
                        "handed in carries `sha` (its owner's head then), `branch`, `base` and `merged` (sha in the "
                        "squad folder's HEAD now)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The ref, e.g. 'T-12' (or the bare id)."},
                "log_limit": {"type": "integer", "description": "Log entries to return (default 20, max 500)."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "mesh_diff",
        "description": ("Read what a handed-in task changed: the diff of its `base..sha` (its owner's branch against the "
                        "commit it was cut from), in one call, without the builder's worktree. Read-only, your squad's "
                        "tasks only. Over 60,000 characters it is cut: `truncated: true` and `stat_text` (the --stat) "
                        "say what was left out; read the rest with `path`."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The task's ref, e.g. 'T-12'."},
                "stat": {"type": "boolean", "description": "Only the --stat: which files changed, how much."},
                "path": {"type": "string", "description": "Only this repo-relative path."},
                "commit": {"type": "string", "description": "Only this commit (hex id), one of the task's own."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "mesh_file",
        "description": (
            "File a task in your squad. Filing it with an owner is the assignment: the owner gets one message with "
            "the task. Use `parent` to make it part of another task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["task"], "description": "Stage 1 files tasks only."},
                "title": {"type": "string", "description": "One line, at most 120 characters."},
                "body": {"type": "string", "description": "What to do, at most 8,000 characters."},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Repo-relative paths or globs the task may touch (at most 50)."},
                "owner": {"type": "string", "description": "The squad member's handle who owns it."},
                "parent": {"type": "string", "description": "Ref of the task this is a part of."},
                "status": {"type": "string", "enum": ["queued", "in progress", "in review", "accepted", "blocked"],
                           "description": "Default: in progress with an owner, queued without."},
                "backlog": {"type": "boolean", "description": (
                    "File follow-up work in the project's backlog instead of your squad: no owner, queued until the "
                    "operator dispatches a squad on it. `parent` is then another backlog task of the project (a "
                    "feature and its slices).")},
                "roster": {"type": "array", "description": "Backlog only: the squad to dispatch, lead first.",
                           "items": {"type": "object", "properties": {"role": {"type": "string"},
                                                                      "name": {"type": "string"}},
                                     "required": ["role"]}},
                "spawn_role": {"type": "string",
                               "description": "Backlog only: the role (or '*') that squad's lead may add."},
                "spawn_limit": {"type": "integer", "description": "Backlog only: how many members it may add (0-20)."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "mesh_log",
        "description": (
            "Append to a task's log, optionally changing its status or owner. Hand in with status='in review' and a "
            "body naming files touched, the test command and result, and what is left. A status change tells the "
            "task's creator; a new owner gets the assignment message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The task's ref, e.g. 'T-12'."},
                "body": {"type": "string", "description": "The entry. Required unless status, owner or parent changes."},
                "kind": {"type": "string", "enum": ["note", "learned", "dead", "parked", "next"],
                         "description": "note (default), learned, dead (a dead end), parked ('retry when ...'), next."},
                "status": {"type": "string", "enum": ["queued", "in progress", "in review", "accepted", "blocked"]},
                "owner": {"type": "string", "description": "Hand the task to this squad member."},
                "parent": {"type": "string", "description": (
                    "A backlog task only: its new parent, a backlog task of the project ('T-7'); '' clears it. The "
                    "entry says from what to what.")},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "mesh_amend",
        "description": (
            "Change a task's title, body or files (send only what changes). The log entry (kind `amend`) keeps the old "
            "title, body and files. A task in a squad: its lead (a role that may add members) or the operator; a "
            "backlog task: any agent of its project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The task, e.g. 'T-12'."},
                "title": {"type": "string", "description": "One line, at most 120 characters."},
                "body": {"type": "string", "description": "At most 8,000 characters."},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Repo-relative paths or globs (at most 50); replaces the list."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "mesh_respawn",
        "description": (
            "Respawn a task's owner, if your role may add members: its session ends and a fresh one starts with the same "
            "handle, role and worktree (uncommitted files included), whose kickoff is the task and its whole log. Use it "
            "when the owner's context has grown too large, or when it stalled and `continue` did not get it going. It "
            "uses no spawn slot. Not on yourself, nor on a backlog task. If the fresh session cannot start, the task "
            "keeps its old owner and you are told."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The task, e.g. 'T-12'."},
                "model": {"type": "string", "description": "Optional: another of the role's models (mesh_roster); "
                                                           "by default the owner's own."},
                "effort": {"type": "string", "description": "Optional: another of the role's efforts; by default the "
                                                            "owner's own."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "mesh_restart",
        "description": (
            "Restart Almanac, if your role may: only after a merge that changed Python code, never with a failing "
            "suite (static files need no restart). Almanac first runs a preflight on the code on disk and refuses, "
            "with its output, if that fails; it also refuses unless a launcher will bring it back."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def infer_self_name(explicit: str | None = None, sessions_dir: Path | None = None) -> str:
    """Resolve the calling agent's peer name from flag -> env -> parent process tree in ~/.claude/sessions."""
    if explicit and explicit.strip():
        return explicit.strip()
    env_name = os.environ.get("ALMANAC_AGENT_NAME") or os.environ.get("CLAUDE_PEER_NAME")
    if env_name and env_name.strip():
        return env_name.strip()
    sdir = Path(sessions_dir or (Path.home() / ".claude" / "sessions"))
    if sdir.is_dir():
        from almanac.fleet import process_table
        from almanac.uds import discover_peers
        parent_map = {p["pid"]: p["ppid"] for p in process_table()}
        by_pid = {p["pid"]: p for p in discover_peers(sdir) if p.get("name")}
        cur = os.getpid()
        for _ in range(8):
            if cur in by_pid and by_pid[cur].get("provider") != "almanac":
                return by_pid[cur]["name"]
            nxt = parent_map.get(cur)
            if not nxt or nxt == cur:
                break
            cur = nxt
    return f"agent-{os.getpid()}"


def http_api(base_url: str, token: str, path: str, *, method: str = "GET", body: dict | None = None, timeout: float = 30.0) -> dict:
    url = base_url.rstrip("/") + path
    payload = json.dumps(body).encode("utf-8") if body is not None else (b"{}" if method != "GET" else None)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = {"error": raw or str(exc)}
        raise RuntimeError(doc.get("error") or f"HTTP {exc.code}") from exc


RETRY_S = 30.0          # how long a call waits out a door that is down (a restart), refused or reset connections only


def _retryable(exc: BaseException, method: str) -> bool:
    """A refused connection means nothing was listening, so nothing happened: safe to send again whatever the method.
    Any other dropped connection (reset, or aborted, which Windows raises when the door exits under a blocked GET)
    may come after the door carried the request out (a spawn, a restart), so only a GET is sent again."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ConnectionRefusedError):
        return True
    return isinstance(reason, ConnectionError) and method.upper() == "GET"


def http_api_retrying(base_url: str, token: str, path: str, **kw) -> dict:
    """`http_api`, but a door that is down (a restart) is waited out for about RETRY_S seconds, so a `mesh_wait` in
    flight across it does not end an agent's turn with a tool error. See `_retryable`; a 4xx or 5xx is never retried."""
    deadline = None             # from the first failure: a mesh_wait may have been open for minutes before it
    while True:
        try:
            return http_api(base_url, token, path, **kw)
        except (urllib.error.URLError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) or not _retryable(exc, kw.get("method") or "GET"):
                raise
            deadline = deadline if deadline is not None else time.monotonic() + RETRY_S
            if time.monotonic() >= deadline:
                raise
        time.sleep(1.0)


class MeshClient:
    """Executes mesh operations against Almanac's HTTP API (or an injected callable in tests)."""

    def __init__(self, *, url: str, token: str, self_name: str | None = None, squad_id: str | None = None, api_fn=None):
        self.url = url
        self.token = token
        self.self_name = infer_self_name(self_name)
        self.squad_id = squad_id or os.environ.get("ALMANAC_SQUAD_ID") or None
        self._api = api_fn or (lambda path, method="GET", body=None, timeout=30.0: http_api_retrying(
            self.url, self.token, path, method=method, body=body, timeout=timeout))
        self.cursor_id: int | None = None

    def _ensure_cursor(self) -> int:
        if self.cursor_id is None:
            doc = self._api("/api/mesh/messages?limit=1")
            msgs = doc.get("messages") or []
            self.cursor_id = int(doc.get("latest_id") or (msgs[-1]["id"] if msgs else 0))
        return self.cursor_id

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        if name == "mesh_roster":
            params_r: dict[str, str | int] = {"limit": 0}
            if args.get("squad_id"):
                params_r["squad"] = str(args["squad_id"])
            doc = self._api(f"/api/mesh?{urllib.parse.urlencode(params_r)}")
            return {
                "peers": doc.get("peers") or [],
                "squads": doc.get("squads") or [],
                "open": doc.get("open") or [],
                "roles": [{k: r.get(k) for k in ("id", "title", "provider", "model", "effort", "models", "efforts", "context_policy")}
                          for r in doc.get("roles") or []],
                "hub_pipe": doc.get("hub_pipe"),
            }
        if name == "mesh_context_stream":
            peer = str(args.get("peer") or "").strip()
            if not peer:
                raise ValueError("peer is required")
            limit = max(1, min(120, int(args.get("limit") or 30)))
            kinds = ",".join(str(k) for k in (args.get("kinds") or []) if k)
            qs = urllib.parse.urlencode({"peer": peer, "limit": limit, **({"kinds": kinds} if kinds else {})})
            return self._api(f"/api/mesh/context?{qs}")
        if name == "mesh_contact_stream":
            params: dict[str, str | int] = {"limit": max(1, min(200, int(args.get("limit") or 50)))}
            if args.get("peer"):
                params["peer"] = str(args["peer"])
            if args.get("channel"):
                params["channel"] = str(args["channel"])
            if args.get("squad_id"):
                params["squad"] = str(args["squad_id"])
            if args.get("since_id") is not None:
                params["since_id"] = int(args["since_id"])
            doc = self._api(f"/api/mesh/messages?{urllib.parse.urlencode(params)}")
            if doc.get("latest_id") is not None:
                self.cursor_id = max(self.cursor_id or 0, int(doc["latest_id"]))
            max_chars = int(args.get("max_body_chars") or 4000)
            for m in doc.get("messages") or []:
                if isinstance(m.get("body"), str) and len(m["body"]) > max_chars:
                    m["body"] = m["body"][:max_chars] + f"\n… [truncated {len(m['body']) - max_chars} chars]"
            return doc
        if name == "mesh_send":
            to = str(args.get("to") or "").strip()
            message = str(args.get("message") or "").strip()
            if not to or not message:
                raise ValueError("'to' and 'message' are required")
            payload = {
                "to": to,
                "text": message,
                "from_name": str(args.get("from_name") or self.self_name),
                "channel": args.get("channel"),
                "squad_id": args.get("squad_id") or self.squad_id,
                "needs": args.get("needs") or None,
            }
            if args.get("re"):
                payload["re"] = str(args["re"])
            return self._api("/api/mesh/send", method="POST", body=payload)
        if name == "mesh_restart":
            return self._api("/api/mesh/restart", method="POST", body={}, timeout=PREFLIGHT_WAIT)
        if name == "mesh_objects":
            params = {k: args[k] for k in ("status", "owner", "q", "kind", "limit") if args.get(k)}
            if args.get("backlog"):
                return self._api(f"/api/mesh/objects?{urllib.parse.urlencode(dict(params, backlog=1))}")
            if args.get("squad") or self.squad_id:
                params["squad"] = args.get("squad") or self.squad_id
            return self._api(f"/api/mesh/objects?{urllib.parse.urlencode(params)}")
        if name == "mesh_object":
            ref = str(args.get("ref") or "").strip()
            if not ref:
                raise ValueError("ref is required")
            return self._api(f"/api/mesh/object?{urllib.parse.urlencode({'ref': ref, 'log_limit': int(args.get('log_limit') or 20)})}")
        if name == "mesh_diff":
            ref = str(args.get("ref") or "").strip()
            if not ref:
                raise ValueError("ref is required")
            params = dict(ref=ref, **{k: str(args[k]) for k in ("path", "commit") if args.get(k)})
            if args.get("stat"):
                params["stat"] = 1
            return self._api(f"/api/mesh/diff?{urllib.parse.urlencode(params)}")
        if name == "mesh_file":
            payload = {k: args[k] for k in ("kind", "title", "body", "files", "owner", "parent", "status", "backlog",
                                            "roster", "spawn_role") if args.get(k)}
            for k in ("roster", "spawn_limit"):           # an empty roster or a limit of 0 still goes, to be checked
                if args.get(k) is not None:
                    payload[k] = args[k]
            if self.squad_id:
                payload["squad"] = self.squad_id
            return self._api("/api/mesh/object", method="POST", body=payload)
        if name == "mesh_log":
            payload = {k: args[k] for k in ("ref", "body", "kind", "status", "owner") if args.get(k)}
            if args.get("parent") is not None:            # '' clears it
                payload["parent"] = str(args["parent"])
            return self._api("/api/mesh/object/log", method="POST", body=payload)
        if name == "mesh_amend":
            ref = str(args.get("ref") or "").strip()
            if not ref:
                raise ValueError("ref is required")
            payload = dict(ref=ref, **{k: args[k] for k in ("title", "body", "files") if args.get(k) is not None})
            return self._api("/api/mesh/object/amend", method="POST", body=payload)
        if name == "mesh_respawn":
            ref = str(args.get("ref") or "").strip()
            if not ref:
                raise ValueError("ref is required")
            payload = dict(ref=ref, **{k: str(args[k]) for k in ("model", "effort") if args.get(k)})
            return self._api("/api/mesh/object/respawn", method="POST", body=payload, timeout=120.0)
        if name == "mesh_spawn":
            task, ref = str(args.get("task") or "").strip(), str(args.get("ref") or "").strip()
            if not task and not ref:
                raise ValueError("'task' is required (or 'ref': an existing queued, unowned task)")
            payload = {"task": task}
            if ref:
                payload["ref"] = ref
            if args.get("name"):
                payload["name"] = str(args["name"])
            if args.get("role"):
                payload["role"] = str(args["role"])
            if args.get("files"):
                payload["files"] = args["files"]
            payload.update({k: str(args[k]) for k in ("model", "effort", "why") if args.get(k)})
            return self._api("/api/mesh/spawn", method="POST", body=payload, timeout=120.0)
        if name == "mesh_wait":     # CLI-only (`--cli wait`, operator scripts), not in MCP_TOOLS: an idle agent is woken by its messages
            timeout_s = max(1.0, min(300.0, float(args.get("timeout_seconds") or 120.0)))
            since = int(args["since_id"]) if args.get("since_id") is not None else self._ensure_cursor()
            params = {"peer": self.self_name, "since_id": since, "timeout": timeout_s}
            if args.get("channel"):
                params["channel"] = str(args["channel"])
            if self.squad_id:
                params["squad"] = self.squad_id
            doc = self._api(f"/api/mesh/wait?{urllib.parse.urlencode(params)}", timeout=timeout_s + 10.0)
            msgs = doc.get("messages") or []
            if msgs:
                self.cursor_id = max(since, max(int(m["id"]) for m in msgs))
            return doc
        raise KeyError(f"Unknown tool: {name}")


def handle_rpc(msg: dict, client: MeshClient) -> dict | None:
    if not isinstance(msg, dict):
        return None
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "almanac-mesh", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {}
        try:
            if tname == "mesh_wait":    # not in MCP_TOOLS, but an agent may call it by name from habit
                raise KeyError("mesh_wait is not an agent tool: end your turn; a message to you starts your next one")
            out = client.call_tool(tname, targs)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}]},
            }
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(exc)}}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"isError": True, "content": [{"type": "text", "text": f"Error: {exc}"}]},
            }
    if req_id is not None:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def _resolve_auth(url_arg: str | None, token_arg: str | None, state_dir: Path | None = None) -> tuple[str, str]:
    url = url_arg or os.environ.get("ALMANAC_URL") or "http://127.0.0.1:8812"
    token = token_arg or os.environ.get("ALMANAC_TOKEN") or ""
    if not token:
        candidates = [
            Path(state_dir) / "token" if state_dir else None,
            Path(".almanac") / "token",
            Path(__file__).resolve().parents[1] / ".almanac" / "token",
        ]
        for c in candidates:
            if c and c.exists():
                token = c.read_text(encoding="utf-8").strip()
                if token:
                    break
    return url, token


def _run_cli(argv: list[str], client: MeshClient) -> int:
    sub = argparse.ArgumentParser(prog="python -m almanac.mcp_mesh --cli")
    sp = sub.add_subparsers(dest="cmd", required=True)
    p_roster = sp.add_parser("roster")
    p_roster.add_argument("--squad", dest="squad_id")

    p_ctx = sp.add_parser("context")
    p_ctx.add_argument("peer")
    p_ctx.add_argument("--limit", type=int, default=30)
    p_ctx.add_argument("--kinds", default="")

    p_con = sp.add_parser("contacts")
    p_con.add_argument("--peer")
    p_con.add_argument("--channel")
    p_con.add_argument("--squad", dest="squad_id")
    p_con.add_argument("--since-id", type=int)
    p_con.add_argument("--limit", type=int, default=50)

    p_send = sp.add_parser("send")
    p_send.add_argument("to")
    p_send.add_argument("message", nargs="+")
    p_send.add_argument("--channel")
    p_send.add_argument("--from", dest="from_name")
    p_send.add_argument("--needs", choices=("reply", "blocking"))
    p_send.add_argument("--re")

    p_spawn = sp.add_parser("spawn")
    p_spawn.add_argument("task", nargs="*")
    p_spawn.add_argument("--ref")
    p_spawn.add_argument("--name")
    p_spawn.add_argument("--role")
    p_spawn.add_argument("--files", nargs="*")
    p_spawn.add_argument("--model")
    p_spawn.add_argument("--effort")
    p_spawn.add_argument("--why")

    p_respawn = sp.add_parser("respawn")
    p_respawn.add_argument("ref")
    p_respawn.add_argument("--model")
    p_respawn.add_argument("--effort")

    p_objs = sp.add_parser("objects")
    for flag in ("--status", "--owner", "--q", "--kind", "--squad"):
        p_objs.add_argument(flag)
    p_objs.add_argument("--limit", type=int)
    p_objs.add_argument("--backlog", action="store_true")

    p_obj = sp.add_parser("object")
    p_obj.add_argument("ref")
    p_obj.add_argument("--log-limit", type=int, default=20)

    p_diff = sp.add_parser("diff")
    p_diff.add_argument("ref")
    p_diff.add_argument("--stat", action="store_true")
    p_diff.add_argument("--path")
    p_diff.add_argument("--commit")

    p_file = sp.add_parser("file")
    p_file.add_argument("title")
    p_file.add_argument("--body", default="")
    p_file.add_argument("--files", nargs="*")
    p_file.add_argument("--owner")
    p_file.add_argument("--parent")
    p_file.add_argument("--status")
    p_file.add_argument("--kind", default="task")
    p_file.add_argument("--backlog", action="store_true")
    p_file.add_argument("--roster", nargs="*", help="backlog: role or role:name, lead first")
    p_file.add_argument("--spawn-role")
    p_file.add_argument("--spawn-limit", type=int)

    p_log = sp.add_parser("log")
    p_log.add_argument("ref")
    p_log.add_argument("body", nargs="*")
    p_log.add_argument("--kind")
    p_log.add_argument("--status")
    p_log.add_argument("--owner")
    p_log.add_argument("--parent", help="a backlog task's parent; '' clears it")

    p_amend = sp.add_parser("amend")
    p_amend.add_argument("ref")
    p_amend.add_argument("--title")
    p_amend.add_argument("--body")
    p_amend.add_argument("--files", nargs="*")

    sp.add_parser("restart")

    p_wait = sp.add_parser("wait")
    p_wait.add_argument("--timeout", type=float, default=120.0)
    p_wait.add_argument("--channel")
    p_wait.add_argument("--since-id", type=int)

    ns = sub.parse_args(argv)
    if ns.cmd == "roster":
        res = client.call_tool("mesh_roster", {"squad_id": ns.squad_id})
    elif ns.cmd == "context":
        kinds = [k.strip() for k in ns.kinds.split(",") if k.strip()]
        res = client.call_tool("mesh_context_stream", {"peer": ns.peer, "limit": ns.limit, "kinds": kinds})
    elif ns.cmd == "contacts":
        res = client.call_tool(
            "mesh_contact_stream",
            {"peer": ns.peer, "channel": ns.channel, "squad_id": ns.squad_id, "since_id": ns.since_id, "limit": ns.limit},
        )
    elif ns.cmd == "spawn":
        res = client.call_tool("mesh_spawn", {"task": " ".join(ns.task), "ref": ns.ref, "name": ns.name, "role": ns.role,
                                              "files": ns.files, "model": ns.model, "effort": ns.effort, "why": ns.why})
    elif ns.cmd == "respawn":
        res = client.call_tool("mesh_respawn", {"ref": ns.ref, "model": ns.model, "effort": ns.effort})
    elif ns.cmd == "send":
        res = client.call_tool(
            "mesh_send",
            {"to": ns.to, "message": " ".join(ns.message), "channel": ns.channel, "from_name": ns.from_name,
             "needs": ns.needs, "re": ns.re},
        )
    elif ns.cmd == "restart":
        res = client.call_tool("mesh_restart", {})
    elif ns.cmd == "objects":
        res = client.call_tool("mesh_objects", {k: getattr(ns, k) for k in ("status", "owner", "q", "kind", "squad", "limit",
                                                                         "backlog")})
    elif ns.cmd == "object":
        res = client.call_tool("mesh_object", {"ref": ns.ref, "log_limit": ns.log_limit})
    elif ns.cmd == "diff":
        res = client.call_tool("mesh_diff", {"ref": ns.ref, "stat": ns.stat, "path": ns.path, "commit": ns.commit})
    elif ns.cmd == "file":
        res = client.call_tool("mesh_file", {"kind": ns.kind, "title": ns.title, "body": ns.body, "files": ns.files,
                                             "owner": ns.owner, "parent": ns.parent, "status": ns.status,
                                             "backlog": ns.backlog, "spawn_role": ns.spawn_role,
                                             "spawn_limit": ns.spawn_limit,
                                             "roster": [dict(zip(("role", "name"), m.split(":", 1)))
                                                        for m in ns.roster] if ns.roster is not None else None})
    elif ns.cmd == "log":
        res = client.call_tool("mesh_log", {"ref": ns.ref, "body": " ".join(ns.body), "kind": ns.kind,
                                            "status": ns.status, "owner": ns.owner, "parent": ns.parent})
    elif ns.cmd == "amend":
        res = client.call_tool("mesh_amend", {"ref": ns.ref, "title": ns.title, "body": ns.body, "files": ns.files})
    else:
        res = client.call_tool("mesh_wait", {"timeout_seconds": ns.timeout, "channel": ns.channel, "since_id": ns.since_id})
    print(json.dumps(res, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cli_args: list[str] | None = None
    if "--cli" in raw:
        idx = raw.index("--cli")
        cli_args = raw[idx + 1:]
        raw = raw[:idx]
    parser = argparse.ArgumentParser(description="Almanac cross-model mesh MCP server")
    parser.add_argument("--url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--self", dest="self_name", default=None)
    parser.add_argument("--squad", dest="squad_id", default=None)
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args(raw)
    url, token = _resolve_auth(args.url, args.token, args.state)
    client = MeshClient(url=url, token=token, self_name=args.self_name, squad_id=args.squad_id)
    if cli_args is not None:
        return _run_cli(cli_args, client)
    client._ensure_cursor()
    if hasattr(sys.stdin, "reconfigure"):       # MCP speaks UTF-8; a Windows pipe defaults to the locale's cp1252
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            req = json.loads(trimmed)
        except ValueError:
            continue
        resp = handle_rpc(req, client)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

