"""Almanac's own record of every Claude Code session on this PC, kept after Claude Code prunes the transcripts.

Ingest reads `~/.claude/projects/**/*.jsonl` from where it last stopped (each file is append-only; a file that
shrank is read again from the top, and every row is keyed so a re-read is a no-op). From each transcript it keeps:

* `calls`  — one row per tool call: tool, time, for a shell call its family, leading word, length and adoption
  scores, and once the result arrives its size (chars of text read back) and the wait in seconds;
* `usage`  — one row per assistant message: model and token counts (input, cache creation, cache read, output);
* `sessions` — first/last time, project folder, sidechain or not.

`seed_legacy` loads an inventory's call list (tool, command, time; no sizes or waits) so the weeks
before Almanac existed stay on the chart after their transcripts are gone. Live rows win: both share a natural
key of (timestamp, tool, command).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from almanac import classify, uds

SCHEMA = """
create table if not exists files (path text primary key, size integer, mtime_ns integer, offset integer);
create table if not exists sessions (id text primary key, project text, first_ts real, last_ts real,
                                     sidechain integer default 0, version text);
create table if not exists calls (natural text primary key, tool_use_id text unique, session text, ts real,
                                  tool text, family text, head text, cmd_len integer,
                                  search integer default 0, tests integer default 0, http integer default 0,
                                  edit integer default 0, out_chars integer, seconds real, is_error integer,
                                  source text default 'live');
create index if not exists calls_ts on calls(ts);
create table if not exists usage (message_id text primary key, session text, ts real, model text,
                                  input integer, cache_create integer, cache_read integer, output integer);
create index if not exists usage_ts on usage(ts);
create table if not exists meta (key text primary key, value text);
create table if not exists turns (session text not null, start_ts real not null, end_ts real not null, kind text,
                                  primary key (session, start_ts));
create table if not exists actions (id integer primary key autoincrement, ts real, action text, pid integer,
                                    session text, name text, ok integer, detail text, who text);
create table if not exists messages (
    id integer primary key autoincrement,
    msg_id text unique,
    ts real not null,
    from_name text not null,
    from_session text,
    from_provider text,
    to_name text not null,
    to_session text,
    to_provider text,
    channel text,
    squad_id text,
    body text not null,
    transport text default 'uds'
);
create index if not exists messages_ts on messages(ts);
create index if not exists messages_peers on messages(from_name, to_name);
create table if not exists roles (
    id text primary key,
    title text not null,
    provider text not null,
    model text,
    effort text,
    workspace_mode text default 'shared',
    permission_mode text default 'auto',
    system_prompt text not null,
    channels text default '[]',
    updated_ts real
);
create table if not exists squads (
    id text primary key,
    name text not null,
    cwd text not null,
    brief text,
    created_ts real not null,
    status text default 'active'
);
create table if not exists objects (
    id integer primary key autoincrement, kind text not null, project text not null, squad_id text,
    title text not null, body text not null default '', status text not null,
    owner_session text, parent_id integer references objects(id),
    files text not null default '[]', sha text, branch text,
    created_by text, created_by_name text not null, created_ts real not null, updated_ts real not null,
    data text not null default '{}');
create index if not exists objects_squad on objects(squad_id, kind, status);
create table if not exists object_log (
    id integer primary key autoincrement, object_id integer not null references objects(id), ts real not null,
    session text, agent_name text not null, role_id text, provider text, kind text not null, body text not null,
    status_from text, status_to text, owner_to text, message_id integer);
create index if not exists object_log_object on object_log(object_id, id);
"""
# One row per spawn. Keyed by session: squads reuse handles like `impl-claude`, and a later squad must not erase
# an earlier squad's roster. Lookups by name take the newest row.
SPAWNED_TABLE = """
create table if not exists spawned_agents (
    session_id text primary key not null,
    agent_name text not null,
    squad_id text,
    role_id text,
    provider text not null,
    model text,
    effort text,
    cwd text not null,
    worktree_path text,
    pid integer,
    created_ts real not null,
    channels text,
    spawned_by text,
    retired_ts real
);
"""
SPAWNED_COLUMNS = "session_id, agent_name, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, created_ts, channels, spawned_by"
SHELL = ("Bash", "PowerShell", "run_command")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit", "replace_file_content", "write_to_file",
              "multi_replace_file_content")   # Claude Code's and Gemini's file-editing tools
ANY_ROLE = "*"                # a squad's spawn_role that lets its architect add any role the operator defined
NEEDS = (None, "reply", "blocking")        # a message may ask for an answer; `blocking` means the sender cannot go on
OPERATOR_NAMES = ("operator", "almanac-hub")  # the human, by every name the hub answers to in the journal
TASK_STATUSES = ("queued", "in progress", "in review", "accepted", "blocked")
OBJECT_KINDS = {"T": "task"}                  # stage 1: tasks only; F finding and N note come later
OBJECT_COLUMNS = ("id, kind, project, squad_id, title, body, status, owner_session, parent_id, files, sha, branch, "
                  "created_by, created_by_name, created_ts, updated_ts, data, base")
LOG_COLUMNS = ("id, object_id, ts, session, agent_name, role_id, provider, kind, body, status_from, status_to, owner_to, "
               "message_id, sha")
MESSAGE_COLUMNS = ("id, msg_id, ts, from_name, from_session, from_provider, to_name, to_session, to_provider, "
                   "channel, squad_id, body, transport, needs, answered_ts, answered_by, read_ts")
DUPLICATE_WINDOW = 900      # seconds: a transcript copy of a message the hub journaled this recently is the same message

# T-29: the low/medium/high routing in these four briefs is brief text only; nothing in the hub enforces it
ARCHITECT = (
    "You are the Lead Architect: you plan and review, you do not write code. Break the mission into stages and "
    "slices, give each slice a named owner and the files it may touch, and review what comes back. Read teammates' "
    "live context (`mesh_context_stream`) before unblocking anyone and coordinate handoffs over `mesh_send`. "
    "You must NOT edit or create project files: hand every code change to an implementer. If no implementer is "
    "live, tell the operator (`mesh_send(to=\"operator\")`) instead of doing it yourself. Rule on medium, high and "
    "disputed findings; read the lows in the reviewer's sign-off, and reopen any. In a plan-first squad, file a plan (`mesh_file(plan=true)`), then its slices under it, hand the plan in, and wait "
    "for the operator before `mesh_spawn`."
)
PREV_ARCHITECT_T56 = (  # shipped from T-52 until T-57
    "You are the Lead Architect: you plan and review, you do not write code. Break the mission into stages and "
    "slices, give each slice a named owner and the files it may touch, and review what comes back. Read teammates' "
    "live context (`mesh_context_stream`) before unblocking anyone and coordinate handoffs over `mesh_send`. "
    "You must NOT edit or create project files: hand every code change to an implementer. If no implementer is "
    "live, tell the operator (`mesh_send(to=\"operator\")`) instead of doing it yourself. Rule on medium, high and "
    "disputed findings; read the lows in the reviewer's sign-off, and reopen any. In a plan-first squad, file the slices and a plan (`mesh_file(plan=true)`), hand the plan in, and wait for the operator before `mesh_spawn`."
)
PREV_ARCHITECT_T52 = (  # shipped until T-52
    "You are the Lead Architect: you plan and review, you do not write code. Break the mission into stages and "
    "slices, give each slice a named owner and the files it may touch, and review what comes back. Read teammates' "
    "live context (`mesh_context_stream`) before unblocking anyone and coordinate handoffs over `mesh_send`. "
    "You must NOT edit or create project files: hand every code change to an implementer. If no implementer is "
    "live, tell the operator (`mesh_send(to=\"operator\")`) instead of doing it yourself. Rule on medium, high and "
    "disputed findings; read the lows in the reviewer's sign-off, and reopen any."
)
PREV_ARCHITECT = (       # shipped until T-29
    "You are the Lead Architect: you plan and review, you do not write code. Break the mission into stages and "
    "slices, give each slice a named owner and the files it may touch, and review what comes back. Read teammates' "
    "live context (`mesh_context_stream`) before unblocking anyone and coordinate handoffs over `mesh_send`. "
    "You must NOT edit or create project files: hand every code change to an implementer. If no implementer is "
    "live, tell the operator (`mesh_send(to=\"operator\")`) instead of doing it yourself."
)
IMPLEMENTER = (
    "You are a Software Implementer, the only role that edits code. Own your assigned slice end to end with tests, "
    "and stay inside the files you were given; ask the architect before touching anything else. Check peer context "
    "and contact streams (`mesh_context_stream`, `mesh_contact_stream`) to stay aligned on shared interfaces. When "
    "a slice is done, hand in with `mesh_log(ref, status=\"in review\", body=<files touched, the test command and its "
    "result, anything you could not do>)` and nothing else: the hub tells the lead and the reviewer. Fix a low "
    "finding at once; wait for the lead's verdict on a medium or high one; send a low you disagree with to the lead "
    "instead of fixing it. Build to your task's Sketch; name any departure from it in your hand-in."
)
PREV_IMPLEMENTER_T52 = (  # shipped until T-52
    "You are a Software Implementer, the only role that edits code. Own your assigned slice end to end with tests, "
    "and stay inside the files you were given; ask the architect before touching anything else. Check peer context "
    "and contact streams (`mesh_context_stream`, `mesh_contact_stream`) to stay aligned on shared interfaces. When "
    "a slice is done, hand in with `mesh_log(ref, status=\"in review\", body=<files touched, the test command and its "
    "result, anything you could not do>)` and nothing else: the hub tells the lead and the reviewer. Fix a low "
    "finding at once; wait for the lead's verdict on a medium or high one; send a low you disagree with to the lead "
    "instead of fixing it."
)
PREV_IMPLEMENTER_T37 = (  # shipped from T-37 until T-29
    "You are a Software Implementer, the only role that edits code. Own your assigned slice end to end with tests, "
    "and stay inside the files you were given; ask the architect before touching anything else. Check peer context "
    "and contact streams (`mesh_context_stream`, `mesh_contact_stream`) to stay aligned on shared interfaces. When "
    "a slice is done, hand in with `mesh_log(ref, status=\"in review\", body=<files touched, the test command and its "
    "result, anything you could not do>)` and nothing else: the hub tells the lead and the reviewer."
)
PREV_IMPLEMENTER = (     # shipped until T-37: a separate hand-in message the hub now sends itself
    "You are a Software Implementer, the only role that edits code. Own your assigned slice end to end with tests, "
    "and stay inside the files you were given; ask the architect before touching anything else. Check peer context "
    "and contact streams (`mesh_context_stream`, `mesh_contact_stream`) to stay aligned on shared interfaces. When "
    "a slice is done, report to the architect and reviewer over `mesh_send`: files touched, the test command and "
    "its result, and anything you could not do."
)
PREV_IMPLEMENTERS = (PREV_IMPLEMENTER, PREV_IMPLEMENTER_T37, PREV_IMPLEMENTER_T52)
# a role's `allowed_commands`: exact command lines its runner may run. For Gemini they become agy allow rules (agy's
# default mode, all this account gets, denies every other shell command); for Claude agents the field is
# informational for now. The shipped reviewers get the lines a review needs. A list with FULL_SUITE in it also gets,
# at spawn time, one `python -m unittest discover tests -p test_<name>.py` line per test module (spawner.role_commands).
FULL_SUITE = "python -m unittest discover tests"
REVIEWER_COMMANDS = ["git status --short", "git diff", "git diff --stat", FULL_SUITE]
REVIEWER = (
    "You are the Cross-Model Peer Reviewer: you do not edit code. Read the implementer's diff (`mesh_diff(ref)`) and "
    "live context (`mesh_context_stream`), run the tests yourself, and probe edge cases. Rate every finding low, "
    "medium or high and send it (file:line, what breaks, how to reproduce, rating) over `mesh_send`: a low to the "
    "implementer only, a medium or high to the implementer and the lead. Your sign-off to the lead lists every "
    "finding with its rating and state. Check the diff against the task's Sketch."
)
PREV_REVIEWER_T52 = (  # shipped until T-52
    "You are the Cross-Model Peer Reviewer: you do not edit code. Read the implementer's diff (`mesh_diff(ref)`) and "
    "live context (`mesh_context_stream`), run the tests yourself, and probe edge cases. Rate every finding low, "
    "medium or high and send it (file:line, what breaks, how to reproduce, rating) over `mesh_send`: a low to the "
    "implementer only, a medium or high to the implementer and the lead. Your sign-off to the lead lists every "
    "finding with its rating and state."
)
PREV_REVIEWER_T38 = (     # shipped from T-38 until T-29
    "You are the Cross-Model Peer Reviewer: you do not edit code. Read the implementer's diff (`mesh_diff(ref)`) and "
    "live context (`mesh_context_stream`), run the tests yourself, and probe edge cases. Send findings (file:line, what breaks, "
    "how to reproduce) to the implementer, and sign-off or the open findings to the architect, over `mesh_send`."
)
ORCHESTRATOR = (
    "You are the Squad Orchestrator: you lead the squad and you may also act. Break the mission into slices with a "
    "named owner and the files it may touch, and review what comes back. You may yourself: make small edits in the "
    "main checkout (a few lines in one or two files; never auth, migrations or the spawn path), committed as the user "
    "with no AI trailers; merge a teammate's worktree branch into the checkout once its slice is reviewed and the full "
    "suite passes on the merged tree; restart Almanac with `mesh_restart` after a merge that changed Python code. Hand "
    "anything larger to an implementer. Every edit of your own goes to the reviewer afterwards, with its commit. Never "
    "force-push, reset or discard work; never delete a worktree with unmerged commits; never restart with a failing suite. "
    "Rule on medium, high and disputed findings; read the lows in the reviewer's sign-off, and reopen any. In a plan-first squad, file a plan (`mesh_file(plan=true)`), then its slices under it, hand the plan in, and wait "
    "for the operator before `mesh_spawn`."
)
PREV_ORCHESTRATOR_T56 = (  # shipped from T-52 until T-57
    "You are the Squad Orchestrator: you lead the squad and you may also act. Break the mission into slices with a "
    "named owner and the files it may touch, and review what comes back. You may yourself: make small edits in the "
    "main checkout (a few lines in one or two files; never auth, migrations or the spawn path), committed as the user "
    "with no AI trailers; merge a teammate's worktree branch into the checkout once its slice is reviewed and the full "
    "suite passes on the merged tree; restart Almanac with `mesh_restart` after a merge that changed Python code. Hand "
    "anything larger to an implementer. Every edit of your own goes to the reviewer afterwards, with its commit. Never "
    "force-push, reset or discard work; never delete a worktree with unmerged commits; never restart with a failing suite. "
    "Rule on medium, high and disputed findings; read the lows in the reviewer's sign-off, and reopen any. In a plan-first squad, file the slices and a plan (`mesh_file(plan=true)`), hand the plan in, and wait for the operator before `mesh_spawn`."
)
PREV_ORCHESTRATOR_T52 = (  # shipped until T-52
    "You are the Squad Orchestrator: you lead the squad and you may also act. Break the mission into slices with a "
    "named owner and the files it may touch, and review what comes back. You may yourself: make small edits in the "
    "main checkout (a few lines in one or two files; never auth, migrations or the spawn path), committed as the user "
    "with no AI trailers; merge a teammate's worktree branch into the checkout once its slice is reviewed and the full "
    "suite passes on the merged tree; restart Almanac with `mesh_restart` after a merge that changed Python code. Hand "
    "anything larger to an implementer. Every edit of your own goes to the reviewer afterwards, with its commit. Never "
    "force-push, reset or discard work; never delete a worktree with unmerged commits; never restart with a failing suite. "
    "Rule on medium, high and disputed findings; read the lows in the reviewer's sign-off, and reopen any."
)
PREV_ORCHESTRATOR = (    # shipped until T-29
    "You are the Squad Orchestrator: you lead the squad and you may also act. Break the mission into slices with a "
    "named owner and the files it may touch, and review what comes back. You may yourself: make small edits in the "
    "main checkout (a few lines in one or two files; never auth, migrations or the spawn path), committed as the user "
    "with no AI trailers; merge a teammate's worktree branch into the checkout once its slice is reviewed and the full "
    "suite passes on the merged tree; restart Almanac with `mesh_restart` after a merge that changed Python code. Hand "
    "anything larger to an implementer. Every edit of your own goes to the reviewer afterwards, with its commit. Never "
    "force-push, reset or discard work; never delete a worktree with unmerged commits; never restart with a failing suite."
)
# the briefs as first shipped; a stored role still equal to one of these is upgraded once (schema v4)
OLD_ARCHITECT = "You are the Lead Architect. Break down missions into clear stages and contracts, inspect teammates' live context streams (`mesh_context_stream`) before unblocking dependencies, and coordinate handoffs over `mesh_send`."
OLD_IMPLEMENTER = 'You are a Software Implementer. Own your assigned slice end-to-end with tests, check peer context and contact streams (`mesh_context_stream`, `mesh_contact_stream`) to stay aligned on shared interfaces, and report wave completion to the architect/reviewer via `mesh_send`.'
# the reviewer brief before it named `mesh_diff` (T-38); a stored reviewer still equal to it is upgraded on open
PREV_REVIEWER = (
    "You are the Cross-Model Peer Reviewer: you do not edit code. Read the implementer's diff and live context "
    "(`mesh_context_stream`), run the tests yourself, and probe edge cases. Send findings (file:line, what breaks, "
    "how to reproduce) to the implementer, and sign-off or the open findings to the architect, over `mesh_send`."
)
PREV_REVIEWERS = (PREV_REVIEWER, PREV_REVIEWER_T38, PREV_REVIEWER_T52)
PREV_ARCHITECTS = (PREV_ARCHITECT, PREV_ARCHITECT_T52, PREV_ARCHITECT_T56)
PREV_ORCHESTRATORS = (PREV_ORCHESTRATOR, PREV_ORCHESTRATOR_T52, PREV_ORCHESTRATOR_T56)
OLD_REVIEWER = "You are the Cross-Model Peer Reviewer. Watch implementers' context streams (`mesh_context_stream`), verify diffs, edge cases, and test results when notified, and send actionable sign-off or findings back over `mesh_send`."

DEFAULT_ROLES = (
    dict(
        id="orchestrator-claude",
        context_policy="compact-at-300000",
        title="Squad Orchestrator (Claude)",
        provider="claude",
        model="opus",
        effort="high",
        workspace_mode="shared",
        permission_mode="auto",
        channels=["#squad", "#architecture"],
        system_prompt=ORCHESTRATOR,
        may_page_operator=True,
        may_spawn=True,
        may_restart=True,
    ),
    dict(
        id="architect-gemini",
        context_policy="compact-at-300000",
        title="System Architect (Gemini)",
        provider="gemini",
        model="",
        effort="high",
        workspace_mode="shared",
        permission_mode="auto",
        channels=["#squad", "#architecture"],
        system_prompt=ARCHITECT,
        may_page_operator=True,
        may_spawn=True,
    ),
    dict(
        id="architect-claude",
        context_policy="compact-at-300000",
        title="System Architect (Claude)",
        provider="claude",
        model="opus",
        effort="high",
        workspace_mode="shared",
        permission_mode="auto",
        channels=["#squad", "#architecture"],
        system_prompt=ARCHITECT,
        may_page_operator=True,
        may_spawn=True,
    ),
    dict(
        id="implementer-claude",
        context_policy="fresh-per-task",
        title="Implementer (Claude)",
        provider="claude",
        model="sonnet",
        effort="high",
        workspace_mode="worktree",
        permission_mode="auto",
        channels=["#squad", "#impl"],
        system_prompt=IMPLEMENTER,
        # elastic: whoever adds one may also pick these per task, besides the defaults (new databases only)
        models=["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
        efforts=["low", "medium", "high"],
    ),
    dict(
        id="implementer-gemini",
        context_policy="fresh-per-task",
        title="Implementer (Gemini)",
        provider="gemini",
        model="",
        effort="high",
        workspace_mode="worktree",
        permission_mode="auto",
        channels=["#squad", "#impl"],
        system_prompt=IMPLEMENTER,
    ),
    dict(
        id="reviewer-gemini",
        context_policy="none",
        title="Peer Reviewer (Gemini)",
        provider="gemini",
        model="",
        effort="high",
        workspace_mode="shared",
        permission_mode="auto",
        channels=["#squad", "#review"],
        system_prompt=REVIEWER,
        allowed_commands=REVIEWER_COMMANDS,
    ),
    dict(
        id="reviewer-claude",
        context_policy="none",
        title="Peer Reviewer (Claude)",
        provider="claude",
        model="opus",
        effort="high",
        workspace_mode="shared",
        permission_mode="auto",
        channels=["#squad", "#review"],
        system_prompt=REVIEWER,
        allowed_commands=REVIEWER_COMMANDS,
    ),
)


def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def natural_key(ts: str | None, tool: str, cmd: str | None) -> str:
    return hashlib.sha1(f"{ts}|{tool}|{cmd or ''}".encode("utf-8", "replace")).hexdigest()


IMAGE_CHARS = 6000   # an image costs ~1,600 tokens however large its base64 is; ~4 chars a token


def result_chars(content) -> int:
    """Characters of text a tool result put back into context; an image counts as IMAGE_CHARS."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content or []:
        if isinstance(part, dict):
            if part.get("type") == "text":
                total += len(part.get("text") or "")
            elif part.get("type") == "image":
                total += IMAGE_CHARS
    return total


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.msg_cond = threading.Condition()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript(SCHEMA + SPAWNED_TABLE)
        self.db.execute("pragma journal_mode=wal")
        self.last_ingest: dict = {}
        try:
            self._migrate()
        except BaseException:
            self.db.close()                 # a failed start must not keep the database file locked
            raise

    def _migrate(self):
        """v2 (2026-11-08): sessions learn title, cwd, last model and context size. Existing rows only get them
        from a full re-read, so v2 forgets the read offsets once; every row is keyed, so the re-read is a no-op
        for calls and usage."""
        have = {r[1] for r in self.db.execute("pragma table_info(sessions)")}
        for column, kind in (("title", "text"), ("first_prompt", "text"), ("cwd", "text"), ("model", "text"),
                             ("context", "integer"), ("messages", "integer default 0"), ("custom_title", "text"),
                             ("name", "text"), ("name_usd", "real"), ("named_at", "real"), ("name_error", "text"),
                             ("provider", "text default 'claude'")):
            if column not in have:
                self.db.execute(f"alter table sessions add column {column} {kind}")
        # v5: a message can ask for an answer (`needs`), knows which message answered it, and when the operator read it
        have = {r[1] for r in self.db.execute("pragma table_info(messages)")}
        for column, kind in (("needs", "text"), ("answered_ts", "real"), ("answered_by", "integer"), ("read_ts", "real")):
            if column not in have:
                self.db.execute(f"alter table messages add column {column} {kind}")
        self.db.execute("create index if not exists messages_open on messages(needs, answered_ts)")
        if "may_page_operator" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column may_page_operator integer default 0")
        # v6: the model that issued each tool call; per-session lookups that the 2 s tile poll makes
        if "model" not in {r[1] for r in self.db.execute("pragma table_info(calls)")}:
            self.db.execute("alter table calls add column model text")
        self.db.execute("create index if not exists calls_session on calls(session, ts)")
        self.db.execute("create index if not exists usage_session on usage(session, ts)")
        self._rekey_spawned()
        # v7: elastic squads. A squad may let an agent whose role may_spawn add members of one role, up to a limit;
        # each spawn row says who added it (null: the operator)
        if "spawned_by" not in {r[1] for r in self.db.execute("pragma table_info(spawned_agents)")}:
            self.db.execute("alter table spawned_agents add column spawned_by text")
        # v11: a member retired by its role's fresh-per-task policy (door.context_boundary) gives its slot back
        if "retired_ts" not in {r[1] for r in self.db.execute("pragma table_info(spawned_agents)")}:
            self.db.execute("alter table spawned_agents add column retired_ts real")
        # v12 (T-46): a worktree member's branch and the commit it was cut from; a task's head at hand-in and that
        # branch's base, and the hand-in log row's sha
        for table, column in (("spawned_agents", "branch"), ("spawned_agents", "base"), ("objects", "base"),
                              ("object_log", "sha")):
            if column not in {r[1] for r in self.db.execute(f"pragma table_info({table})")}:
                self.db.execute(f"alter table {table} add column {column} text")
        have = {r[1] for r in self.db.execute("pragma table_info(squads)")}
        # T-55: a plan-first squad's agents start members only on slices of a plan the operator accepted (running
        # squads stay off)
        for column, kind in (("spawn_role", "text"), ("spawn_limit", "integer default 0"),
                             ("plan_first", "integer default 0")):
            if column not in have:
                self.db.execute(f"alter table squads add column {column} {kind}")
        if "may_spawn" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column may_spawn integer default 0")
        # v8: exact shell command lines a role's runner may run (agy's allow rules; informational for Claude)
        if "allowed_commands" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column allowed_commands text default '[]'")
        # v10: a notice (an assignment, a status notice, the hub's own messages) answers nothing and grants nothing
        if "notice" not in {r[1] for r in self.db.execute("pragma table_info(messages)")}:
            self.db.execute("alter table messages add column notice integer default 0")
        # v10: the models and efforts a spawner may pick for a role ([]: fixed at the role's model and effort)
        have = {r[1] for r in self.db.execute("pragma table_info(roles)")}
        for column in ("models", "efforts"):
            if column not in have:
                self.db.execute(f"alter table roles add column {column} text default '[]'")
        # v11: what happens to an agent's context at a task boundary. Roles that already exist get the shipped
        # default by id once, when the column is added (the rest: none); a restart never overwrites a later choice
        if "context_policy" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column context_policy text default 'none'")
            for r in DEFAULT_ROLES:
                self.db.execute("update roles set context_policy = ? where id = ?", (r["context_policy"], r["id"]))
        # v10: a role that may restart Almanac (POST /api/mesh/restart)
        if "may_restart" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column may_restart integer default 0")
        # v9: folders outside its workspace a Gemini role's runner may read (agy `--add-dir`)
        if "add_dirs" not in {r[1] for r in self.db.execute("pragma table_info(roles)")}:
            self.db.execute("alter table roles add column add_dirs text default '[]'")
        self.db.execute("create index if not exists spawned_agents_name on spawned_agents(agent_name, created_ts)")
        row = self.db.execute("select value from meta where key = 'schema'").fetchone()
        version = int(row[0]) if row else 0
        if version < 3:                      # v3: custom titles (the user's /rename, or ours) are read too
            self.db.execute("delete from files")
        if version < 4:
            # v4: messages read back out of a recipient's transcript name the recipient and its squad (older rows
            # carry the first 8 characters of its session id), and the ones the hub had already journaled go.
            self.db.execute(
                "update messages set "
                "to_name = (select agent_name from spawned_agents where session_id = messages.to_session), "
                "squad_id = (select squad_id from spawned_agents where session_id = messages.to_session) "
                "where transport = 'uds-transcript' and squad_id is null "
                "and to_session in (select session_id from spawned_agents)")
            self.db.execute(
                "delete from messages where transport = 'uds-transcript' and exists ("
                "select 1 from messages d where d.transport != 'uds-transcript' and d.from_name = messages.from_name "
                "and d.body = messages.body and d.ts between messages.ts - ? and messages.ts + ? "
                "and (d.to_session = messages.to_session or d.to_name = messages.to_name "
                "or (d.transport = 'uds-broadcast' and d.squad_id is messages.squad_id)))",
                (DUPLICATE_WINDOW, DUPLICATE_WINDOW))
            # role briefs that still read as shipped get the ones that say what each role must not do
            for old, role_ids in ((OLD_ARCHITECT, ("architect-gemini", "architect-claude")),
                                  (OLD_IMPLEMENTER, ("implementer-claude", "implementer-gemini")),
                                  (OLD_REVIEWER, ("reviewer-gemini", "reviewer-claude"))):
                for rid in role_ids:
                    new = next(r["system_prompt"] for r in DEFAULT_ROLES if r["id"] == rid)
                    self.db.execute("update roles set system_prompt = ?, updated_ts = ? where id = ? and system_prompt = ?",
                                    (new, time.time(), rid, old))
        if version < 5:                      # v5: the shipped roles that may page the operator
            for r in DEFAULT_ROLES:
                if r.get("may_page_operator"):
                    self.db.execute("update roles set may_page_operator = 1 where id = ?", (r["id"],))
        if version < 6:
            # v6: turns (worktime) and calls.model exist only from a read of the transcripts, so read them all once
            # more; every row is keyed, so the re-read adds nothing twice
            self.db.execute("delete from files")
        if version < 7:                      # v7: the shipped roles that may add squad members
            for r in DEFAULT_ROLES:
                if r.get("may_spawn"):
                    self.db.execute("update roles set may_spawn = 1 where id = ?", (r["id"],))
        if version < 8:                      # v8: the shipped reviewers may run the commands a review needs
            for r in DEFAULT_ROLES:
                if r.get("allowed_commands"):
                    self.db.execute("update roles set allowed_commands = ? where id = ? and coalesce(allowed_commands, '[]') = '[]'",
                                    (json.dumps(r["allowed_commands"]), r["id"]))
        if not self.db.execute("select 1 from meta where key = 'gemini_usage_deltas'").fetchone():
            # Gemini usage was stored as agy's running totals: read the Gemini transcripts once more so each turn
            # stores its own counts (rows are keyed by message id, so the re-read replaces them). A flag, not a
            # schema step, so it cannot collide with a schema bump on another branch.
            self.db.execute("delete from files where exists (select 1 from sessions s where s.provider = 'gemini' "
                            "and files.path like '%' || s.id || '.jsonl')")
            self.db.execute("delete from meta where key like 'gemini_usage:%'")
            self.db.execute("insert into meta values ('gemini_usage_deltas', '1')")
        seeded = self.db.execute("select count(*) from roles").fetchone()[0] > 0
        if version < 10 and seeded:
            # v10: the orchestrator role, once: a database that has roles gets it unless it has that id already (and
            # an operator who deletes it later keeps it deleted). A new database gets it with the others below.
            self._insert_role(next(r for r in DEFAULT_ROLES if r["id"] == "orchestrator-claude"), ignore=True)
        # v10: work objects (`objects`, `object_log`) are new tables in SCHEMA; nothing to backfill
        self.db.execute("insert or replace into meta values ('schema', '10')")
        # T-38, T-37, T-29: idempotent, so no schema step. A brief still equal to ANY earlier shipped text ends at the
        # current one; an edited brief is left alone. (The low/medium/high routing is brief text; nothing enforces it.)
        for new, prevs, rids in ((REVIEWER, PREV_REVIEWERS, ("reviewer-gemini", "reviewer-claude")),
                                 (IMPLEMENTER, PREV_IMPLEMENTERS, ("implementer-claude", "implementer-gemini")),
                                 (ARCHITECT, PREV_ARCHITECTS, ("architect-gemini", "architect-claude")),
                                 (ORCHESTRATOR, PREV_ORCHESTRATORS, ("orchestrator-claude",))):
            for rid in rids:
                for prev in prevs:
                    self.db.execute("update roles set system_prompt = ?, updated_ts = ? where id = ? and system_prompt = ?",
                                    (new, time.time(), rid, prev))
        if not seeded:
            for r in DEFAULT_ROLES:
                self._insert_role(r)
        self.db.commit()

    def _insert_role(self, r: dict, ignore: bool = False):
        self.db.execute(
            f"insert {'or ignore ' if ignore else ''}into roles (id, title, provider, model, effort, workspace_mode, "
            "permission_mode, system_prompt, channels, updated_ts, may_page_operator, may_spawn, allowed_commands, "
            "may_restart, models, efforts, context_policy) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["title"], r["provider"], r["model"], r["effort"], r["workspace_mode"],
             r["permission_mode"], r["system_prompt"], json.dumps(r["channels"]), time.time(),
             int(bool(r.get("may_page_operator"))), int(bool(r.get("may_spawn"))),
             json.dumps(r.get("allowed_commands") or []), int(bool(r.get("may_restart"))),
             json.dumps(r.get("models") or []), json.dumps(r.get("efforts") or []),
             r.get("context_policy") or "none"),
        )

    def _rekey_spawned(self):
        """Spawns were keyed by agent name before 2026-11-10; key them by session. Rename, create, copy and drop
        are one transaction, so a crash leaves the old table as it was. A rebuild that an earlier build left
        half done (rows stranded in spawned_agents_by_name) is finished here."""
        keyed_by_name = bool({r[1]: r[5] for r in self.db.execute("pragma table_info(spawned_agents)")}.get("agent_name"))
        stranded = self.db.execute("select 1 from sqlite_master where name = 'spawned_agents_by_name'").fetchone()
        if not keyed_by_name and not stranded:
            return
        self.db.commit()
        try:
            self.db.execute("begin")
            if keyed_by_name:
                self.db.execute("alter table spawned_agents rename to spawned_agents_by_name")
                self.db.execute(SPAWNED_TABLE)
            old = {r[1] for r in self.db.execute("pragma table_info(spawned_agents_by_name)")}
            self.db.execute(
                f"insert or ignore into spawned_agents ({SPAWNED_COLUMNS}) select coalesce(session_id, 'legacy:' || agent_name), "
                "agent_name, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, created_ts, "
                f"{'channels' if 'channels' in old else 'null'}, null from spawned_agents_by_name")
            self.db.execute("drop table spawned_agents_by_name")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ ingest

    def ingest(self, root: Path, *, provider: str = "claude") -> dict:
        """Read every transcript under `root` from where the last pass stopped. Returns what changed."""
        started = time.monotonic()
        root = Path(root).expanduser()
        stats = dict(files=0, read=0, calls=0, results=0, messages=0, bytes=0)
        if not root.is_dir():
            self.last_ingest = dict(stats, error=f"{root} is not a folder", at=time.time())
            return self.last_ingest
        with self.lock:
            known = {p: (s, m, o) for p, s, m, o in self.db.execute("select path, size, mtime_ns, offset from files")}
            for path in sorted(root.rglob("*.jsonl")):
                stats["files"] += 1
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                size, mtime, offset = known.get(key, (0, 0, 0))
                if st.st_size == size and st.st_mtime_ns == mtime:
                    continue
                if st.st_size < offset:
                    offset = 0          # rewritten or truncated: read it again; keys keep this idempotent
                new_offset = self._read_file(path, root, offset, stats, provider=provider)
                self.db.execute("insert or replace into files values (?, ?, ?, ?)", (key, st.st_size, st.st_mtime_ns, new_offset))
                stats["read"] += 1
            self.last_ingest = dict(stats, at=time.time(), seconds=round(time.monotonic() - started, 2))
            # under the lock like every other write: the HTTP and hub threads share this connection
            self.db.execute("insert or replace into meta values ('last_ingest', ?)", (json.dumps(self.last_ingest),))
            self.db.commit()
        return self.last_ingest

    def _read_file(self, path: Path, root: Path, offset: int, stats: dict, *, provider: str = "claude") -> int:
        project = path.relative_to(root).parts[0]
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return offset
        stats["bytes"] += end + 1
        self._from_top, self._totals_reset = offset == 0, set()   # see _gemini_turn
        for raw in data[:end + 1].splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self._record(rec, project, stats, default_provider=provider)
        return offset + end + 1

    def _gemini_turn(self, session: str, totals: tuple) -> tuple:
        """One Gemini turn's usage from agy's running totals: agy reports the conversation's totals so far, and
        they carry on across a relaunch with `--conversation` (measured 2026-11-11: inputs 10,439, 12,598, then
        14,884 after a kill and relaunch). The previous totals per session live in `meta`; a file read from the top
        starts the session over, and any count below the previous one is a reset, taken as it is."""
        if not any(totals):
            return totals                 # a failed turn reports zeros: no turn's usage, and not a reset of the totals
        key = f"gemini_usage:{session}"
        prev = None
        if not (getattr(self, "_from_top", False) and session not in self._totals_reset):
            row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
            prev = json.loads(row[0]) if row else None
        elif getattr(self, "_from_top", False):
            self._totals_reset.add(session)
        self.db.execute("insert or replace into meta values (?, ?)", (key, json.dumps(list(totals))))
        if prev is None or any(t < p for t, p in zip(totals, prev)):
            return totals
        return tuple(t - p for t, p in zip(totals, prev))

    def _record(self, rec: dict, project: str, stats: dict, *, default_provider: str = "claude"):
        kind = rec.get("type")
        session = rec.get("sessionId") or ""
        prov = rec.get("provider") or default_provider
        if kind == "custom-title" and session and rec.get("customTitle"):
            self.db.execute("insert into sessions (id, project, custom_title, provider) values (?, ?, ?, ?) "
                            "on conflict(id) do update set custom_title = excluded.custom_title",
                            (session, project, str(rec["customTitle"])[:200], prov))
            return
        if kind == "ai-title" and session and rec.get("aiTitle"):
            self.db.execute("insert into sessions (id, project, title, provider) values (?, ?, ?, ?) "
                            "on conflict(id) do update set title = excluded.title",
                            (session, project, str(rec["aiTitle"])[:200], prov))
            return
        if kind not in ("assistant", "user"):
            return
        ts_raw = rec.get("timestamp")
        ts = parse_ts(ts_raw)
        main = not rec.get("isSidechain")
        if main and session and ts:
            self._turn(session, ts, kind, (rec.get("message") or {}).get("content"))
        if session and ts:
            self.db.execute(
                "insert into sessions (id, project, first_ts, last_ts, sidechain, version, cwd, provider) values (?, ?, ?, ?, ?, ?, ?, ?) "
                "on conflict(id) do update set first_ts = min(coalesce(first_ts, excluded.first_ts), excluded.first_ts), "
                "last_ts = max(coalesce(last_ts, excluded.last_ts), excluded.last_ts), version = coalesce(excluded.version, version), "
                "cwd = coalesce(excluded.cwd, cwd), provider = coalesce(excluded.provider, provider)",
                (session, project, ts, ts, int(not main), rec.get("version"), rec.get("cwd") if main else None, prov))
        msg = rec.get("message") or {}
        if prov == "gemini" and msg.get("model"):
            # agy echoes the wire id (`gemini-4-carbon-high`) on tool calls and the base id on usage; one model
            from almanac.gemini_host import split_agy_model_effort
            msg = dict(msg, model=split_agy_model_effort(msg["model"])[0] or msg["model"])
        content = msg.get("content")
        if main and session and kind == "user":
            text = content if isinstance(content, str) else next(
                (b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"), None)
            if text:
                parsed_peer = uds.parse_cross_session(str(text))
                if parsed_peer:
                    sender, body, stamp = parsed_peer.get("from_name") or "peer", parsed_peer.get("body") or "", ts or time.time()
                    det_id = "tx:" + hashlib.sha1(
                        f"{session}|{ts_raw or ''}|{parsed_peer.get('from_name') or ''}|{body}".encode("utf-8", "replace")
                    ).hexdigest()[:24]
                    # A message sent through the hub is journaled when sent; its copy in the recipient's transcript
                    # is the same message, not a second one. Only what bypassed the hub (native SendMessage) is new.
                    to = self.db.execute("select agent_name, squad_id from spawned_agents where session_id = ?",
                                         (session,)).fetchone() or (session[:8], None)
                    journaled = self.db.execute(
                        "select 1 from messages where transport != 'uds-transcript' and from_name = ? and body = ? "
                        "and ts between ? and ? and (to_session = ? or to_name = ? "
                        "or (transport = 'uds-broadcast' and (squad_id is ? or squad_id is null))) limit 1",
                        (sender, body, stamp - DUPLICATE_WINDOW, stamp + DUPLICATE_WINDOW, session, to[0], to[1])).fetchone()
                    if not journaled:
                        cur = self.db.execute(
                            "insert or ignore into messages (msg_id, ts, from_name, to_name, to_session, to_provider, channel, squad_id, body, transport, needs) "
                            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'uds-transcript', ?)",
                            (det_id, stamp, sender, to[0], session, prov, parsed_peer.get("channel"), to[1], body,
                             parsed_peer.get("needs")),
                        )
                        if cur.rowcount:
                            self._close_answered(cur.lastrowid, stamp, sender, to[0], to[1])
                            if parsed_peer.get("needs"):
                                self._close_if_answered(cur.lastrowid, stamp, sender, to[0], to[1])
                elif not str(text).startswith("<"):
                    self.db.execute("update sessions set first_prompt = coalesce(first_prompt, ?) where id = ?",
                                    (" ".join(str(text).split())[:200], session))
        if kind == "assistant":
            usage = msg.get("usage")
            if usage:
                counts = tuple(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                if prov == "gemini" and session:
                    counts = self._gemini_turn(session, counts)
            if main and session and usage and any(counts) and msg.get("model") not in (None, "<synthetic>"):
                self.db.execute("update sessions set model = ?, context = coalesce(?, context) where id = ?", (
                    # a step that re-read nothing (agy's /compact checkpoint) leaves the reading as it was
                    msg.get("model"), int(rec.get("context_tokens") or 0) or counts[0] + counts[1] + counts[2] or None,
                    session))
            if usage and msg.get("id"):
                self.db.execute("insert or replace into usage values (?, ?, ?, ?, ?, ?, ?, ?)",
                                (msg["id"], session, ts, msg.get("model"), *counts))
                stats["messages"] += 1
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool = block.get("name") or "?"
                cmd = (block.get("input") or {}).get("command") if tool in SHELL else None
                model = msg.get("model") if msg.get("model") not in (None, "<synthetic>") else None
                self._call(ts_raw, ts, session, tool, block.get("id"), cmd, model=model)
                stats["calls"] += 1
        else:
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result" or not block.get("tool_use_id"):
                    continue
                self.db.execute(
                    "update calls set out_chars = ?, is_error = ?, seconds = case when ts is not null and ? is not null "
                    "then max(0, ? - ts) end where tool_use_id = ?",
                    (result_chars(block.get("content")), int(bool(block.get("is_error"))), ts, ts, block["tool_use_id"]))
                stats["results"] += 1

    def _turn(self, session: str, ts: float, kind: str, content) -> None:
        """Worktime. A user record that carries a prompt opens a turn at its time; every other main-thread record
        stretches the newest turn that began at or before it (or opens an 'other' turn if there is none).
        Keyed and max-only, so reading a transcript again changes nothing."""
        text, prompt = (content, True) if isinstance(content, str) else (None, False)
        if kind == "user" and isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            # any user record that is not a tool result is a prompt, with text or without (an image-only prompt
            # must not stretch the previous turn across the idle gap before it)
            prompt = not any(b.get("type") == "tool_result" for b in blocks)
            text = next((b.get("text") for b in blocks if b.get("type") == "text"), None) if prompt else None
        if kind == "user" and prompt:
            opens = ("peer" if text and uds.parse_cross_session(str(text)) else "other" if text is None or str(text).startswith("<")
                     else "prompt")
            self.db.execute("insert or ignore into turns values (?, ?, ?, ?)", (session, ts, ts, opens))
            return
        cur = self.db.execute(
            "update turns set end_ts = max(end_ts, ?) where session = ? and start_ts = "
            "(select max(start_ts) from turns where session = ? and start_ts <= ?)", (ts, session, session, ts))
        if not cur.rowcount:
            self.db.execute("insert or ignore into turns values (?, ?, ?, 'other')", (session, ts, ts))

    def _call(self, ts_raw, ts, session, tool, tool_use_id, cmd, source="live", model=None):
        row = dict(natural=natural_key(ts_raw, tool, cmd), tool_use_id=tool_use_id, session=session, ts=ts, tool=tool,
                   family=None, head=None, cmd_len=None, search=0, tests=0, http=0, edit=0, source=source, model=model)
        if cmd is not None:
            row.update(family=classify.family(cmd), head=classify.head(cmd), cmd_len=len(cmd), **classify.adoption(cmd))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        if source == "live":
            # a legacy row for the same call steps aside for the live one, which has sizes and waits
            self.db.execute("delete from calls where natural = ? and source = 'legacy'", (row["natural"],))
            self.db.execute(f"insert or ignore into calls ({cols}) values ({marks})", tuple(row.values()))
            if model and tool_use_id:          # a re-read corrects a row: no model before v6, or a Gemini wire id
                self.db.execute("update calls set model = ? where tool_use_id = ? and model is not ?", (model, tool_use_id, model))
        else:
            self.db.execute(f"insert or ignore into calls ({cols}) values ({marks})", tuple(row.values()))

    def seed_legacy(self, path: Path) -> int:
        """Load the inventory's `tooluses.jsonl` ({tool, cmd, proj, ts}). Returns rows added."""
        added = 0
        with self.lock:
            before = self.db.execute("select count(*) from calls").fetchone()[0]
            for raw in Path(path).read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                self._call(r.get("ts"), parse_ts(r.get("ts")), f"legacy:{r.get('proj', '')}", r.get("tool") or "?",
                           None, r.get("cmd"), source="legacy")
            self.db.commit()
            added = self.db.execute("select count(*) from calls").fetchone()[0] - before
        return added

    # ------------------------------------------------------------------ report

    def report(self, days: int = 30, bucket: str = "day", now: float | None = None, provider: str | None = None) -> dict:
        """Everything the dashboard draws: tiles (this period vs the one before), a series per bucket,
        the family table, the tool table and the per-model and per-provider tables. `days` 0 means everything.
        `provider` ('claude' or 'gemini') keeps only calls and usage of sessions from that provider; legacy calls
        (no session, no provider) then drop out. Coverage stays the whole database either way."""
        if bucket not in ("day", "week"):
            raise ValueError("bucket is day or week")
        if provider not in REPORT_PROVIDERS:
            raise ValueError("provider is claude or gemini")
        now = now or time.time()
        call_prov = "case when c.source = 'legacy' then null else coalesce(s.provider, 'claude') end"
        only = " and c.source = 'live' and coalesce(s.provider, 'claude') = ?" if provider else ""
        only_usage = " and coalesce(s.provider, 'claude') = ?" if provider else ""
        with self.lock:
            first = self.db.execute("select min(ts) from calls").fetchone()[0]
            span = days * 86400 if days > 0 else max(86400, now - (first or now) + 86400)
            start, prev = now - span, now - 2 * span
            # columns 0..10 are what the older helpers index; model, is_error and provider ride at the end
            calls = self.db.execute(
                "select c.ts, c.tool, c.family, c.search, c.tests, c.http, c.edit, c.out_chars, c.seconds, c.session, "
                f"c.source, c.model, c.is_error, {call_prov} from calls c left join sessions s on s.id = c.session "
                f"where c.ts >= ?{only} order by c.ts", (prev, provider) if provider else (prev,)).fetchall()
            usage = self.db.execute(
                "select u.ts, u.input, u.cache_create, u.cache_read, u.output, u.session, u.model, "
                "coalesce(s.provider, 'claude') from usage u left join sessions s on s.id = u.session "
                f"where u.ts >= ?{only_usage}", (prev, provider) if provider else (prev,)).fetchall()
            totals = self.db.execute("select count(*), sum(source = 'legacy'), count(distinct session) from calls").fetchone()
            last = self.db.execute("select value from meta where key = 'last_ingest'").fetchone()
        cur_calls = [c for c in calls if c[0] >= start]
        old_calls = [c for c in calls if c[0] < start]
        cur_usage = [u for u in usage if u[0] >= start]
        old_usage = [u for u in usage if u[0] < start]
        return {
            "range": dict(days=days, bucket=bucket, start=start, end=now, provider=provider),
            "coverage": dict(first=first, calls=totals[0] or 0, legacy_calls=totals[1] or 0, sessions=totals[2] or 0),
            "last_ingest": json.loads(last[0]) if last else None,
            "tiles": tiles(cur_calls, old_calls, cur_usage, old_usage),
            "series": series(cur_calls, cur_usage, start, now, bucket),
            "families": family_table(cur_calls, old_calls),
            "tools": tool_table(cur_calls),
            "models": model_table(cur_calls, old_calls, cur_usage, old_usage),
            "providers": provider_table(cur_calls, old_calls, cur_usage, old_usage),
        }

    def log_action(self, action: str, pid: int, row: dict | None, ok: bool, detail: str, who: str = "") -> dict:
        entry = dict(ts=time.time(), action=action, pid=pid, session=(row or {}).get("session_id"),
                     name=(row or {}).get("name") or (row or {}).get("role"), ok=bool(ok), detail=detail[:500], who=who)
        with self.lock:
            self.db.execute("insert into actions (ts, action, pid, session, name, ok, detail, who) values (?, ?, ?, ?, ?, ?, ?, ?)",
                            (entry["ts"], action, pid, entry["session"], entry["name"], int(entry["ok"]), entry["detail"], who))
            self.db.commit()
        return entry

    def actions(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select ts, action, pid, session, name, ok, detail, who from actions order by id desc limit ?",
                                   (limit,)).fetchall()
        return [dict(ts=r[0], action=r[1], pid=r[2], session=r[3], name=r[4], ok=bool(r[5]), detail=r[6], who=r[7]) for r in rows]

    def session_usage(self, session: str) -> dict:
        with self.lock:
            row = self.db.execute("select count(*), sum(input), sum(cache_create), sum(cache_read), sum(output) "
                                  "from usage where session = ?", (session,)).fetchone()
            calls = self.db.execute("select count(*), sum(out_chars) from calls where session = ?", (session,)).fetchone()
        return dict(messages=row[0] or 0, input=row[1] or 0, cache_create=row[2] or 0, cache_read=row[3] or 0,
                    output=row[4] or 0, calls=calls[0] or 0, read_back_kb=round((calls[1] or 0) / 1000, 1),
                    **self.work_summary(session))

    # ------------------------------------------------------------------ topology and swimlane (slice 4)

    def message_graph(self, squad_id: str) -> list[dict]:
        """Who wrote to whom in a squad: one edge per (from, to), broadcasts kept as their `@...` / `#...` target and
        the hub's names folded into `operator`, with the count, the newest time and the flags still open on it."""
        with self.lock:
            rows = self.db.execute(
                "select from_name, to_name, count(*), max(ts), "
                "sum(needs = 'reply' and answered_ts is null), sum(needs = 'blocking' and answered_ts is null) "
                "from messages where squad_id = ? and transport != 'system' group by from_name, to_name",
                (squad_id,)).fetchall()
        edges: dict[tuple, dict] = {}
        for frm, to, count, last, reply, blocking in rows:
            key = ("operator" if frm in OPERATOR_NAMES else frm, "operator" if to in OPERATOR_NAMES else to)
            e = edges.setdefault(key, {"from": key[0], "to": key[1], "count": 0, "last_ts": 0.0,
                                       "open": {"reply": 0, "blocking": 0}})
            e["count"] += count
            e["last_ts"] = max(e["last_ts"], last or 0.0)
            e["open"]["reply"] += reply or 0
            e["open"]["blocking"] += blocking or 0
        return sorted(edges.values(), key=lambda e: (-e["count"], e["from"], e["to"]))

    def timeline_messages(self, *, squad_id: str | None = None, sessions=(), start: float, end: float,
                          limit: int = 1000) -> list[dict]:
        """Messages for the swimlane inside [start, end], oldest first (the newest `limit`): a squad's, or those to or
        from any of `sessions`."""
        sessions = [s for s in sessions if s][:20]
        if squad_id:
            where, params = "squad_id = ?", [squad_id]
        elif sessions:
            marks = ",".join("?" * len(sessions))
            where, params = f"(from_session in ({marks}) or to_session in ({marks}))", [*sessions, *sessions]
        else:
            return []
        with self.lock:
            rows = self.db.execute(
                f"select id, ts, from_name, to_name, needs, answered_ts, channel, body from messages where {where} "
                "and transport != 'system' and ts between ? and ? order by ts desc limit ?", (*params, start, end, int(limit))).fetchall()
        return [{"id": r[0], "ts": r[1], "from": "operator" if r[2] in OPERATOR_NAMES else r[2],
                 "to": "operator" if r[3] in OPERATOR_NAMES else r[3], "needs": r[4], "answered_ts": r[5],
                 "channel": r[6], "preview": " ".join(str(r[7] or "").split())[:120]} for r in reversed(rows)]

    def timeline_marks(self, sessions, *, start: float, end: float, limit: int = 2000) -> list[dict]:
        """Tool errors and file edits per session inside [start, end], oldest first (the newest `limit`). An edit is
        an edit tool (Claude's or Gemini's) or a shell call classified as one; an error wins over an edit."""
        sessions = [s for s in sessions if s][:20]
        if not sessions:
            return []
        marks = ",".join("?" * len(sessions))
        tools = ",".join("?" * len(EDIT_TOOLS))
        with self.lock:
            rows = self.db.execute(
                f"select ts, session, is_error from calls where session in ({marks}) and ts between ? and ? "
                f"and (is_error = 1 or tool in ({tools}) or family = 'edit') order by ts desc limit ?",
                (*sessions, start, end, *EDIT_TOOLS, int(limit))).fetchall()
        return [{"ts": r[0], "session": r[1], "kind": "error" if r[2] else "edit"} for r in reversed(rows)]

    # ------------------------------------------------------------------ worktime

    def worktime(self, sessions, *, start=None, end=None, bucket_s=None, open_until=None) -> dict:
        """When each session worked, waited and sat idle, and how the sessions overlapped.

        A turn runs from a prompt (typed, a peer message, a harness wrapper) to the last record before the next one.
        Inside a turn, time in a `mesh_wait` call is waiting; the rest is work. Idle is the session's own span (first
        turn to last activity, clipped to the range) minus its turns. Known limit: time a tool sits on a human
        permission prompt counts as work, because the transcript cannot tell it from a slow tool; and a Gemini agent
        that waits through `call_mcp_tool(ToolName="mesh_wait")` is not seen by `is_wait_tool`, so that wait is work too.
        `open_until` {session: ts} stretches a session's last turn to ts (ingest lags a live, busy session)."""
        sessions = [s for s in dict.fromkeys(sessions or []) if s][:20]
        open_until = open_until or {}
        if not sessions:
            return dict(range=None, agents=[], overlap=None)
        marks = ",".join("?" * len(sessions))
        with self.lock:
            turn_rows = self.db.execute(f"select session, start_ts, end_ts from turns where session in ({marks}) "
                                        "order by session, start_ts", sessions).fetchall()
            call_rows = self.db.execute(f"select session, ts, tool, seconds, is_error from calls where session in ({marks}) "
                                        "and ts is not null", sessions).fetchall()
            usage_rows = self.db.execute(f"select session, ts, output from usage where session in ({marks}) and ts is not null",
                                         sessions).fetchall()
        turns = {s: [] for s in sessions}
        for s, a, b in turn_rows:
            turns[s].append([a, b])
        # a busy session whose newest prompt is not ingested yet: stretch its last turn only if that turn ended
        # moments ago; otherwise the idle gap before the new work would count as work, so open a provisional turn
        # from its first activity after that end (or the last OPEN_TURN_S seconds) to now
        open_from: dict[str, float] = {}
        for s, until in open_until.items():
            if s not in turns or not until:
                continue
            spans = turns[s]
            if spans and until - spans[-1][1] <= OPEN_STRETCH_S:
                spans[-1][1] = max(spans[-1][1], until)
                open_from[s] = spans[-1][0]
                continue
            after = spans[-1][1] if spans else None
            later = [r[1] for r in call_rows + usage_rows if r[0] == s and (after is None or r[1] > after)]
            begin = min(later) if later else until - OPEN_TURN_S
            if begin < until:
                spans.append([begin, until])
                open_from[s] = begin
        firsts = [iv[0][0] for iv in turns.values() if iv]
        lasts = [max(b for _, b in iv) for iv in turns.values() if iv]
        lasts += [r[1] for r in call_rows] + [r[1] for r in usage_rows]
        start = start if start is not None else (min(firsts) if firsts else None)
        end = end if end is not None else (max(lasts) if lasts else None)
        if start is None or end is None or end <= start:
            return dict(range=dict(start=start, end=end, bucket_s=bucket_s, n=0),
                        agents=[_empty_agent(s) for s in sessions], overlap=None)
        bucket_s = bucket_s or default_bucket(end - start)
        n = max(1, math.ceil((end - start) / bucket_s))
        clip = lambda iv: [(max(a, start), min(b, end)) for a, b in iv if b > start and a < end]   # noqa: E731
        agents, work_by = [], {}
        for s in sessions:
            spans = clip(turns[s])
            in_turn = merge_intervals(spans)
            wait_iv = [(ts, ts + sec) for ss, ts, tool, sec, _ in call_rows if ss == s and is_wait_tool(tool) and sec]
            if s in open_from:                         # a mesh_wait still waiting (no result yet) in the open turn
                wait_iv += [(ts, open_until[s]) for ss, ts, tool, sec, _ in call_rows
                            if ss == s and is_wait_tool(tool) and sec is None and ts >= open_from[s]]
            waits = merge_intervals(clip(wait_iv))
            waiting = intersect_intervals(in_turn, waits)
            work = subtract_intervals(in_turn, waits)
            work_by[s] = work
            calls = [r for r in call_rows if r[0] == s and start <= r[1] <= end]
            out = [r for r in usage_rows if r[0] == s and start <= r[1] <= end]
            seen = [t for t in [iv[0] for iv in turns[s]] + [r[1] for r in calls] + [r[1] for r in out] if start <= t <= end]
            first = min(seen) if seen else None
            last = max([b for _, b in spans] + seen) if seen or spans else None
            wall = (last - first) if first is not None and last is not None else 0.0
            turn_s = _total(in_turn)
            events, output = [0] * n, [0] * n
            for r in calls:
                events[_bucket(r[1], start, bucket_s, n)] += 1
            for r in out:
                i = _bucket(r[1], start, bucket_s, n)
                events[i] += 1
                output[i] += int(r[2] or 0)
            agents.append(dict(
                session_id=s, first_ts=first, last_ts=last, wall_s=round(wall, 1), work_s=round(_total(work), 1),
                wait_s=round(_total(waiting), 1), idle_s=round(max(0.0, wall - turn_s), 1), turns=len(spans),
                longest_turn_s=round(max((b - a for a, b in spans), default=0.0), 1), calls=len(calls),
                tool_errors=sum(1 for r in calls if r[4]), output_tokens=sum(int(r[2] or 0) for r in out),
                work=[round(v, 1) for v in bucket_seconds(work, start, bucket_s, n)],
                wait=[round(v, 1) for v in bucket_seconds(waiting, start, bucket_s, n)], events=events, output=output))
        overlap = concurrency(work_by, start=start, end=end)
        working = [round(sum(a["work"][i] for a in agents) / bucket_s, 2) for i in range(n)]
        max_working = [0] * n
        for a, b, count in overlap.pop("segments"):
            if count:
                for i in range(_bucket(a, start, bucket_s, n), _bucket(max(a, b - 1e-9), start, bucket_s, n) + 1):
                    max_working[i] = max(max_working[i], count)
        overlap.update(working=working, max_working=max_working)
        return dict(range=dict(start=start, end=end, bucket_s=bucket_s, n=n), agents=agents, overlap=overlap)

    def work_summary(self, session: str) -> dict:
        """Work, wait and turn count for one session (the FLEET detail and tile heads)."""
        with self.lock:
            spans = self.db.execute("select start_ts, end_ts from turns where session = ?", (session,)).fetchall()
            waits = self.db.execute("select ts, seconds, tool from calls where session = ? and seconds is not null "
                                    "and tool like '%mesh_wait'", (session,)).fetchall()
        in_turn = merge_intervals(spans)
        wait_iv = merge_intervals([(ts, ts + sec) for ts, sec, tool in waits if is_wait_tool(tool)])
        return dict(work_s=round(_total(subtract_intervals(in_turn, wait_iv)), 1),
                    wait_s=round(_total(intersect_intervals(in_turn, wait_iv)), 1), turns=len(spans))

    def chats(self, query: str = "", limit: int = 300) -> list[dict]:
        """Every main session Almanac has seen, newest first: title, folder, model, context, replies, output tokens."""
        like = f"%{query.strip()}%" if query.strip() else "%"
        with self.lock:
            rows = self.db.execute(
                "select s.id, s.project, s.title, s.first_prompt, s.cwd, s.model, s.context, s.first_ts, s.last_ts, s.version, "
                "count(u.message_id), coalesce(sum(u.output), 0), s.custom_title, s.name, s.name_error, coalesce(s.provider, 'claude') "
                "from sessions s left join usage u on u.session = s.id "
                "where s.id not like 'legacy:%' and s.first_ts is not null and s.cwd is not null "
                "and (coalesce(s.title, '') like ? or coalesce(s.first_prompt, '') like ? or coalesce(s.cwd, '') like ? or s.id like ? "
                "or coalesce(s.custom_title, '') like ? or coalesce(s.name, '') like ?) "
                "group by s.id order by s.last_ts desc limit ?", (like, like, like, like, like, like, limit)).fetchall()
        out = []
        for r in rows:
            title, source = next(((t, src) for t, src in ((r[12], "renamed"), (r[13], "haiku"), (r[2], "auto"), (r[3], "prompt")) if t),
                                 ("(untitled)", None))
            if r[12] and r[13] and r[12] == r[13]:
                source = "haiku"             # the custom title is the one we wrote back
            out.append(dict(id=r[0], project=r[1], title=title, title_source=source, auto_title=r[2], first_prompt=r[3],
                            cwd=r[4], model=r[5], context=r[6] or 0, first=r[7], last=r[8], version=r[9], replies=r[10],
                            output=r[11], custom_title=r[12], name=r[13], name_error=r[14], provider=r[15]))
        return out

    def titles(self, ids: list[str]) -> dict[str, str]:
        """{session id: the name to show}: custom title (yours or ours) > Haiku name > Claude Code's auto title."""
        ids = [i for i in ids if i][:200]
        if not ids:
            return {}
        with self.lock:
            rows = self.db.execute(f"select id, custom_title, name, title from sessions where id in ({','.join('?' * len(ids))})",
                                   ids).fetchall()
        return {r[0]: r[1] or r[2] or r[3] for r in rows if r[1] or r[2] or r[3]}

    def unnamed(self, quiet_since: float, limit: int = 20) -> list[dict]:
        """Chats with neither a custom title nor a Haiku name that have been quiet since `quiet_since`, newest first;
        a chat whose naming failed is retried after a day."""
        with self.lock:
            rows = self.db.execute(
                "select id, project, cwd from sessions where id not like 'legacy:%' and cwd is not null and first_ts is not null "
                "and custom_title is null and name is null and last_ts < ? and (name_error is null or named_at < ?) "
                "order by last_ts desc limit ?", (quiet_since, time.time() - 86400, limit)).fetchall()
        return [dict(id=r[0], project=r[1], cwd=r[2]) for r in rows]

    def set_name(self, session: str, name: str | None, usd: float = 0.0, error: str | None = None) -> None:
        with self.lock:
            self.db.execute("update sessions set name = coalesce(?, name), name_usd = coalesce(name_usd, 0) + ?, "
                            "named_at = ?, name_error = ? where id = ?", (name, usd, time.time(), error, session))
            self.db.commit()

    def session_context(self, session_id: str) -> int | None:
        with self.lock:
            row = self.db.execute("select context from sessions where id = ?", (session_id,)).fetchone()
        return (row[0] or None) if row else None

    def chat(self, session_id: str) -> dict | None:
        found = [c for c in self.chats(session_id, 5) if c["id"] == session_id]
        return found[0] if found else None

    def counts(self, since: float) -> dict:
        with self.lock:
            calls, sessions = self.db.execute("select count(*), count(distinct session) from calls where ts >= ?",
                                              (since,)).fetchone()
            chars = self.db.execute("select avg(out_chars) from calls where ts >= ? and out_chars is not null",
                                    (since,)).fetchone()[0]
        return dict(calls=calls or 0, sessions=sessions or 0, kb_per_call=round((chars or 0) / 1000, 2))

    # ------------------------------------------------------------------ mesh: contact stream, roles, squads

    def record_message(
        self,
        *,
        from_name: str,
        to_name: str,
        body: str,
        msg_id: str | None = None,
        ts: float | None = None,
        from_session: str | None = None,
        from_provider: str | None = None,
        to_session: str | None = None,
        to_provider: str | None = None,
        channel: str | None = None,
        squad_id: str | None = None,
        transport: str = "uds",
        needs: str | None = None,
        answers: bool = True,
    ) -> dict:
        """`answers`: False for a message the hub sends about work (an assignment, a status notice). It must not
        close anyone's open flag, now or when a late question is journaled after it, nor count as a reply to the
        operator; the row is stored as a notice (`notice = 1`) so later checks skip it too."""
        needs = needs or None
        if needs not in NEEDS:
            raise ValueError("needs must be 'reply', 'blocking' or empty")
        mid = msg_id or f"m:{hashlib.sha1(f'{time.time_ns()}|{from_name}|{to_name}'.encode()).hexdigest()[:20]}"
        stamp = ts if ts is not None else time.time()
        with self.lock:
            cur = self.db.execute(
                "insert or ignore into messages "
                "(msg_id, ts, from_name, from_session, from_provider, to_name, to_session, to_provider, channel, squad_id, body, transport, needs, "
                "notice) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, stamp, from_name, from_session, from_provider, to_name, to_session, to_provider, channel, squad_id,
                 body, transport, needs, int(not answers)),
            )
            if cur.rowcount:
                row_id = cur.lastrowid
                if answers:
                    self._close_answered(row_id, stamp, from_name, to_name, squad_id)
                if needs:
                    self._close_if_answered(row_id, stamp, from_name, to_name, squad_id)
            else:
                row_id = (self.db.execute("select id from messages where msg_id = ?", (mid,)).fetchone() or (0,))[0]
            self.db.commit()
        entry = dict(
            id=row_id,
            msg_id=mid,
            ts=stamp,
            from_name=from_name,
            from_session=from_session,
            from_provider=from_provider,
            to_name=to_name,
            to_session=to_session,
            to_provider=to_provider,
            channel=channel,
            squad_id=squad_id,
            body=body,
            transport=transport,
            needs=needs,
            answered_ts=None,
            answered_by=None,
            read_ts=None,
        )
        with self.msg_cond:
            self.msg_cond.notify_all()
        return entry

    def _close_answered(self, row_id: int, ts: float, sender: str, to: str, squad_id: str | None) -> int:
        """Message `row_id` (sender -> to) was just journaled: close every earlier open flagged message that `to`
        sent to `sender`, or that `to` broadcast in a squad the reply does not contradict. Only a direct message
        answers; a broadcast closes nothing. Runs inside the caller's lock and transaction."""
        if not to or not sender or to[0] in "@#" or sender == to:
            return 0
        senders = OPERATOR_NAMES if to in OPERATOR_NAMES else (to,)          # who asked: the reply's recipient
        askees = OPERATOR_NAMES if sender in OPERATOR_NAMES else (sender,)   # whom they asked: the reply's sender
        if set(senders) & set(askees):
            return 0
        cur = self.db.execute(
            "update messages set answered_ts = ?, answered_by = ? "
            "where needs is not null and answered_ts is null and id < ? and ts <= ? "
            f"and from_name in ({','.join('?' * len(senders))}) "
            f"and (to_name in ({','.join('?' * len(askees))}) or substr(to_name, 1, 1) in ('@', '#')) "
            "and (squad_id is null or ? is null or squad_id = ?)",
            (ts, row_id, row_id, ts + ANSWER_SKEW_S, *senders, *askees, squad_id, squad_id))
        return cur.rowcount

    def _close_if_answered(self, row_id: int, ts: float, sender: str, to: str, squad_id: str | None) -> int:
        """The flagged message `row_id` (sender -> to) was just journaled: if its answer is already in the journal,
        close it now. That happens when the question came by native SendMessage (journaled only at the next
        ingest) and the answer went through the hub first. The answer is a direct message to the asker from the
        recipient (from anyone, for a question broadcast in a squad), no older than the question. Runs inside the
        caller's lock and transaction. No clock tolerance here, unlike `_close_answered`: a late question's
        transcript time is at or before when it was really sent, so its real answer is never older, and a window
        backwards would let a message the recipient sent just before the question close it."""
        if not to or not sender:
            return 0
        askers = OPERATOR_NAMES if sender in OPERATOR_NAMES else (sender,)
        if to[0] in "@#":
            who, who_params = f"from_name not in ({','.join('?' * len(askers))})", list(askers)
        else:
            repliers = OPERATOR_NAMES if to in OPERATOR_NAMES else (to,)
            who, who_params = f"from_name in ({','.join('?' * len(repliers))})", list(repliers)
        row = self.db.execute(
            f"select id, ts from messages where id != ? and ts >= ? and not coalesce(notice, 0) "
            f"and to_name in ({','.join('?' * len(askers))}) "
            f"and {who} and (squad_id is null or ? is null or squad_id = ?) order by ts asc, id asc limit 1",
            (row_id, ts, *askers, *who_params, squad_id, squad_id)).fetchone()
        if row is None:
            return 0
        return self.db.execute("update messages set answered_ts = ?, answered_by = ? where id = ? and answered_ts is null",
                               (row[1], row[0], row_id)).rowcount

    def messages(
        self,
        *,
        peer: str | None = None,
        channel: str | None = None,
        squad_id: str | None = None,
        since_id: int = 0,
        limit: int = 100,
        unscoped: bool = False,
    ) -> list[dict]:
        """`unscoped`: with `squad_id`, rows in no squad (an operator's @all) count too."""
        clauses = ["id > ?"]
        params: list = [max(0, int(since_id))]
        if peer:
            clauses.append("(from_name = ? or to_name = ? or from_session = ? or to_session = ? or to_name in ('@all', '@squad', '#squad'))")
            params.extend([peer, peer, peer, peer])
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if squad_id:
            clauses.append("(squad_id = ? or squad_id is null)" if unscoped else "squad_id = ?")
            params.append(squad_id)
        return self._message_rows(clauses, params, since_id, limit)

    def _message_rows(self, clauses: list[str], params: list, since_id: int, limit: int) -> list[dict]:
        """Rows matching `clauses`: the newest `limit` (oldest first), or with `since_id` the first `limit` after it."""
        lim = max(1, min(500, int(limit)))
        order = "asc" if since_id > 0 else "desc"
        sql = f"select {MESSAGE_COLUMNS} from messages where {' and '.join(clauses) or '1'} order by id {order} limit ?"
        with self.lock:
            rows = self.db.execute(sql, [*params, lim]).fetchall()
        if order == "desc":
            rows = list(reversed(rows))
        return [_message(r) for r in rows]

    def message(self, row_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute(f"select {MESSAGE_COLUMNS} from messages where id = ?", (int(row_id),)).fetchone()
        return _message(row) if row else None

    def resolve_message(self, row_id: int) -> bool:
        """Close an open flagged message by hand (answered_by 0)."""
        with self.lock:
            cur = self.db.execute("update messages set answered_ts = ?, answered_by = 0 "
                                  "where id = ? and needs is not null and answered_ts is null", (time.time(), int(row_id)))
            self.db.commit()
        return cur.rowcount > 0

    def open_needs(self, squad_id: str | None = None, to_name: str | None = None) -> list[dict]:
        """Open flagged messages, oldest first. `to_name` 'operator' also matches the hub's other names."""
        clauses, params = ["needs is not null", "answered_ts is null"], []
        if squad_id:
            clauses.append("squad_id = ?")
            params.append(squad_id)
        if to_name:
            names = OPERATOR_NAMES if to_name in OPERATOR_NAMES else (to_name,)
            clauses.append(f"to_name in ({','.join('?' * len(names))})")
            params.extend(names)
        with self.lock:
            rows = self.db.execute(f"select {MESSAGE_COLUMNS} from messages where {' and '.join(clauses)} "
                                   "order by id asc limit 500", params).fetchall()
        return [_message(r) for r in rows]

    # -- the operator's inbox: messages to the operator (by any of the hub's names) and from it

    def operator_counts(self, squad_id: str | None = None) -> dict:
        to_op = f"to_name in ({','.join('?' * len(OPERATOR_NAMES))})"
        squad = " and squad_id = ?" if squad_id else ""
        params = [*OPERATOR_NAMES, *([squad_id] if squad_id else [])]
        with self.lock:
            unread = self.db.execute(f"select count(*) from messages where {to_op} and read_ts is null{squad}",
                                     params).fetchone()[0]
            rows = self.db.execute(f"select needs, count(*), min(ts) from messages where {to_op} and needs is not null "
                                   f"and answered_ts is null{squad} group by needs", params).fetchall()
            latest = self.db.execute("select coalesce(max(id), 0) from messages").fetchone()[0]
        by = {r[0]: r for r in rows}
        blocking = by.get("blocking")
        return dict(unread=unread or 0,
                    open=dict(reply=by["reply"][1] if "reply" in by else 0, blocking=blocking[1] if blocking else 0),
                    oldest_blocking_ts=blocking[2] if blocking else None, latest_id=int(latest or 0))

    def operator_inbox(self, *, squad_id: str | None = None, since_id: int = 0, limit: int = 100) -> dict:
        """Counts, plus (unless `limit` is 0) the messages to or from the operator, oldest first."""
        doc = self.operator_counts(squad_id)
        doc["messages"] = []
        if int(limit) > 0:
            marks = ",".join("?" * len(OPERATOR_NAMES))
            # what the operator broadcast (launch notices, joins, @squad / #channel posts) stays in the contact stream:
            # it is no conversation, and a reply there would broadcast again
            # a notice the hub wrote (from almanac-hub) is not the operator's own outgoing mail
            clauses = ["id > ?", f"(to_name in ({marks}) or (from_name in ({marks}) and substr(to_name, 1, 1) not in ('@', '#') "
                                 "and not (coalesce(notice, 0) and from_name = 'almanac-hub')))"]
            params: list = [max(0, int(since_id)), *OPERATOR_NAMES, *OPERATOR_NAMES]
            if squad_id:
                clauses.append("squad_id = ?")
                params.append(squad_id)
            doc["messages"] = self._message_rows(clauses, params, int(since_id), limit)
        return doc

    def mark_read(self, ids: list[int] | None = None, upto: int | None = None) -> int:
        """Mark messages to the operator read, by id or every one up to `upto`. Returns rows changed."""
        to_op = f"to_name in ({','.join('?' * len(OPERATOR_NAMES))})"
        now = time.time()
        with self.lock:
            if upto is not None:
                cur = self.db.execute(f"update messages set read_ts = ? where read_ts is null and {to_op} and id <= ?",
                                      (now, *OPERATOR_NAMES, int(upto)))
            else:
                wanted = [int(i) for i in (ids or [])][:500]
                if not wanted:
                    return 0
                cur = self.db.execute(f"update messages set read_ts = ? where read_ts is null and {to_op} "
                                      f"and id in ({','.join('?' * len(wanted))})", (now, *OPERATOR_NAMES, *wanted))
            self.db.commit()
        return cur.rowcount

    def last_operator_exchange(self, agent: str, squad_id: str | None = None) -> dict | None:
        """The newest direct message between the operator and `agent`, either way, not in another squad."""
        marks = ",".join("?" * len(OPERATOR_NAMES))
        with self.lock:
            row = self.db.execute(
                f"select {MESSAGE_COLUMNS} from messages where ((from_name in ({marks}) and to_name = ?) "
                f"or (from_name = ? and to_name in ({marks}))) and (squad_id is null or ? is null or squad_id = ?) "
                "and not coalesce(notice, 0) order by id desc limit 1",
                (*OPERATOR_NAMES, agent, agent, *OPERATOR_NAMES, squad_id, squad_id)).fetchone()
        return _message(row) if row else None

    def latest_message_id(self) -> int:
        with self.lock:
            row = self.db.execute("select coalesce(max(id), 0) from messages").fetchone()
        return int(row[0] or 0)

    def wait_messages(
        self,
        *,
        peer: str | None = None,
        channel: str | None = None,
        squad_id: str | None = None,
        since_id: int = 0,
        timeout: float = 30.0,
        stop_event: threading.Event | None = None,
        unscoped: bool = False,
    ) -> list[dict]:
        deadline = time.monotonic() + max(0.0, min(600.0, float(timeout)))
        while True:
            found = self.messages(peer=peer, channel=channel, squad_id=squad_id, since_id=since_id, limit=100,
                                  unscoped=unscoped)
            # When waiting as a specific peer, exclude messages sent BY that peer itself
            if peer:
                found = [m for m in found if m["from_name"] != peer and m["from_session"] != peer]
            if found:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (stop_event is not None and stop_event.is_set()):
                return []
            with self.msg_cond:
                self.msg_cond.wait(timeout=min(remaining, 1.0))

    def roles(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "select id, title, provider, model, effort, workspace_mode, permission_mode, system_prompt, channels, updated_ts, "
                "may_page_operator, may_spawn, allowed_commands, add_dirs, may_restart, models, efforts, "
                "coalesce(context_policy, 'none') from roles order by id"
            ).fetchall()
        out = []
        for r in rows:
            try:
                ch = json.loads(r[8] or "[]")
            except ValueError:
                ch = []
            out.append(
                dict(
                    id=r[0],
                    title=r[1],
                    provider=r[2],
                    model=r[3] or "",
                    effort=r[4] or "",
                    workspace_mode=r[5] or "shared",
                    permission_mode=r[6] or "auto",
                    system_prompt=r[7] or "",
                    channels=ch if isinstance(ch, list) else [],
                    updated_ts=r[9],
                    may_page_operator=bool(r[10]),
                    may_spawn=bool(r[11]),
                    allowed_commands=[str(c) for c in _json_list(r[12])],
                    add_dirs=[str(d) for d in _json_list(r[13])],
                    may_restart=bool(r[14]),
                    models=[str(m) for m in _json_list(r[15])],
                    efforts=[str(e) for e in _json_list(r[16])],
                    context_policy=r[17],
                )
            )
        return out

    def role(self, role_id: str) -> dict | None:
        return next((r for r in self.roles() if r["id"] == role_id), None)

    def upsert_role(self, doc: dict) -> dict:
        import re as _re
        rid = str(doc.get("id") or "").strip().lower()
        if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", rid):
            raise ValueError("role id must be 1..63 lowercase letters, digits, '-' or '_'")
        old = self.role(rid)
        if old:
            # an update keeps every field the doc leaves out (the role form once sent no channels and still sends
            # no permission mode); a present key sets it, an empty one clears it. An explicit null keeps the stored
            # value for may_page_operator, may_spawn, allowed_commands and add_dirs (their SQL coalesce) and clears
            # the rest. add_dirs is not merged here, so a folder deleted since is not re-validated by a save that does
            # not touch it.
            doc = {**{k: v for k, v in old.items() if k != "add_dirs"}, **doc}
        provider = str(doc.get("provider") or "claude").strip().lower()
        if provider not in ("claude", "gemini"):
            raise ValueError("provider must be 'claude' or 'gemini'")
        wmode = str(doc.get("workspace_mode") or "shared").strip().lower()
        if wmode == "inherit":
            wmode = "shared"
        if wmode not in ("shared", "worktree"):
            raise ValueError("workspace_mode must be 'shared' or 'worktree'")
        pmode = str(doc.get("permission_mode") or "auto").strip()
        if pmode not in ("plan", "accept-edits", "auto", "bypass", "manual"):
            raise ValueError("permission_mode must be plan, accept-edits, auto, bypass, or manual")
        title = str(doc.get("title") or rid).strip()[:120]
        prompt = str(doc.get("system_prompt") or "").strip()
        if not prompt:
            raise ValueError("system_prompt is required")
        model_raw = str(doc.get("model") or "").strip()[:120]
        effort_raw = str(doc.get("effort") or "").strip()[:20]
        from almanac.spawner import split_model_effort
        norm_m, norm_e = split_model_effort(provider, model_raw or None, effort_raw or None)
        model = (norm_m or model_raw)[:120]
        effort = (norm_e or effort_raw)[:20]
        raw_channels = doc.get("channels") or []
        if isinstance(raw_channels, str):
            raw_channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
        # `review` typed without its `#` would never match a `#review` post
        channels = list(dict.fromkeys(c if c.startswith("#") else "#" + c
                                      for c in (str(c).strip() for c in raw_channels) if c))[:20]
        # absent: an update keeps what the role had, a new role may not page the operator
        page = doc.get("may_page_operator")
        page = None if page is None else int(page is True or str(page).strip().lower() in ("1", "true", "yes", "on"))
        grow = doc.get("may_spawn")
        grow = None if grow is None else int(grow is True or str(grow).strip().lower() in ("1", "true", "yes", "on"))
        models, efforts = _choices(doc, "models", lambda m: split_model_effort(provider, m)[0]), \
            _choices(doc, "efforts", lambda e: split_model_effort(provider, None, e)[1])
        # absent or null keeps the stored policy (a new role: none); anything else must be one of the three forms
        policy = doc.get("context_policy")
        policy = normalize_context_policy(policy) if policy is not None else (old or {}).get("context_policy") or "none"
        boot = doc.get("may_restart")
        boot = None if boot is None else int(boot is True or str(boot).strip().lower() in ("1", "true", "yes", "on"))
        cmds = doc.get("allowed_commands")              # a list, or one command per line; absent keeps what it was
        if isinstance(cmds, str):
            cmds = cmds.splitlines()
        cmds = None if cmds is None else json.dumps([str(c).strip() for c in cmds if str(c).strip()][:40])
        dirs = doc.get("add_dirs")                      # likewise: a list, or one folder per line
        if isinstance(dirs, str):
            dirs = dirs.splitlines()
        if dirs is not None:
            dirs = [d for d in (str(d).strip().strip('"').strip() for d in dirs) if d]   # Explorer's "Copy as path" quotes
            if len(dirs) > 20:
                raise ValueError(f"add_dirs takes at most 20 folders, not {len(dirs)}")
            bad = [d for d in dirs if not (Path(d).is_absolute() and Path(d).is_dir())]
            if bad:
                raise ValueError("add_dirs must be absolute paths of existing folders; not: " + ", ".join(bad))
            dirs = json.dumps(list(dict.fromkeys(str(Path(d).resolve()) for d in dirs)))   # `C:\a` and `C:\a\` once
        now = time.time()
        with self.lock:
            self.db.execute(
                "insert into roles (id, title, provider, model, effort, workspace_mode, permission_mode, system_prompt, channels, "
                "updated_ts, may_page_operator, may_spawn, allowed_commands, add_dirs, may_restart, models, efforts, "
                "context_policy) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "on conflict(id) do update set title=excluded.title, provider=excluded.provider, model=excluded.model, "
                "effort=excluded.effort, workspace_mode=excluded.workspace_mode, permission_mode=excluded.permission_mode, "
                "system_prompt=excluded.system_prompt, channels=excluded.channels, updated_ts=excluded.updated_ts, "
                "may_page_operator=coalesce(?, roles.may_page_operator), may_spawn=coalesce(?, roles.may_spawn), "
                "allowed_commands=coalesce(?, roles.allowed_commands), add_dirs=coalesce(?, roles.add_dirs), "
                "may_restart=coalesce(?, roles.may_restart), models=excluded.models, efforts=excluded.efforts, "
                "context_policy=excluded.context_policy",
                (rid, title, provider, model, effort, wmode, pmode, prompt, json.dumps(channels), now, page or 0, grow or 0,
                 cmds or "[]", dirs or "[]", boot or 0, json.dumps(models), json.dumps(efforts), policy, page, grow, cmds, dirs, boot),
            )
            self.db.commit()
        return self.role(rid)

    def delete_role(self, role_id: str) -> bool:
        with self.lock:
            cur = self.db.execute("delete from roles where id = ?", (role_id,))
            self.db.commit()
        return cur.rowcount > 0

    def _spawn_settings(self, spawn_role, spawn_limit) -> tuple[str | None, int]:
        role = str(spawn_role).strip() if spawn_role else None
        if role and role != ANY_ROLE and self.role(role) is None:
            raise ValueError(f"unknown role {role!r}")
        try:
            limit = int(spawn_limit or 0)
        except (TypeError, ValueError):
            raise ValueError("spawn_limit is a whole number 0..20") from None
        if not 0 <= limit <= 20:
            raise ValueError("spawn_limit is a whole number 0..20")
        return role, limit

    def create_squad(self, squad_id: str, name: str, cwd: str, brief: str = "", *, spawn_role: str | None = None,
                     spawn_limit: int = 0, plan_first: bool = False) -> dict:
        """`spawn_role` / `spawn_limit`: the role (or `*`, any defined role) and how many members an agent whose role
        may_spawn can add to this squad (elastic squads). 0 means agents add nobody; the operator always can.
        `plan_first`: an agent's mesh_spawn starts a member only on a slice of an accepted plan (door.spawn_covered)."""
        role, limit = self._spawn_settings(spawn_role, spawn_limit)
        now = time.time()
        with self.lock:
            self.db.execute(
                "insert or replace into squads (id, name, cwd, brief, created_ts, status, spawn_role, spawn_limit, "
                "plan_first) values (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (squad_id, name, cwd, brief, now, role, limit, int(bool(plan_first))),
            )
            self.db.commit()
        return dict(id=squad_id, name=name, cwd=cwd, brief=brief, created_ts=now, status="active", spawn_role=role,
                    spawn_limit=limit, spawn_used=0, plan_first=bool(plan_first))

    _KEEP = object()

    def delete_squad(self, squad_id: str) -> None:
        """Take back a squad row whose launch stopped before it had a member (a dispatch whose lead did not start).
        A spawn row the failed lead left keeps its squad_id, which then names no squad."""
        with self.lock:
            self.db.execute("delete from squads where id = ?", (squad_id,))
            self.db.commit()

    def update_squad(self, squad_id: str, *, spawn_role=_KEEP, spawn_limit=_KEEP, plan_first=_KEEP) -> dict | None:
        """Change what agents may add to a running squad. Left out = unchanged. None if there is no such squad."""
        current = self.squad(squad_id)
        if current is None:
            return None
        role, limit = self._spawn_settings(current["spawn_role"] if spawn_role is self._KEEP else spawn_role,
                                           current["spawn_limit"] if spawn_limit is self._KEEP else spawn_limit)
        first = current["plan_first"] if plan_first is self._KEEP else bool(plan_first)
        with self.lock:
            self.db.execute("update squads set spawn_role = ?, spawn_limit = ?, plan_first = ? where id = ?",
                            (role, limit, int(first), squad_id))
            self.db.commit()
        return self.squad(squad_id)

    def squad(self, squad_id: str) -> dict | None:
        found = self.squads(limit=1, squad_id=squad_id) if squad_id else []
        return found[0] if found else None

    def spawn_of(self, agent_name: str) -> dict | None:
        """The newest spawn row for a handle (handles are reused across squads), however many spawns there are."""
        found = self.spawned(limit=1, agent_name=agent_name) if agent_name else []
        return found[0] if found else None

    def squads(self, limit: int = 50, *, squad_id: str | None = None) -> list[dict]:
        """The newest `limit` squads with their agents, oldest agent first; with `squad_id`, just that one."""
        where, params = ("where id = ? ", [squad_id]) if squad_id else ("", [])
        with self.lock:
            rows = self.db.execute(
                "select id, name, cwd, brief, created_ts, status, spawn_role, coalesce(spawn_limit, 0), coalesce(plan_first, 0), "
                # members, not sessions: a respawn (door.respawn_task) reuses its handle and takes no slot; a handle
                # whose newest row was retired (fresh-per-task) gives its slot back
                "(select count(distinct a.agent_name) from spawned_agents a where a.squad_id = squads.id "
                "and a.spawned_by is not null and (select n.retired_ts from spawned_agents n where n.squad_id = a.squad_id "
                "and n.agent_name = a.agent_name order by n.created_ts desc, n.rowid desc limit 1) is null) "
                f"from squads {where}order by created_ts desc limit ?",
                (*params, max(1, min(200, int(limit)))),
            ).fetchall()
            agents_rows = self.db.execute(
                "select agent_name, session_id, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, created_ts, "
                f"spawned_by from spawned_agents {'where squad_id = ? ' if squad_id else ''}order by created_ts asc, rowid asc",
                params,
            ).fetchall()
        by_squad: dict[str, list[dict]] = {}
        for a in agents_rows:
            if a[2]:
                by_squad.setdefault(a[2], []).append(
                    dict(
                        agent_name=a[0],
                        session_id=a[1],
                        squad_id=a[2],
                        role_id=a[3],
                        provider=a[4],
                        model=a[5],
                        effort=a[6],
                        cwd=a[7],
                        worktree_path=a[8],
                        pid=a[9],
                        created_ts=a[10],
                        spawned_by=a[11],
                    )
                )
        return [
            dict(
                id=r[0],
                name=r[1],
                cwd=r[2],
                brief=r[3],
                created_ts=r[4],
                status=r[5],
                spawn_role=r[6],
                spawn_limit=r[7],
                spawn_used=r[9],
                plan_first=bool(r[8]),
                agents=by_squad.get(r[0], []),
            )
            for r in rows
        ]

    def record_spawn(
        self,
        *,
        agent_name: str,
        session_id: str | None,
        squad_id: str | None,
        role_id: str | None,
        provider: str,
        model: str | None,
        effort: str | None,
        cwd: str,
        worktree_path: str | None,
        pid: int,
        channels: list[str] | None = None,
        spawned_by: str | None = None,
        branch: str | None = None,
        base: str | None = None,
    ) -> dict:
        if not session_id:
            raise ValueError("a spawn record needs its session id")
        now = time.time()
        with self.lock:
            self.db.execute(
                "insert or replace into spawned_agents "
                "(agent_name, session_id, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, created_ts, channels, "
                "spawned_by, branch, base) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_name, session_id, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, now,
                 json.dumps(list(channels or [])), spawned_by, branch, base),
            )
            self.db.commit()
        return dict(
            agent_name=agent_name,
            session_id=session_id,
            squad_id=squad_id,
            role_id=role_id,
            provider=provider,
            model=model,
            effort=effort,
            cwd=cwd,
            worktree_path=worktree_path,
            pid=pid,
            created_ts=now,
            spawned_by=spawned_by,
            branch=branch,
            base=base,
        )

    def update_spawn_model(
        self,
        *,
        session_id: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        with self.lock:
            if session_id:
                self.db.execute(
                    "update spawned_agents set model = coalesce(?, model), effort = coalesce(?, effort) where session_id = ?",
                    (model, effort, session_id),
                )
                self.db.execute("update sessions set model = coalesce(?, model) where id = ?", (model, session_id))
            if agent_name:
                self.db.execute(
                    "update spawned_agents set model = coalesce(?, model), effort = coalesce(?, effort) where session_id = "
                    "(select session_id from spawned_agents where agent_name = ? order by created_ts desc, rowid desc limit 1)",
                    (model, effort, agent_name),
                )
            self.db.commit()

    def known_models(self) -> dict[str, list[str]]:
        by_prov: dict[str, list[str]] = {"claude": [], "gemini": []}
        with self.lock:
            rows = (
                self.db.execute("select coalesce(provider, 'claude'), model from sessions where model is not null and model != ''").fetchall()
                + self.db.execute(
                    "select coalesce(s.provider, 'claude'), u.model "
                    "from usage u left join sessions s on s.id = u.session "
                    "where u.model is not null and u.model != ''"
                ).fetchall()
                + self.db.execute("select provider, model from roles where model is not null and model != ''").fetchall()
                + self.db.execute("select provider, model from spawned_agents where model is not null and model != ''").fetchall()
            )
        for prov, m in rows:
            key = "gemini" if str(prov).lower() == "gemini" else "claude"
            ms = str(m).strip()
            if ms and ms != "<synthetic>" and ms not in by_prov[key]:
                by_prov[key].append(ms)
        return by_prov

    def spawned(self, squad_id: str | None = None, limit: int = 100, *, agent_name: str | None = None) -> list[dict]:
        """Spawn rows, newest first, optionally of one squad and/or one handle."""
        clauses, params = [], []
        for column, value in (("squad_id", squad_id), ("agent_name", agent_name)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"where {' and '.join(clauses)} " if clauses else ""
        with self.lock:
            rows = self.db.execute(
                "select agent_name, session_id, squad_id, role_id, provider, model, effort, cwd, worktree_path, pid, created_ts, "
                f"channels, spawned_by, retired_ts, branch, base from spawned_agents {where}order by created_ts desc, rowid desc "
                "limit ?",
                (*params, limit),
            ).fetchall()
        return [
            dict(
                agent_name=a[0],
                session_id=a[1],
                squad_id=a[2],
                role_id=a[3],
                provider=a[4],
                model=a[5],
                effort=a[6],
                cwd=a[7],
                worktree_path=a[8],
                pid=a[9],
                created_ts=a[10],
                channels=_json_list(a[11]),
                spawned_by=a[12],
                retired_ts=a[13],
                branch=a[14],
                base=a[15],
            )
            for a in rows
        ]

    # ------------------------------------------------------------------ work objects (stage 1: tasks)
    def create_object(self, *, kind: str, project: str, squad_id: str | None, title: str, body: str, status: str,
                      owner_session: str | None, parent_id: int | None, files: list[str], created_by: str | None,
                      created_by_name: str, who: dict, owner_name: str | None = None,
                      data: dict | None = None) -> dict:
        """Insert an object and its `create` log row in one transaction. `who`: the log row's session, agent_name,
        role_id, provider."""
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "insert into objects (kind, project, squad_id, title, body, status, owner_session, parent_id, files, "
                "created_by, created_by_name, created_ts, updated_ts, data) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, project, squad_id, title, body, status, owner_session, parent_id, json.dumps(files),
                 created_by, created_by_name, now, now, json.dumps(data or {})))
            oid = cur.lastrowid
            self._insert_log(oid, now, who, "create", "", None, status, owner_name, None)
            self.db.commit()
        return self.object_row(oid)

    def _insert_log(self, oid, ts, who, kind, body, status_from, status_to, owner_to, message_id, sha=None) -> int:
        return self.db.execute(
            f"insert into object_log ({LOG_COLUMNS.removeprefix('id, ')}) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, ts, who.get("session"), who.get("agent_name") or "operator", who.get("role_id"), who.get("provider"),
             kind, body, status_from, status_to, owner_to, message_id, sha)).lastrowid

    def log_object(self, oid: int, *, who: dict, kind: str, body: str = "", status: str | None = None,
                   owner_session: str | None = None, owner_name: str | None = None,
                   message_id: int | None = None, squad_id: str | None = None, head: dict | None = None) -> dict:
        """Append a log row; a `status`, `owner_session` or `squad_id` (a backlog task's dispatch) also changes the
        object (and its updated_ts). `head` {sha, branch, base}: what a hand-in shipped, set on the object (all three,
        a None included) and as the row's sha."""
        now = time.time()
        with self.lock:
            row = self.db.execute("select status from objects where id = ?", (oid,)).fetchone()
            if row is None:
                raise LookupError(oid)
            sets, params = ["updated_ts = ?"], [now]
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if owner_session is not None:
                sets.append("owner_session = ?")
                params.append(owner_session)
            if squad_id is not None:
                sets.append("squad_id = ?")
                params.append(squad_id)
            if head is not None:
                sets.append("sha = ?, branch = ?, base = ?")
                params.extend((head["sha"], head.get("branch"), head.get("base")))
            self.db.execute(f"update objects set {', '.join(sets)} where id = ?", (*params, oid))
            lid = self._insert_log(oid, now, who, kind, body, row[0] if status is not None else None, status,
                                   owner_name, message_id, head and head["sha"])
            self.db.commit()
            r = self.db.execute(f"select {LOG_COLUMNS} from object_log where id = ?", (lid,)).fetchone()
        return _log(r)

    def reparent_object(self, oid: int, parent_id: int | None, *, who: dict, body: str) -> dict:
        """Set (or clear, None) an object's parent and write the `parent` log row, in one transaction."""
        now = time.time()
        with self.lock:
            self.db.execute("update objects set parent_id = ?, updated_ts = ? where id = ?", (parent_id, now, oid))
            lid = self._insert_log(oid, now, who, "parent", body, None, None, None, None)
            self.db.commit()
            r = self.db.execute(f"select {LOG_COLUMNS} from object_log where id = ?", (lid,)).fetchone()
        return _log(r)

    def amend_object(self, oid: int, *, title: str, body: str, files: list[str], who: dict, log_body: str) -> dict:
        """Replace an object's title, body and files and write the `amend` log row (which holds the old text), in one
        transaction."""
        now = time.time()
        with self.lock:
            self.db.execute("update objects set title = ?, body = ?, files = ?, updated_ts = ? where id = ?",
                            (title, body, json.dumps(files), now, oid))
            lid = self._insert_log(oid, now, who, "amend", log_body, None, None, None, None)
            self.db.commit()
            r = self.db.execute(f"select {LOG_COLUMNS} from object_log where id = ?", (lid,)).fetchone()
        return _log(r)

    def amended_since(self, oid: int, ts: float) -> bool:
        """Has the object an `amend` log row at or after `ts`?"""
        with self.lock:
            return self.db.execute("select 1 from object_log where object_id = ? and kind = 'amend' and ts >= ? limit 1",
                                   (oid, ts)).fetchone() is not None

    def object_row(self, oid: int) -> dict | None:
        with self.lock:
            r = self.db.execute(f"select {OBJECT_COLUMNS} from objects where id = ?", (oid,)).fetchone()
        return _object(r) if r else None

    def objects(self, *, squad_id: str | None, kind: str | None = None, statuses=(), owner: str | None = None,
                q: str | None = None, project: str | None = None) -> list[dict]:
        """Matching objects of one squad, by id; `squad_id=None` is the backlog (of `project`, when given).
        `owner`: a handle, or 'none' for unowned."""
        clauses, params = ["squad_id is ?"], [squad_id]
        if project:
            clauses.append("project = ?")
            params.append(project)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if statuses:
            clauses.append(f"status in ({','.join('?' * len(statuses))})")
            params += list(statuses)
        if owner == "none":
            clauses.append("owner_session is null")
        elif owner:
            clauses.append("owner_session in (select session_id from spawned_agents where agent_name = ?)")
            params.append(owner)
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(title like ? escape '\\' or body like ? escape '\\')")
            params += [like, like]
        with self.lock:
            rows = self.db.execute(f"select {OBJECT_COLUMNS} from objects where {' and '.join(clauses)} order by id",
                                   params).fetchall()
        return [_object(r) for r in rows]

    def children(self, parent_id: int) -> list[dict]:
        """An object's direct children wherever they are (backlog or any squad), by id."""
        with self.lock:
            rows = self.db.execute(f"select {OBJECT_COLUMNS} from objects where parent_id = ? order by id",
                                   (parent_id,)).fetchall()
        return [_object(r) for r in rows]

    def object_stats(self, ids: list[int]) -> dict:
        """{id: dict(children=(total, accepted), log_count, last_log)} for the plan view's rows."""
        out = {i: dict(children=(0, 0), log_count=0, last_log=None) for i in ids}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        with self.lock:
            for pid, total, accepted in self.db.execute(
                    f"select parent_id, count(*), sum(status = 'accepted') from objects where parent_id in ({marks}) "
                    "group by parent_id", ids).fetchall():
                out[pid]["children"] = (total, accepted or 0)
            last_ids = []
            for oid, count, last in self.db.execute(
                    f"select object_id, count(*), max(id) from object_log where object_id in ({marks}) "
                    "group by object_id", ids).fetchall():
                out[oid]["log_count"] = count
                last_ids.append(last)
            if last_ids:
                for r in self.db.execute(f"select {LOG_COLUMNS} from object_log where id in "
                                         f"({','.join('?' * len(last_ids))})", last_ids).fetchall():
                    out[r[1]]["last_log"] = _log(r)
        return out

    def object_log(self, oid: int, limit: int = 20) -> list[dict]:
        """An object's log, newest first."""
        with self.lock:
            rows = self.db.execute(f"select {LOG_COLUMNS} from object_log where object_id = ? order by id desc limit ?",
                                   (oid, limit)).fetchall()
        return [_log(r) for r in rows]

    def objects_rev(self) -> int:
        with self.lock:
            return self.db.execute("select coalesce(max(id), 0) from object_log").fetchone()[0]

    def spawn_by_session(self, session_id: str) -> dict | None:
        with self.lock:
            a = self.db.execute(f"select {SPAWNED_COLUMNS}, retired_ts, branch, base from spawned_agents "
                                "where session_id = ?", (session_id,)).fetchone()
        return (dict(zip(SPAWNED_COLUMNS.split(", ") + ["retired_ts", "branch", "base"], a), channels=_json_list(a[11]))
                if a else None)

    def retire_spawn(self, session_id: str, retired: bool = True) -> None:
        """Mark a spawn row retired (fresh-per-task): its handle no longer counts toward the squad's spawn_used.
        `retired=False` takes that back (a session that could not be ended after all)."""
        with self.lock:
            if retired:
                self.db.execute("update spawned_agents set retired_ts = ? where session_id = ? and retired_ts is null",
                                (time.time(), session_id))
            else:
                self.db.execute("update spawned_agents set retired_ts = null where session_id = ?", (session_id,))
            self.db.commit()

    def merge_object_data(self, oid: int, **values) -> None:
        """Set keys of an object's `data` (its other keys kept), read and written as one step."""
        with self.lock:
            r = self.db.execute("select data from objects where id = ?", (oid,)).fetchone()
            if r is None:
                raise LookupError(oid)
            try:
                data = json.loads(r[0] or "{}")
            except ValueError:
                data = {}
            data = dict(data if isinstance(data, dict) else {}, **values)
            self.db.execute("update objects set data = ? where id = ?", (json.dumps(data), oid))
            self.db.commit()


CONTEXT_POLICY_FORMS = "none, fresh-per-task, or compact-at-<tokens> (compact-at-300000 or compact-at-300k)"


def normalize_context_policy(value) -> str:
    """A role's `context_policy` in its stored form: none, fresh-per-task, or compact-at-<N> with N whole tokens
    (`compact-at-300k` is read as 300000). Anything else is refused."""
    text = str(value if value is not None else "").strip().lower()
    m = re.fullmatch(r"compact-at-([0-9]{1,9})(k?)", text)
    if text in ("none", "fresh-per-task"):
        return text
    if m and int(m[1]) > 0:
        return f"compact-at-{int(m[1]) * (1000 if m[2] else 1)}"
    raise ValueError(f"context_policy must be {CONTEXT_POLICY_FORMS}, not {str(value)!r}")


def parse_context_policy(value) -> tuple[str, int | None]:
    """(kind, n): kind is `none`, `fresh` or `compact`; n is the token count for compact, else None. An empty value
    (a role stored before the column) is none; an invalid one raises ValueError."""
    text = normalize_context_policy(value) if value else "none"
    if text == "fresh-per-task":
        return "fresh", None
    if text.startswith("compact-at-"):
        return "compact", int(text[len("compact-at-"):])
    return "none", None


def _choices(doc: dict, key: str, norm) -> list[str]:
    """A role's `models` / `efforts`: a list or one per line or comma, each normalized, invalid ones refused, deduped.
    An update merges in the stored role first, so an absent key keeps what the role had."""
    raw = doc.get(key) or []
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if not isinstance(raw, list):
        raise ValueError(f"{key}: a list")
    out = []
    for item in (str(x).strip() for x in raw):
        if not item:
            continue
        value = norm(item)
        if not value:
            raise ValueError(f"{key}: {item!r} is not one")
        if value not in out:
            out.append(value)
    if len(out) > 12:
        raise ValueError(f"{key}: at most 12")
    return out


def _object(r) -> dict:
    try:
        data = json.loads(r[16] or "{}")
    except ValueError:
        data = {}
    return dict(zip(OBJECT_COLUMNS.split(", "), r), files=_json_list(r[9]), data=data if isinstance(data, dict) else {})


def _log(r) -> dict:
    return dict(zip(LOG_COLUMNS.split(", "), r))


def _message(r) -> dict:
    return dict(id=r[0], msg_id=r[1], ts=r[2], from_name=r[3], from_session=r[4], from_provider=r[5], to_name=r[6],
                to_session=r[7], to_provider=r[8], channel=r[9], squad_id=r[10], body=r[11], transport=r[12],
                needs=r[13], answered_ts=r[14], answered_by=r[15], read_ts=r[16])


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------- report pieces (plain functions, testable)

def _ratio(a, b):
    return round(a / b, 3) if b else None


def _kb_per_call(rows):
    sized = [r[7] for r in rows if r[7] is not None]
    return _ratio(sum(sized) / 1000, len(sized))


def _ctx_per_out(usage):
    out = sum(u[4] for u in usage)
    return _ratio(sum(u[1] + u[2] + u[3] for u in usage), out)


def _shell_min_per_session(rows):
    shell = [r for r in rows if r[1] in SHELL and r[10] == "live"]
    waits = [r[8] for r in shell if r[8] is not None and r[8] < 3600]
    sessions = {r[9] for r in shell}
    return _ratio(sum(waits) / 60, len(sessions))


def _adoption(rows):
    """Share of the four habits done the new way: new / (new + old), counting each call once per pair."""
    new = old = 0
    for r in rows:
        for score in r[3:7]:
            new += bool(score & 2)
            old += bool(score & 1)
    return _ratio(new, new + old)


def _pair_share(rows, index):
    new = sum(bool(r[index] & 2) for r in rows)
    old = sum(bool(r[index] & 1) for r in rows)
    return _ratio(new, new + old) if new + old else None


REPORT_PROVIDERS = (None, "claude", "gemini")
# report rows: calls are (ts, tool, family, search, tests, http, edit, out_chars, seconds, session, source, model,
# is_error, provider); usage is (ts, input, cache_create, cache_read, output, session, model, provider)
C_MODEL, C_ERROR, C_PROV = 11, 12, 13
U_MODEL, U_PROV = 6, 7


def _errors(rows) -> int:
    return sum(1 for r in rows if r[C_ERROR])


def _error_rate(rows):
    """Errors over the calls whose result arrived (is_error set). A call still waiting, a legacy call, or a period
    with no results at all does not count as a success: no results gives None, never 0."""
    done = [r[C_ERROR] for r in rows if r[C_ERROR] is not None]
    return _ratio(sum(1 for e in done if e), len(done))


def tiles(cur, old, cur_usage, old_usage) -> list[dict]:
    return [
        dict(id="kb_per_call", label="KB read back per tool call", value=_kb_per_call(cur), previous=_kb_per_call(old),
             unit="KB", better="down"),
        dict(id="ctx_per_out", label="Context tokens per output token", value=_ctx_per_out(cur_usage),
             previous=_ctx_per_out(old_usage), unit="×", better="down"),
        dict(id="shell_min", label="Shell wait per session", value=_shell_min_per_session(cur),
             previous=_shell_min_per_session(old), unit="min", better="down"),
        dict(id="adoption", label="Helper adoption", value=_adoption(cur), previous=_adoption(old), unit="%", better="up"),
        dict(id="error_rate", label="Tool error rate", value=_error_rate(cur), previous=_error_rate(old), unit="%",
             better="down"),
    ]


def _bucket_key(ts: float, bucket: str) -> str:
    day = datetime.fromtimestamp(ts).date()
    if bucket == "week":
        day = day - timedelta(days=day.weekday())
    return day.isoformat()


def series(calls, usage, start, end, bucket) -> list[dict]:
    keys = []
    step = timedelta(days=7 if bucket == "week" else 1)
    day = datetime.fromisoformat(_bucket_key(start, bucket))
    last = datetime.fromisoformat(_bucket_key(end, bucket))
    while day <= last:
        keys.append(day.date().isoformat())
        day += step
    by_calls = {k: [] for k in keys}
    by_usage = {k: [] for k in keys}
    for c in calls:
        by_calls.setdefault(_bucket_key(c[0], bucket), []).append(c)
    for u in usage:
        by_usage.setdefault(_bucket_key(u[0], bucket), []).append(u)
    out = []
    for k in keys:
        rows, us = by_calls.get(k, []), by_usage.get(k, [])
        shell = [r for r in rows if r[1] in SHELL]
        families: dict[str, int] = {}
        for r in shell:
            families[r[2] or "other"] = families.get(r[2] or "other", 0) + 1
        out.append(dict(
            bucket=k, calls=len(rows), shell_calls=len(shell), sessions=len({r[9] for r in rows}),
            kb_per_call=_kb_per_call(rows), out_kb=round(sum(r[7] or 0 for r in rows) / 1000, 1),
            ctx_per_out=_ctx_per_out(us), output_tokens=sum(u[4] for u in us),
            context_tokens=sum(u[1] + u[2] + u[3] for u in us),
            shell_min=round(sum(r[8] for r in shell if r[8] is not None and r[8] < 3600) / 60, 1),
            families=families, adoption={p: _pair_share(rows, 3 + i) for i, p in enumerate(classify.PAIRS)},
            legacy=sum(r[10] == "legacy" for r in rows), errors=_errors(rows), error_rate=_error_rate(rows)))
    return out


def family_table(cur, old) -> list[dict]:
    table: dict[str, dict] = {}
    for r in cur:
        if r[1] not in SHELL:
            continue
        row = table.setdefault(r[2] or "other", dict(family=r[2] or "other", calls=0, previous=0, waits=[], chars=[]))
        row["calls"] += 1
        if r[8] is not None and r[8] < 3600:
            row["waits"].append(r[8])
        if r[7] is not None:
            row["chars"].append(r[7])
    for r in old:
        if r[1] in SHELL:
            table.setdefault(r[2] or "other", dict(family=r[2] or "other", calls=0, previous=0, waits=[], chars=[]))["previous"] += 1
    out = []
    for row in table.values():
        waits, chars = row.pop("waits"), row.pop("chars")
        row.update(median_s=round(statistics.median(waits), 1) if waits else None,
                   total_min=round(sum(waits) / 60, 1), out_kb=round(sum(chars) / 1000, 1),
                   kb_per_call=round(sum(chars) / 1000 / len(chars), 2) if chars else None)
        out.append(row)
    return sorted(out, key=lambda r: (-r["calls"], r["family"]))


def tool_table(cur) -> list[dict]:
    table: dict[str, dict] = {}
    for r in cur:
        row = table.setdefault(r[1], dict(tool=r[1], calls=0, out_kb=0.0))
        row["calls"] += 1
        row["out_kb"] += (r[7] or 0) / 1000
    for row in table.values():
        row["out_kb"] = round(row["out_kb"], 1)
    return sorted(table.values(), key=lambda r: -r["calls"])[:15]


def _metrics(calls, usage) -> dict:
    """The per-model / per-provider columns for one group of call rows and usage rows."""
    return dict(
        sessions=len({r[9] for r in calls} | {u[5] for u in usage}), messages=len(usage),
        output_tokens=sum(u[4] for u in usage), context_tokens=sum(u[1] + u[2] + u[3] for u in usage),
        ctx_per_out=_ctx_per_out(usage), calls=len(calls), kb_per_call=_kb_per_call(calls),
        tool_errors=_errors(calls), error_rate=_error_rate(calls), shell_min=_shell_min_per_session(calls))


def _grouped(calls, usage, call_key, usage_key) -> dict:
    groups: dict = {}
    for r in calls:
        groups.setdefault(call_key(r), ([], []))[0].append(r)
    for u in usage:
        groups.setdefault(usage_key(u), ([], []))[1].append(u)
    return groups


def _model_call_key(r):
    if r[10] == "legacy":
        return ("legacy", None, None)
    return ("model", r[C_PROV], r[C_MODEL]) if r[C_MODEL] else ("unattributed", None, None)


def _model_usage_key(u):
    return ("model", u[U_PROV], u[U_MODEL]) if u[U_MODEL] and u[U_MODEL] != "<synthetic>" else ("unattributed", None, None)


def _with_previous(row: dict, before: dict | None) -> dict:
    row["previous"] = {k: (before or {}).get(k) for k in ("ctx_per_out", "kb_per_call", "error_rate")}
    return row


def model_table(cur, old, cur_usage, old_usage) -> list[dict]:
    """One row per (provider, model), busiest output first. Calls take the model of the message that issued them
    (calls.model) and tokens the model of their own message (usage.model), so a session that switches model mid-way
    splits between both rows. Calls with no model sit in one `unattributed` row; seeded legacy calls (no session,
    model or sizes) in one `legacy` row that carries only its call count. Both go last."""
    now = _grouped(cur, cur_usage, _model_call_key, _model_usage_key)
    then = _grouped(old, old_usage, _model_call_key, _model_usage_key)
    rows = []
    for (kind, prov, model), (calls, usage) in now.items():
        if kind == "legacy":
            rows.append(dict(kind="legacy", provider=None, model=None, calls=len(calls)))
            continue
        before = then.get((kind, prov, model))
        row = dict(kind=kind, provider=prov, model=model, **_metrics(calls, usage))
        rows.append(_with_previous(row, _metrics(*before) if before else None))
    order = {"model": 0, "unattributed": 1, "legacy": 2}
    return sorted(rows, key=lambda r: (order[r["kind"]], -(r.get("output_tokens") or 0), -r["calls"],
                                       r["provider"] or "", r["model"] or ""))


def provider_table(cur, old, cur_usage, old_usage) -> list[dict]:
    """The model table's metrics per provider. Legacy calls have no provider and are left out."""
    live, live_old = [r for r in cur if r[10] != "legacy"], [r for r in old if r[10] != "legacy"]
    now = _grouped(live, cur_usage, lambda r: r[C_PROV], lambda u: u[U_PROV])
    then = _grouped(live_old, old_usage, lambda r: r[C_PROV], lambda u: u[U_PROV])
    rows = [_with_previous(dict(provider=prov, **_metrics(*group)), _metrics(*then[prov]) if prov in then else None)
            for prov, group in now.items()]
    return sorted(rows, key=lambda r: (-r["output_tokens"], -r["calls"], r["provider"] or ""))


# ---------------------------------------------------------------------- worktime pieces (plain functions, testable)

BUCKETS = (10, 30, 60, 120, 300, 600, 900, 1800, 3600)
SLIVER_S = 0.5      # a working interval this short (a crashed agent's millisecond turn) is noise, not overlap
ANSWER_SKEW_S = 2.0    # hub times are float, native transcript times can be whole seconds
OPEN_STRETCH_S = 120   # a busy session's last turn is stretched to now only if it ended this recently
OPEN_TURN_S = 30       # else a provisional turn covers the last this-many seconds
MAX_BUCKETS = 240


def default_bucket(span_s: float) -> int:
    """The smallest bucket that draws `span_s` in at most MAX_BUCKETS columns."""
    return next((b for b in BUCKETS if span_s / b <= MAX_BUCKETS), BUCKETS[-1])


def is_wait_tool(tool: str | None) -> bool:
    """`mesh_wait` as Gemini names it, or as Claude Code names an MCP tool (`mcp__almanac-mesh__mesh_wait`)."""
    t = str(tool or "")
    return t == "mesh_wait" or t.endswith("__mesh_wait") or t.endswith("_mesh_wait")


def merge_intervals(intervals) -> list[tuple[float, float]]:
    """Sorted, non-overlapping; touching intervals join, empty or backwards ones drop."""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b is not None and a is not None and b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def subtract_intervals(keep, remove) -> list[tuple[float, float]]:
    """`keep` minus `remove`; both are merged first."""
    out = []
    remove = merge_intervals(remove)
    for a, b in merge_intervals(keep):
        cur = a
        for c, d in remove:
            if d <= cur or c >= b:
                continue
            if c > cur:
                out.append((cur, c))
            cur = max(cur, d)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return out


def intersect_intervals(x, y) -> list[tuple[float, float]]:
    return subtract_intervals(x, subtract_intervals(x, y))


def _total(intervals) -> float:
    return sum(b - a for a, b in intervals)


def _bucket(ts: float, start: float, bucket_s: float, n: int) -> int:
    return min(n - 1, max(0, int((ts - start) // bucket_s)))


def bucket_seconds(intervals, start: float, bucket_s: float, n: int) -> list[float]:
    """Seconds of `intervals` that fall in each of `n` buckets of `bucket_s` from `start`; the rest is cut off."""
    out = [0.0] * n
    end = start + bucket_s * n
    for a, b in merge_intervals(intervals):
        a, b = max(a, start), min(b, end)
        while a < b:
            i = int((a - start) // bucket_s)
            edge = min(b, start + (i + 1) * bucket_s)
            out[min(i, n - 1)] += edge - a
            a = edge
    return out


def concurrency(work_by_agent: dict, *, start: float | None = None, end: float | None = None) -> dict:
    """Exact overlap of working intervals (a sweep line, not buckets): seconds with k agents working (`by_count`,
    0 included when a range is given), each pair's shared seconds, each agent's seconds working alone, the peak,
    and the constant-count segments [(a, b, k)] that the caller buckets."""
    marks = []
    for agent, iv in work_by_agent.items():
        for a, b in merge_intervals(iv):
            if b - a >= SLIVER_S:                      # the one sliver rule: noise never enters the sweep, so every
                marks += [(a, 1, agent), (b, -1, agent)]   # figure below (counts, pairs, peak, segments) agrees
    marks.sort(key=lambda m: (m[0], m[1]))          # at a shared instant, an end comes before a start
    lo = start if start is not None else (marks[0][0] if marks else 0.0)
    hi = end if end is not None else (marks[-1][0] if marks else 0.0)
    by_count: dict[int, float] = {}
    pairs: dict[tuple, float] = {}
    solo = {agent: 0.0 for agent in work_by_agent}
    segments: list[tuple[float, float, int]] = []
    active: set = set()
    prev = lo
    for t, step, agent in marks + [(hi, 0, None)]:
        t = min(max(t, lo), hi)
        if t > prev:
            k = len(active)
            span = t - prev
            by_count[k] = by_count.get(k, 0.0) + span
            segments.append((prev, t, k))
            if k == 1:
                solo[next(iter(active))] += span
            names = sorted(active)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    pairs[(a, b)] = pairs.get((a, b), 0.0) + span
            prev = t
        if step == 1:
            active.add(agent)
        elif step == -1:
            active.discard(agent)
    peak = max((k for _, _, k in segments), default=0)
    any_s = sum(s for k, s in by_count.items() if k >= 1)
    return dict(
        by_count={k: round(s, 1) for k, s in sorted(by_count.items()) if s > 0},
        pairs=sorted((dict(a=a, b=b, overlap_s=round(s, 1)) for (a, b), s in pairs.items() if s > 0),
                     key=lambda p: -p["overlap_s"]),
        solo_s={a: round(s, 1) for a, s in solo.items()}, peak=peak, any_s=round(any_s, 1),
        parallel_s=round(sum(s for k, s in by_count.items() if k >= 2), 1), segments=segments)


def _empty_agent(session: str) -> dict:
    return dict(session_id=session, first_ts=None, last_ts=None, wall_s=0.0, work_s=0.0, wait_s=0.0, idle_s=0.0, turns=0,
                longest_turn_s=0.0, calls=0, tool_errors=0, output_tokens=0, work=[], wait=[], events=[], output=[])
