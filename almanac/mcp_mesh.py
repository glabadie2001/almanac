"""Almanac Mesh MCP server & CLI: gives every Claude and Gemini agent live visibility into peer context streams,
inter-agent contact streams, and two-way `uds-messaging` communication.

Stdio MCP mode (configured automatically by `almanac.spawner`):
    python -m almanac.mcp_mesh --url http://127.0.0.1:8812 --token <token> [--self <agent_name>]

CLI mode (usable from any agent shell or script):
    python -m almanac.mcp_mesh --cli roster
    python -m almanac.mcp_mesh --cli context <peer> [--limit 30]
    python -m almanac.mcp_mesh --cli contacts [--peer <name>] [--channel <#chan>]
    python -m almanac.mcp_mesh --cli send <to> <message...> [--channel <#chan>]
    python -m almanac.mcp_mesh --cli wait [--timeout 120]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
            "'@squad' (your squad), '@all' (every live session), or a '#channel'."
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
                "from_name": {
                    "type": "string",
                    "description": "Optional sender name override (defaults to your registered agent name).",
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "mesh_wait",
        "description": (
            "Wait until a new cross-session message arrives for this agent (or on an optional channel) "
            "and return the newly arrived messages immediately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning (default: 120, max: 300).",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional channel filter.",
                },
                "since_id": {
                    "type": "integer",
                    "description": "Optional message cursor ID; defaults to this session's last seen message ID.",
                },
            },
        },
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


class MeshClient:
    """Executes mesh operations against Almanac's HTTP API (or an injected callable in tests)."""

    def __init__(self, *, url: str, token: str, self_name: str | None = None, squad_id: str | None = None, api_fn=None):
        self.url = url
        self.token = token
        self.self_name = infer_self_name(self_name)
        self.squad_id = squad_id or os.environ.get("ALMANAC_SQUAD_ID") or None
        self._api = api_fn or (lambda path, method="GET", body=None, timeout=30.0: http_api(self.url, self.token, path, method=method, body=body, timeout=timeout))
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
            }
            return self._api("/api/mesh/send", method="POST", body=payload)
        if name == "mesh_wait":
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
    elif ns.cmd == "send":
        res = client.call_tool(
            "mesh_send",
            {"to": ns.to, "message": " ".join(ns.message), "channel": ns.channel, "from_name": ns.from_name},
        )
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

