"""Cross-model agent & squad spawner (`almanac.spawner`): launches role-configured Claude (`claude.exe`)
and Gemini (`almanac.gemini_host` / `agy.exe`) agents wired into Almanac's `uds-messaging` mesh.

Each spawned agent receives:
  1. A deterministic peer identity (`agent_name`) registered in `~/.claude/sessions/`;
  2. Its Role Charter + live Squad/Mesh roster and protocol instructions;
  3. Almanac's `almanac-mesh` MCP server (`mesh_roster`, `mesh_context_stream`, `mesh_contact_stream`,
     `mesh_send`, `mesh_spawn`, `mesh_wait`) plus CLI fallback (`python -m almanac.mcp_mesh --cli ...`);
  4. Optional isolated Git worktree (`workspace_mode == "worktree"`) so parallel implementers never collide.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from almanac import gemini_host, resume, uds, web

AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REPO_ROOT = Path(__file__).resolve().parents[1]

CLAUDE_PERM_MAP = {
    "plan": "plan",
    "accept-edits": "acceptEdits",
    "auto": "auto",
    "bypass": "bypassPermissions",
    "manual": "manual",
}
CLAUDE_SHORT_ALIASES = {"opus", "sonnet", "haiku", "fable", "mythos", "opusplan", "default"}
CLAUDE_FAMILY_PREFIXES = ("opus-", "sonnet-", "haiku-", "fable-", "mythos-")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_ALIASES = {"low": "low", "med": "medium", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
_EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<eff>low|med|medium|high|xhigh|max)$", re.IGNORECASE)
GEMINI_MODEL_ALIASES = {
    "carbon": "gemini-4-carbon",
    "gemini-carbon": "gemini-4-carbon",
    "gemini carbon": "gemini-4-carbon",
    "gemini 4 carbon": "gemini-4-carbon",
    "argon": "gemini-4-argon",
    "gemini-argon": "gemini-4-argon",
    "gemini argon": "gemini-4-argon",
    "gemini 4 argon": "gemini-4-argon",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "gemini flash": "gemini-3.8-flash",
    "gemini 3.8 flash": "gemini-3.8-flash",
}


def normalize_effort(raw: str | None) -> str | None:
    """Normalize effort shorthand ('med' -> 'medium') into one of low|medium|high|xhigh|max."""
    val = str(raw or "").strip().lower()
    if not val:
        return None
    return EFFORT_ALIASES.get(val)


def normalize_model(provider: str | None, raw: str | None) -> str | None:
    """Normalize shorthand model names into clean base model IDs (without trailing effort suffixes)."""
    val = str(raw or "").strip()
    if not val:
        return None
    m_suf = _EFFORT_SUFFIX_RE.match(val)
    if m_suf:
        val = m_suf.group("base").strip()
    prov = (provider or "claude").strip().lower()
    low = val.lower()
    if prov == "gemini" and low in GEMINI_MODEL_ALIASES:
        return GEMINI_MODEL_ALIASES[low]
    if low in CLAUDE_SHORT_ALIASES:
        return low
    if prov == "claude" or low.startswith(("claude-", *CLAUDE_FAMILY_PREFIXES)):
        # Convert version dots like 5.5 -> 5-5 for Claude model IDs
        norm = re.sub(r"(?<=\d)\.(?=\d)", "-", val)
        if norm.lower().startswith(CLAUDE_FAMILY_PREFIXES):
            norm = "claude-" + norm
        return norm
    return GEMINI_MODEL_ALIASES.get(low, val)


def split_model_effort(
    provider: str | None,
    raw_model: str | None,
    raw_effort: str | None = None,
) -> tuple[str | None, str | None]:
    """Separate any embedded effort suffix (e.g. 'gemini-4-carbon-high' -> ('gemini-4-carbon', 'high')).
    Explicit `raw_effort` takes precedence when provided."""
    val = str(raw_model or "").strip()
    embedded_eff: str | None = None
    if val:
        m_suf = _EFFORT_SUFFIX_RE.match(val)
        if m_suf:
            embedded_eff = normalize_effort(m_suf.group("eff"))
            val = m_suf.group("base").strip()
    base = normalize_model(provider, val)
    eff = normalize_effort(raw_effort) or embedded_eff
    return base, eff


def slugify_name(raw: str, fallback: str = "agent") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(raw or "").strip()).strip("-_")[:48]
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"{fallback}-{cleaned}".strip("-_")[:48]
    return cleaned or fallback


def build_mesh_charter(
    *,
    agent_name: str,
    provider: str,
    role_title: str,
    role_prompt: str,
    almanac_url: str,
    state_dir: Path | None = None,
    squad_id: str | None = None,
    squad_name: str | None = None,
    channels: list[str] | None = None,
    teammates: list[dict] | None = None,
    python_exe: str = sys.executable,
    may_page_operator: bool = False,
    grow: dict | None = None,
    allowed_commands: list[str] | None = None,
    add_dirs: list[str] | None = None,
) -> str:
    """System charter giving every Claude and Gemini agent shared stream awareness and uds-messaging etiquette."""
    # no --state in the CLI line: with it, a CLI that lacks a token falls back to the door's master token file.
    # A spawned agent has ALMANAC_URL and its own ALMANAC_TOKEN in its environment, which the CLI reads first.
    del state_dir
    lines = [
        f"# Almanac Cross-Model Mesh Identity: `{agent_name}` ({provider.upper()})",
        f"- **Assigned Role**: {role_title}",
    ]
    if squad_id:
        lines.append(f"- **Squad**: {squad_name or squad_id} (`{squad_id}`)")
    if channels:
        lines.append(f"- **Subscribed Channels**: {', '.join(channels)}")
    lines += [
        "",
        "## Role Charter",
        role_prompt.strip(),
        "",
        "## Peer Context Streams & Contact Streams (`uds-messaging`)",
        "You are connected to Almanac's cross-model orchestration mesh alongside Claude and Gemini peers:",
        "1. **Discover Peers**: Call `mesh_roster` to inspect live teammates, their roles, models, folders, and busy/idle status.",
        "2. **Read Peer Context Streams**: Call `mesh_context_stream(peer=\"<name>\")` to read a teammate's live transcript tail (prompts, reasoning, tool calls, errors) before duplicating work or reviewing.",
        "3. **Read Contact Streams**: Call `mesh_contact_stream()` (or filter by `peer` / `channel`) to catch up on inter-agent decisions.",
        "4. **Communicate (`uds-messaging`)**: Call `mesh_send(to=\"<peer_name>|@squad|#channel\", message=\"...\")` (or Claude's native `SendMessage(to=\"<peer_name>\", ...)`). In Antigravity/Gemini, if `almanac-mesh` is lazy-loaded, call via `call_mcp_tool(ServerName=\"almanac-mesh_almanac-mesh\", ToolName=\"mesh_send\", Arguments={...})`. Inbound peer messages arrive wrapped in `<cross-session-message from-name=\"...\">`. "
        "When you need an answer, add `needs=\"reply\"` (wanted, not urgent) or `needs=\"blocking\"` (you cannot continue without it); the message arrives with a `[needs: ...]` line and stays open until its recipient replies to the sender directly. Answer any flagged message you receive with a direct `mesh_send` to its sender.",
        "5. **Wait When Blocked**: Call `mesh_wait(timeout_seconds=120)` when waiting on a teammate's reply or review.",
        f"6. **Shell CLI Fallback**: You can also run `\"{python_exe}\" -P -m almanac.mcp_mesh --self {agent_name} --cli <roster|context|contacts|send|wait|spawn> ...` (with `PYTHONPATH=\"{REPO_ROOT}\"`; your own token and Almanac's address come from the environment).",
        "7. **Operator**: `mesh_send(to=\"operator\", ...)` reaches the human running this squad; use it for decisions only they can make and for the final report; `needs=\"blocking\"` raises an alert on their screen, so use it only when the squad cannot proceed."
        if may_page_operator else
        "7. **Operator**: your role does not page the operator; route through a teammate whose role does. You may always answer a message the operator sends you.",
    ]
    if grow:
        what = ("`mesh_spawn(task=..., role=\"<role id>\")` adds one member of any defined role (`mesh_roster` lists the "
                "role ids)" if grow.get("any") else f"`mesh_spawn(task=...)` adds one {grow['role_title']}")
        lines.append(
            f"8. **Grow the squad**: {what} to this squad, up to {grow['limit']} in total "
            "(the operator sets it; `mesh_roster` shows what is left). Use it for slices that can run in parallel on separate "
            "files; give each a named slice and its files; you stay responsible for integrating their branches."
        )
    if allowed_commands and provider == "gemini":     # agy's allow rules; for Claude the role field is informational
        lines.append("- **Shell**: your runner only permits these exact command lines, typed character for character: "
                     + ", ".join(f"`{c}`" for c in allowed_commands) + "; anything else is denied, so do not try variants.")
    if add_dirs and provider == "gemini":             # agy `--add-dir`; Claude agents already read outside their cwd
        lines.append("- **Folders**: these folders are also part of your workspace: "
                     + ", ".join(f"`{d}`" for d in add_dirs) + ".")
    if teammates:
        lines += ["", "## Initial Squad Roster"]
        for tm in teammates:
            lines.append(
                f"- **`{tm['agent_name']}`** [{tm['provider'].upper()} · {tm.get('role_title') or tm.get('role_id') or 'peer'}]"
                + (f" — {tm['task']}" if tm.get("task") else "")
            )
            if tm.get("charter"):
                lines.append(f"  - Role: {' '.join(str(tm['charter']).split())}")
    return "\n".join(lines) + "\n"


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120,
                          creationflags=NO_WINDOW, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {_git_cause((proc.stderr or proc.stdout).decode('utf-8', 'replace'))}")
    return proc.stdout.decode("utf-8", "replace").strip()


def _git_cause(text: str, limit: int = 240) -> str:
    """git's error output with the cause first: `fatal:` / `error:` / `The following paths…` lines lead, hints and
    line-ending warnings (which come first and bury it) drop out, and what is kept is cut from the end."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cause = [ln for ln in lines if ln.startswith(("fatal:", "error:", "The following paths"))]
    rest = [ln for ln in lines if ln not in cause and not ln.startswith(("warning:", "hint:"))]
    out = " | ".join(cause + rest) or " | ".join(lines)
    return out if len(out) <= limit else "…" + out[-limit:]


def _ignored(top: Path, rel: str) -> bool:
    """True when git already ignores `rel` (exit 0). A pathspec that names an ignored path, even as an exclude,
    makes `git add` refuse with exit 1, so such a path must not be named."""
    proc = subprocess.run(["git", "-C", str(top), "check-ignore", "-q", "--", rel], capture_output=True, timeout=30,
                          creationflags=NO_WINDOW)
    return proc.returncode == 0


SNAPSHOT_SKIP = (":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*")   # secrets stay out of an agent's branch


def snapshot_commit(top: Path, state_dir: Path | None = None) -> str:
    """HEAD plus the checkout's uncommitted changes and untracked, non-ignored files, as one commit (HEAD itself
    when there are none). Built in a throwaway index, so the checkout's index, working tree and stash are untouched.
    Never includes `.env` files or Almanac's own state folder (token, database, worktrees) when it sits inside."""
    skip = list(SNAPSHOT_SKIP)          # glob excludes are safe even when they match only ignored files
    if state_dir is not None and Path(state_dir).resolve().is_relative_to(top.resolve()):
        rel = Path(state_dir).resolve().relative_to(top.resolve()).as_posix()
        if rel not in ("", ".") and not _ignored(top, rel):     # an ignored state folder is left out by git already
            skip.append(":(exclude,literal)" + rel)
    head = _git(top, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        real = top / _git(top, "rev-parse", "--git-path", "index")
        if real.exists():
            shutil.copyfile(real, index)    # a copy keeps git's stat cache, so `add -A` only hashes what changed
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        _git(top, "add", "-A", "--", ".", *skip, env=env)
        tree = _git(top, "write-tree", env=env)
    if tree == _git(top, "rev-parse", "HEAD^{tree}"):
        return head
    who = dict(GIT_AUTHOR_NAME="Almanac", GIT_AUTHOR_EMAIL="almanac@localhost",
               GIT_COMMITTER_NAME="Almanac", GIT_COMMITTER_EMAIL="almanac@localhost")
    return _git(top, "commit-tree", tree, "-p", head, "-m", "almanac: uncommitted work at spawn",
                env=dict(os.environ, **who))


def ensure_worktree(repo_cwd: Path, state_dir: Path, agent_name: str, squad_id: str | None = None) -> Path:
    """Create or reuse an isolated git worktree under `<state>/worktrees/[<squad_id>/]<agent_name>`, branched from
    a snapshot of the checkout as it is now (uncommitted work included), so the agent's diff is only its own."""
    wt_root = Path(state_dir).resolve() / "worktrees"
    if squad_id:
        wt_root = wt_root / squad_id
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_dir = wt_root / agent_name
    if wt_dir.is_dir() and (wt_dir / ".git").exists():
        return wt_dir
    branch = f"almanac/{squad_id}/{agent_name}" if squad_id else f"almanac/{agent_name}"
    check = subprocess.run(
        ["git", "-C", str(repo_cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        timeout=10,
        creationflags=NO_WINDOW,
    )
    if check.returncode != 0:
        # Not a git repo: fall back cleanly to the shared folder rather than failing squad launch
        return repo_cwd
    top = Path(check.stdout.decode("utf-8", "replace").strip())
    try:
        _git(top, "rev-parse", "--verify", "-q", "HEAD")
    except RuntimeError:
        return repo_cwd                     # no commit yet: nothing to branch from, so work in the folder itself
    try:
        _git(top, "worktree", "add", "-B", branch, str(wt_dir), snapshot_commit(top, state_dir))
    except RuntimeError as exc:
        raise RuntimeError(f"worktree for {agent_name}: {exc}") from exc
    return wt_dir


def mesh_args(agent_name: str, almanac_url: str, token: str, squad_id: str | None = None) -> list[str]:
    """argv after the interpreter for the mesh MCP server. `-P`: an agent's cwd may be a worktree holding an older
    `almanac/` package, which `-m` would otherwise import instead of PYTHONPATH's."""
    args = ["-P", "-m", "almanac.mcp_mesh", "--self", agent_name, "--url", almanac_url, "--token", token]
    return args + (["--squad", squad_id] if squad_id else [])


def stage_agy_mesh_bundle(
    bundle_root: Path,
    *,
    agent_name: str,
    almanac_url: str,
    token: str,
    squad_id: str | None = None,
    python_exe: str = sys.executable,
) -> Path:
    """Write `<bundle_root>/.agents/plugins/almanac-mesh/` so `agy --add-dir` discovers Mesh MCP tools. One bundle
    per agent: its config carries that agent's own token and name."""
    bundle_root = Path(bundle_root).resolve()
    args = mesh_args(agent_name, almanac_url, token, squad_id)
    plugin_dir = bundle_root / ".agents" / "plugins" / "almanac-mesh"
    rules_dir = plugin_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (bundle_root / ".git").mkdir(exist_ok=True)

    plugin_manifest = {
        "name": "almanac-mesh",
        "displayName": "Almanac Cross-Model Mesh",
        "version": "1.0.0",
        "description": "Shared context streams, contact streams, and uds-messaging across Claude and Gemini agents.",
    }
    mcp_cfg = {
        "mcpServers": {
            "almanac-mesh": {
                "command": python_exe,
                "args": args,
                "env": {
                    "PYTHONPATH": str(REPO_ROOT),
                    "ALMANAC_URL": almanac_url,
                    "ALMANAC_TOKEN": token,
                },
            }
        }
    }
    rules_md = (
        "---\nname: almanac-mesh\ndescription: Cross-model peer context/contact streams and uds-messaging.\n---\n\n"
        "Use `mesh_roster`, `mesh_context_stream`, `mesh_contact_stream`, `mesh_send`, and `mesh_wait` "
        "(via `call_mcp_tool` with `ServerName=\"almanac-mesh_almanac-mesh\"` or `python -m almanac.mcp_mesh --cli`) "
        "to inspect Claude and Gemini teammates' live streams and coordinate over `uds-messaging`.\n"
    )
    (plugin_dir / "plugin.json").write_text(json.dumps(plugin_manifest, indent=2) + "\n", encoding="utf-8")
    (plugin_dir / "mcp_config.json").write_text(json.dumps(mcp_cfg, indent=2) + "\n", encoding="utf-8")
    (rules_dir / "AGENTS.md").write_text(rules_md, encoding="utf-8")
    return bundle_root


def write_claude_mcp_config(
    target_path: Path,
    *,
    agent_name: str,
    almanac_url: str,
    token: str,
    squad_id: str | None = None,
    python_exe: str = sys.executable,
) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    args = mesh_args(agent_name, almanac_url, token, squad_id)
    cfg = {
        "mcpServers": {
            "almanac-mesh": {
                "command": python_exe,
                "args": args,
                "env": {
                    "PYTHONPATH": str(REPO_ROOT),
                    "ALMANAC_AGENT_NAME": agent_name,
                    "ALMANAC_URL": almanac_url,
                    "ALMANAC_TOKEN": token,
                },
            }
        }
    }
    target_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return target_path


def spawn_agent(
    service,
    *,
    almanac_url: str,
    token: str,
    cwd: str,
    agent_name: str | None = None,
    role_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    workspace_mode: str | None = None,
    permission_mode: str | None = None,
    system_prompt: str | None = None,
    initial_prompt: str | None = None,
    channels: list[str] | None = None,
    squad_id: str | None = None,
    squad_name: str | None = None,
    teammates: list[dict] | None = None,
    remote: bool = False,
    headless: bool = False,
    watch: float = 0.6,
    python_exe: str = sys.executable,
    spawned_by: str | None = None,
) -> dict:
    """Spawn one Claude or Gemini agent, register it in Almanac, and return its launch metadata. `spawned_by`: the
    agent that added it to a running squad (None: the operator)."""
    if service.state is None:
        raise ValueError("Almanac state directory is required to spawn agents")
    role = service.store.role(role_id) if role_id else None
    if role_id and role is None:
        raise ValueError(f"unknown role {role_id!r}")

    eff_provider = (provider or (role["provider"] if role else "claude")).strip().lower()
    if eff_provider not in ("claude", "gemini"):
        raise ValueError("provider must be 'claude' or 'gemini'")

    base_name = agent_name or (f"{role_id}-{secrets.token_hex(2)}" if role_id else f"{eff_provider}-{secrets.token_hex(2)}")
    eff_name = slugify_name(base_name, fallback=eff_provider)
    if not AGENT_NAME_RE.match(eff_name):
        raise ValueError("agent_name must be 1..48 letters, digits, '-' or '_'")
    if eff_name.lower() in web.RESERVED_AGENT_NAMES:
        raise ValueError(f"{eff_name!r} is a reserved name (the operator's, the hub's or a mesh address); pick another")
    if eff_provider == "claude":
        from almanac import config
        runner = service.runners()["claude"] if hasattr(service, "runners") else {}
        if runner.get("reason") or config.is_shim(service.claude):
            # a batch file gets its command line re-parsed by cmd.exe: the charter and an agent-written task would
            # be cut at newlines and their `&` would run commands
            return dict(ok=False, pid=None, agent_name=eff_name, session_id=None, provider=eff_provider, role_id=role_id,
                        error=runner.get("reason") or f"{service.claude} is a .cmd / .bat shim: Almanac will not launch "
                                                      "it; point \"claude\" at claude.exe")
    if eff_provider == "gemini" and hasattr(service, "runners"):
        runner = service.runners()["gemini"]
        if not runner["ok"]:
            # refused like a launch that died: a squad still starts its other members, and nothing is recorded
            return dict(ok=False, pid=None, agent_name=eff_name, session_id=None, provider=eff_provider, role_id=role_id,
                        error=f"Gemini runner not found; looked in: {'; '.join(runner['searched'])}; "
                              "set \"gemini\" in config.json")
    token = web.agent_token(token, eff_name)    # the agent's own bearer: names it, opens /api/mesh only

    repo_dir = Path(cwd).expanduser().resolve()
    if not repo_dir.is_dir():
        raise ValueError(f"folder does not exist: {cwd}")

    raw_model = (model if model is not None else (role["model"] if role else "")).strip() or None
    raw_effort = (effort if effort is not None else (role["effort"] if role else "")).strip() or None
    eff_model, eff_effort = split_model_effort(eff_provider, raw_model, raw_effort)
    eff_wmode = (workspace_mode or (role["workspace_mode"] if role else "shared")).strip().lower()
    eff_pmode = (permission_mode or (role["permission_mode"] if role else "auto")).strip()
    eff_role_title = role["title"] if role else (role_id or f"{eff_provider.title()} Agent")
    eff_role_prompt = (system_prompt if system_prompt is not None else (role["system_prompt"] if role else "Collaborate with peers to complete the user's goal.")).strip()
    eff_channels = list(channels if channels is not None else (role["channels"] if role else ["#squad"]))

    worktree_path: Path | None = None
    run_cwd = repo_dir
    if eff_wmode == "worktree":
        wt = ensure_worktree(repo_dir, service.state, eff_name, squad_id)
        if wt != repo_dir:
            worktree_path = wt
            run_cwd = wt

    session_id = str(uuid.uuid4())
    spawn_dir = service.state / "spawns" / f"{eff_name}-{session_id[:8]}"
    spawn_dir.mkdir(parents=True, exist_ok=True)

    charter = build_mesh_charter(
        agent_name=eff_name,
        provider=eff_provider,
        role_title=eff_role_title,
        role_prompt=eff_role_prompt,
        almanac_url=almanac_url,
        state_dir=service.state,
        squad_id=squad_id,
        squad_name=squad_name,
        channels=eff_channels,
        teammates=teammates,
        python_exe=python_exe,
        may_page_operator=bool(role is None or role.get("may_page_operator")),   # role-less agents may page (door.may_page_operator)
        grow=_grow_line(service, role, squad_id),
        allowed_commands=(role or {}).get("allowed_commands") or [],
        add_dirs=(role or {}).get("add_dirs") or [],
    )
    charter_file = spawn_dir / "charter.md"
    charter_file.write_text(charter, encoding="utf-8")

    env = resume.clean_env(dict(os.environ))
    env.update(
        PYTHONPATH=str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
        ALMANAC_AGENT_NAME=eff_name,
        ALMANAC_URL=almanac_url,
        ALMANAC_TOKEN=token,
    )
    if squad_id:
        env["ALMANAC_SQUAD_ID"] = squad_id

    flags = NO_WINDOW if headless else NEW_CONSOLE

    if eff_provider == "claude":
        mcp_path = write_claude_mcp_config(
            spawn_dir / "mcp.json",
            agent_name=eff_name,
            almanac_url=almanac_url,
            token=token,
            squad_id=squad_id,
            python_exe=python_exe,
        )
        settings_path = resume.settings_file(service.state)
        argv = [
            service.claude,
            "--settings",
            str(settings_path),
            "--session-id",
            session_id,
            "-n",
            eff_name,
            "--mcp-config",
            str(mcp_path),
            "--allowedTools=mcp__almanac-mesh__*,SendMessage",
            "--append-system-prompt",
            charter,
        ]
        if eff_model:
            argv += ["--model", eff_model]
        if eff_effort:
            argv += ["--effort", eff_effort]
        c_perm = CLAUDE_PERM_MAP.get(eff_pmode)
        if c_perm:
            argv += ["--permission-mode", c_perm]
        if remote:
            argv += ["--remote-control", eff_name[:60]]
        debug_log = spawn_dir / "claude-debug.log"
        argv += ["--debug-file", str(debug_log)]
        if headless:
            argv += ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            out_fh = (spawn_dir / "claude-stream.jsonl").open("a", encoding="utf-8")
            err_fh = (spawn_dir / "claude-stderr.log").open("a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(run_cwd),
                    creationflags=flags,
                    stdin=subprocess.PIPE,
                    stdout=out_fh,
                    stderr=err_fh,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    startupinfo=subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None,
                    env=env,
                )
            finally:
                out_fh.close()
                err_fh.close()
            # Keep stdin open so headless Claude stays alive listening on its uds-messaging Named Pipe
            service.__dict__.setdefault("headless_procs", {})[session_id] = proc
            if initial_prompt and initial_prompt.strip() and proc.stdin is not None:
                try:
                    proc.stdin.write(
                        json.dumps({"type": "user", "message": {"role": "user", "content": initial_prompt.strip()}}) + "\n"
                    )
                    proc.stdin.flush()
                except OSError:
                    pass
        else:
            if initial_prompt and initial_prompt.strip():
                argv.append(initial_prompt.strip())
            proc = subprocess.Popen(
                argv,
                cwd=str(run_cwd),
                creationflags=flags,
                close_fds=True,
                stdin=None,
                stdout=None,
                stderr=None,
                startupinfo=subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None,
                env=env,
            )
        log_file = debug_log
    else:
        bundle_dir = stage_agy_mesh_bundle(
            spawn_dir / "agy-bundle", agent_name=eff_name, almanac_url=almanac_url, token=token,
            squad_id=squad_id, python_exe=python_exe,
        )
        proj = gemini_host.project_slug(str(run_cwd))
        tx_path = service.gemini_transcripts / proj / f"{session_id}.jsonl"
        inbox_path = service.state / "inboxes" / f"{eff_name}.jsonl"
        sessions_dir = (service.fleet.home if service.fleet else (Path.home() / ".claude")) / "sessions"
        argv = [
            python_exe,
            "-m",
            "almanac.gemini_host",
            "--name",
            eff_name,
            "--session-id",
            session_id,
            "--cwd",
            str(run_cwd),
            "--transcript",
            str(tx_path),
            "--sessions-dir",
            str(sessions_dir),
            "--agy",
            service.gemini,
            "--permission-mode",
            eff_pmode,
            "--system-prompt-file",
            str(charter_file),
            "--inbox",
            str(inbox_path),
            "--add-dir",
            str(bundle_dir),
        ]
        for folder in (role or {}).get("add_dirs") or []:
            argv += ["--add-dir", folder]
        if eff_model:
            argv += ["--model", eff_model]
        if eff_effort:
            argv += ["--effort", eff_effort]
        if role_id:
            argv += ["--role", role_id]
        if squad_id:
            argv += ["--squad", squad_id]
        for ch in eff_channels:
            argv += ["--channel", ch]
        for cmd in (role or {}).get("allowed_commands") or []:
            argv += ["--allow-command", cmd]
        if initial_prompt and initial_prompt.strip():
            init_file = spawn_dir / "initial_prompt.md"
            init_file.write_text(initial_prompt.strip(), encoding="utf-8")
            argv += ["--initial-prompt-file", str(init_file)]
        log_file = spawn_dir / "gemini-host.log"
        argv += ["--log-file", str(log_file)]
        if headless:
            argv.append("--headless")
        proc = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            creationflags=flags,
            close_fds=True,
            stdin=None,
            stdout=None,
            stderr=None,
            startupinfo=subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None,
            env=env,
        )

    deadline = time.monotonic() + max(0.0, watch)
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            reason = resume.last_error(log_file) if log_file.exists() else f"process exited at once (code {code})"
            return dict(ok=False, pid=proc.pid, agent_name=eff_name, session_id=session_id, provider=eff_provider, error=reason)
        time.sleep(0.05)

    service.launched[session_id] = proc.pid
    rec = service.store.record_spawn(
        agent_name=eff_name,
        session_id=session_id,
        squad_id=squad_id,
        role_id=role_id,
        provider=eff_provider,
        model=eff_model,
        effort=eff_effort,
        cwd=str(run_cwd),
        worktree_path=str(worktree_path) if worktree_path else None,
        pid=proc.pid,
        channels=eff_channels,
        spawned_by=spawned_by,
    )
    with service.fleet_lock:
        if service.fleet:
            service.fleet.last["at"] = 0
    service.wake.set()                      # read transcripts on the short interval from now, not after a long sleep
    mode_note = None
    if eff_provider == "gemini":                      # if agy already said it ignores the asked mode, say so now
        sessions = (service.fleet.home if service.fleet else (Path.home() / ".claude")) / "sessions"
        mode_note = next((p.get("mode_note") for p in uds.discover_peers(sessions, alive_fn=lambda _: True)
                          if p.get("pid") == proc.pid and p.get("mode_note")), None)
    return dict(ok=True, **rec, role_title=eff_role_title, channels=eff_channels, mode_note=mode_note)


def launch_squad(
    service,
    *,
    almanac_url: str,
    token: str,
    name: str,
    cwd: str,
    brief: str,
    members: list[dict],
    watch: float = 0.3,
    spawn_role: str | None = None,
    spawn_limit: int = 0,
) -> dict:
    """Spawn a multi-agent cross-model squad with pre-wired peer rosters and a shared kickoff brief.
    `spawn_role` / `spawn_limit`: what an agent whose role may_spawn can add later (see `join_squad`)."""
    if not isinstance(members, list) or not members:
        raise ValueError("members must be a non-empty list of agent specs")
    if len(members) > 20:
        raise ValueError("a squad launches at most 20 agents at once")
    repo_dir = Path(cwd).expanduser().resolve()
    if not repo_dir.is_dir():
        raise ValueError(f"folder does not exist: {cwd}")

    squad_slug = slugify_name(name or "squad", fallback="squad")[:24]
    squad_id = f"{squad_slug}-{secrets.token_hex(3)}"
    squad_doc = service.store.create_squad(squad_id, name or squad_slug, str(repo_dir), brief or "",
                                           spawn_role=spawn_role, spawn_limit=spawn_limit)

    # Pre-assign deterministic unique names so every agent gets the complete teammate roster at launch
    prepared: list[dict] = []
    used_names: set[str] = set()
    for idx, spec in enumerate(members, start=1):
        if not isinstance(spec, dict):
            raise ValueError("each squad member must be an object")
        role_id = spec.get("role_id")
        role = service.store.role(role_id) if role_id else None
        prov = (spec.get("provider") or (role["provider"] if role else "claude")).strip().lower()
        raw_name = spec.get("agent_name") or spec.get("name") or (f"{role_id}" if role_id else f"{prov}-{idx}")
        candidate = slugify_name(raw_name, fallback=f"{prov}-{idx}")
        if candidate in used_names:
            candidate = f"{candidate}-{idx}"
        used_names.add(candidate)
        prepared.append(
            dict(
                spec=spec,
                agent_name=candidate,
                provider=prov,
                role_id=role_id,
                role_title=role["title"] if role else (role_id or prov),
                task=str(spec.get("task") or "").strip(),
                charter=spec.get("system_prompt") or (role["system_prompt"] if role else ""),
            )
        )

    roster_preview = [
        dict(
            agent_name=p["agent_name"],
            provider=p["provider"],
            role_id=p["role_id"],
            role_title=p["role_title"],
            task=p["task"],
            charter=p["charter"],
        )
        for p in prepared
    ]

    launched_agents: list[dict] = []
    for p in prepared:
        spec = p["spec"]
        kickoff_parts = []
        if brief and brief.strip():
            kickoff_parts.append(f"## Squad Mission ({name or squad_slug})\n{brief.strip()}")
        if p["task"]:
            kickoff_parts.append(f"## Your Initial Assignment (`{p['agent_name']}`)\n{p['task']}")
        if kickoff_parts:
            kickoff_parts.append(
                "Coordinate with your squadmates via `mesh_send`, check `mesh_roster` and `mesh_context_stream` "
                "as teammates make progress, and post major milestones to `#squad`."
            )
        initial_prompt = spec.get("initial_prompt") or ("\n\n".join(kickoff_parts) if kickoff_parts else None)
        try:
            res = spawn_agent(
                service,
                almanac_url=almanac_url,
                token=token,
                cwd=str(repo_dir),
                agent_name=p["agent_name"],
                role_id=p["role_id"],
                provider=p["provider"],
                model=spec.get("model"),
                effort=spec.get("effort"),
                workspace_mode=spec.get("workspace_mode"),
                permission_mode=spec.get("permission_mode"),
                system_prompt=spec.get("system_prompt"),
                initial_prompt=initial_prompt,
                channels=spec.get("channels"),
                squad_id=squad_id,
                squad_name=name or squad_slug,
                teammates=[r for r in roster_preview if r["agent_name"] != p["agent_name"]],
                remote=bool(spec.get("remote", False)),
                headless=bool(spec.get("headless", False)),
                watch=watch,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            # one member that cannot start (a worktree git refuses, a bad role) must not orphan the ones already running
            res = dict(ok=False, pid=None, agent_name=p["agent_name"], session_id=None, provider=p["provider"],
                       role_id=p["role_id"], error=str(exc))
        launched_agents.append(res)

    if brief and brief.strip():
        service.store.record_message(
            from_name="operator",
            to_name="@squad",
            channel="#squad",
            squad_id=squad_id,
            body=f"[Squad {name or squad_slug} launched]\n{brief.strip()}",
            transport="spawn",
        )

    ok_count = sum(1 for a in launched_agents if a.get("ok"))
    return dict(
        ok=ok_count > 0,
        squad=dict(squad_doc, agents=launched_agents),
        launched=ok_count,
        failed=len(launched_agents) - ok_count,
    )


# ---------------------------------------------------------------------- elastic squads: adding a member to a running squad

ROLE_SHORT = {"architect": "arch", "implementer": "impl", "reviewer": "rev"}


def _grow_line(service, role: dict | None, squad_id: str | None) -> dict | None:
    """Charter item 8 for a role that may add members, in a squad that lets it (limit above 0, a spawn role set)."""
    if not role or not role.get("may_spawn") or not squad_id or not hasattr(service.store, "squad"):
        return None
    squad = service.store.squad(squad_id)
    if not squad or not squad.get("spawn_limit"):
        return None
    if squad.get("spawn_role") == "*":
        return dict(role_title=None, any=True, limit=squad["spawn_limit"])
    target = service.store.role(squad["spawn_role"]) if squad.get("spawn_role") else None
    return dict(role_title=target["title"], limit=squad["spawn_limit"]) if target else None


def free_handle(service, squad: dict, role: dict) -> str:
    """The role's short form + provider (`impl-claude`), numbered from 2 when taken. Never a handle this squad has
    used, nor one live anywhere on the machine (peers are addressed by name)."""
    used = {a["agent_name"] for a in squad.get("agents", [])} | {p.get("name") for p in service.mesh_peers()}
    first = role["id"].split("-")[0]
    base = slugify_name(f"{ROLE_SHORT.get(first, first[:6] or 'agent')}-{role['provider']}")
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def join_squad(
    service,
    *,
    almanac_url: str,
    token: str,
    squad_id: str,
    role_id: str,
    task: str,
    agent_name: str | None = None,
    spawned_by: str | None = None,
    watch: float = 0.6,
    **operator_overrides,
) -> dict:
    """Add one member to a running squad: the one path for the operator's [+ ADD MEMBER] and an agent's mesh_spawn.
    The newcomer's charter lists the squad as it is now, its kickoff carries the brief, its task and whom to report
    to, and on success the squad hears who joined. `operator_overrides` (model, effort, workspace_mode,
    permission_mode) come only from the operator; the door never passes them for an agent."""
    squad = service.store.squad(squad_id)
    if squad is None:
        raise ValueError(f"no squad {squad_id!r}")
    if squad.get("status") != "active":
        raise ValueError(f"squad {squad_id} is {squad.get('status')}")
    role = service.store.role(role_id) if role_id else None
    if role is None:
        raise ValueError(f"unknown role {role_id!r}")
    task = str(task or "").strip()
    live = {p.get("name") for p in service.mesh_peers()}
    if agent_name:
        name = slugify_name(agent_name, fallback=role["provider"])
        if name in live:
            raise ValueError(f"{name} is already live; give another name or leave it out")
        if name in {a["agent_name"] for a in squad.get("agents", [])}:
            raise ValueError(f"{name} was already a member of squad {squad_id}; its worktree, branch and token are "
                             "that agent's. Give another name or leave it out")
    else:
        name = free_handle(service, squad, role)
    newest: dict[str, dict] = {}
    for a in squad.get("agents", []):
        newest[a["agent_name"]] = a                      # agents come oldest first: the newest row per handle wins
    teammates = []
    for a in newest.values():
        r = service.store.role(a["role_id"]) if a.get("role_id") else None
        teammates.append(dict(agent_name=a["agent_name"], provider=a["provider"], role_id=a.get("role_id"),
                              role_title=r["title"] if r else (a.get("role_id") or a["provider"]),
                              charter=r["system_prompt"] if r else ""))
    who = spawned_by or "operator"
    # whom the newcomer reports to must be someone it may reach: the agent that added it, else the operator only if
    # its role may page the operator, else the live squadmates whose role may (a join notice gives no reply pass)
    if spawned_by:
        report_to = f"`{spawned_by}`"
    elif role.get("may_page_operator"):
        report_to = "the operator (`mesh_send(to=\"operator\")`)"
    else:
        pagers = {r["id"] for r in service.store.roles() if r.get("may_page_operator")}
        relays = sorted({p["name"] for p in service.mesh_peers(squad_id=squad_id) if p.get("agent_role") in pagers})
        report_to = (" or ".join(f"`{r}`" for r in relays) if relays
                     else "`#squad` (`mesh_send(to=\"#squad\")`), since nobody live in the squad may page the operator")
    kickoff = []
    if (squad.get("brief") or "").strip():
        kickoff.append(f"## Squad Mission ({squad['name']})\n{squad['brief'].strip()}")
    kickoff.append(f"## Your Initial Assignment (`{name}`)\n{task or '(none given: ask whoever added you)'}")
    kickoff.append(f"You were added to the running squad by {who}. Report to {report_to} over `mesh_send` when your slice "
                   "is done: files touched, the test command and its result, anything you could not do.")
    wmode = (operator_overrides.get("workspace_mode") or role.get("workspace_mode") or "shared").strip().lower()
    if wmode == "worktree":
        kickoff.append(f"You work in your own git worktree on branch `almanac/{squad_id}/{name}`, cut from a snapshot of "
                       "the checkout at spawn time. Commit your work on that branch, and name the branch in your report.")
    kickoff.append("Coordinate with your squadmates via `mesh_send`, check `mesh_roster` and `mesh_context_stream` as "
                   "teammates make progress, and post major milestones to `#squad`.")
    res = spawn_agent(
        service,
        almanac_url=almanac_url,
        token=token,
        cwd=squad["cwd"],
        agent_name=name,
        role_id=role["id"],
        provider=role["provider"],
        model=operator_overrides.get("model"),
        effort=operator_overrides.get("effort"),
        workspace_mode=operator_overrides.get("workspace_mode"),
        permission_mode=operator_overrides.get("permission_mode"),
        initial_prompt="\n\n".join(kickoff),
        squad_id=squad_id,
        squad_name=squad["name"],
        teammates=teammates,
        watch=watch,
        spawned_by=spawned_by,
    )
    if res.get("ok"):
        try:
            # not to the newcomer: it would arrive as a second prompt right after its kickoff and restart its turn
            service.mesh_send(to="@squad", squad_id=squad_id, from_name=who,
                              text=f"[{name} joined as {role['title']}, added by {who}] {task[:300]}",
                              exclude={name, res.get("session_id")} - {None})
        except (ValueError, OSError):
            pass                                         # the spawn stands even if nobody could be told
    return res
