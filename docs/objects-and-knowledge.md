# Work objects and the Almanac Engram

This doc covers ROADMAP items 9 (task ledger), 10 (Engram for Almanac) and 14 (findings ledger). All three need
the same primitive: an addressable record that agents hold a reference to, look up, and log to.

## 1. Status (2026-11-11)

**No work object exists yet.** The plans for tasks-as-objects and an Almanac Engram were sent for planning on
2026-11-10 and did not land. Nothing for them is on this machine, on origin (`main` and `cross-model-mesh`), or in
the mesh journal, and `~/.claude/plans/` holds nothing related. Almanac stores what it observes (session, call,
turn, usage) and what it coordinates (message, flag, squad, role). It does not store work.

### Object inventory

How close each concept is to an object an agent can hold a reference to. Paths are under `almanac/` as of
b3bbabb.

| Concept | Stored | Id | Agent receives the id | Agent can look it up | Agent can append | Lifecycle |
|---|---|---|---|---|---|---|
| session | `sessions` (store.py:31), from transcript ingest | uuid, minted at spawner.py:441 | yes, `session_id` in `mesh_roster` rows | yes, `mesh_context_stream(peer=<sid>)`, including ended sessions from the archive. Any agent can read any session (ROADMAP "Left open") | no, only by working | none: first_ts / last_ts |
| agent | `spawned_agents`, keyed by session_id (store.py:88) | handle `agent_name`, **reused** across squads; a lookup by name takes the newest row. The door token carries the handle, not the session (web.py:70) | yes, its own and its teammates' handles in the charter | roster (live peers only); ended agents appear only in `/api/mesh/graph` | no | live or ended is derived from the process; no stored state |
| squad | `squads` (store.py:76) | slug `name-hex` | yes, in the charter | partly: `/api/mesh` returns the last 20 squads. No get-by-id; `/api/squads` is outside the agent token's scope | no; the brief is set at creation | `status` defaults to `active` and **nothing ever writes it** (spawner.py:840 reads it) |
| role | `roles` (store.py:64) | text slug | yes, in the charter and the roster's `agent_role` | yes, `/api/mesh` returns full roles | no | none; operator CRUD |
| message | `messages` (store.py:47) | rowid `id`, hex `msg_id` | **sender only**: the `mesh_send` result and `mesh_contact_stream` rows. The inbound `<cross-session-message>` envelope carries no id (uds.py `format_cross_session`) | by range (`since_id`), no get-by-id | no; a reply is a new row | append-only |
| open flag (`needs`) | columns on `messages` (schema v5) | the message rowid | as for message | `open` in `/api/mesh`, `open_needs()` (store.py:1042) | no explicit act: a reply closes the flag by **inferring** a sender/recipient match (`_close_answered`, store.py:952), not by naming it | open → answered, or resolved by the operator |
| channel | not an entity: `messages.channel` text, JSON lists on `roles` and `spawned_agents` | the name | yes | `mesh_contact_stream(channel=)` | yes, by posting | none |
| turn | `turns`, key (session, start_ts) (store.py:43), derived | composite | no | no: `/api/worktime` is outside `/api/mesh` (web.py:64) | no | derived |
| call | `calls`, natural key / `tool_use_id` (store.py:33) | composite | no | no | no | derived |
| action | `actions` rowid (store.py:45); `log_action` cuts detail to 500 chars | rowid | no | no: `/api/actions` is the operator's | no | append-only |
| worktree / branch | `spawned_agents.worktree_path`. The branch `almanac/<squad>/<name>` and the snapshot it was cut from are **not stored** | path | yes, in the kickoff text (spawner.py:884) | roster `worktree_path` | n/a | none (ROADMAP §12) |
| spawn task | **not a field anywhere**; see the trace below | none | its own text, in the kickoff | no | no | none |
| finding | prose in message bodies | none | | | | |
| journal note | not stored; native Engram was not installed (ROADMAP §10) | | | | | |
| experiment | not stored; ROADMAP §5 proposes `experiments` | | | | | |

### Where a spawn task's text goes

1. Squad launch (`POST /api/squads`): `members[].task` → `prepared[].task` (spawner.py:708) → each teammate's charter
   roster line "— {task}" (spawner.py:180) → the member's kickoff, "## Your Initial Assignment" (spawner.py:732).
2. `mesh_spawn` and `POST /api/squad/member`: the door requires at least 20 characters → `join_squad(task=)`
   (spawner.py:826) → kickoff (spawner.py:879).
3. Transport: Claude gets the kickoff as an argv item or a stream-json stdin frame (spawner.py:535–545). Gemini gets
   it as `.almanac/spawns/<name>-<sid8>/initial_prompt.md` (spawner.py:605).
4. What outlives the session: `sessions.first_prompt` keeps the first **200 characters**, whitespace-collapsed
   (store.py:507). That is the "## Squad Mission" header, not the task. A `mesh_spawn` join notice to `@squad` keeps
   `task[:300]` (spawner.py:911); `actions.detail` says only "X joined S as R". `record_spawn` (store.py:1344) has
   no task column. The full text survives only in the raw transcript and its archive copy.

## How this maps to Engram

Source: Engram's `DESIGN.md` and `CONCEPTS.md` (`gskills-claude/engram`). Engram stores facts in files by how fast
they change, and counters five ways a memory dies. Here is where each piece lands in Almanac.

| Engram layer | Almanac object |
|---|---|
| `journal/` (append-only, what happened) | `object_log` entries on a task: learned, dead end, next. Engram's Did / Touched / Commits lines are derived in stage 4, not written |
| `tasks.md` (Now / Next / Later / Done) | task objects `T`, with statuses (queued, in progress, in review, accepted, blocked) |
| `gotchas.md` (dated bullets with file refs) | note `N`, kind gotcha, anchored to files |
| `decisions/` ADRs (never edited, superseded by a new ADR) | note, kind decision; never edited, superseded through `parent_id`. The operator's are directives |
| atlas cards (what the code is, sha-stamped, ≤60 lines) | note, kind card. The last kind to build. Like Engram's "judgment, not inventory" rule, it holds judgment, never a list `grep` could produce |
| dead ends (`dead` / `parked`, first-class in the journal) | a log entry on the task; it becomes a note once verified, so it outlives the squad |
| `MEMORY.md` (always loaded, 120-line cap) | the kickoff brief, built per agent and task instead of one static file (about 40 lines) |
| bug-sweep module: `sweeps/` ledger | findings `F`, stage 2 |
| bug-sweep module: `bug-classes.md` (taxonomy and hunt heuristics) | a note kind, not a finding: it describes a class, not one instance. Later than stage 3 |
| `architecture.md` (Live vs Target diagrams) | not mapped; cut from v1 |
| metrics events and scorecard | partly covered by Almanac's telemetry (tokens, calls, worktime per model). Engram's own measure, the recall **hit rate** (memory answered vs code explored), has no counterpart here, so whether the brief helps stays unmeasured |

| Engram death mode | Counter here |
|---|---|
| 1. Write-nothing | Handing in a slice *is* the log entry (`mesh_log` with the status change; the hub notifies the creator). The librarian sweeps at squad end. A task in progress whose owner session ended with no entry is flagged |
| 2. Stale-confident | The door stamps the sha. Freshness is computed on read, `pending` covers unmerged branches, and commits since verification are shown |
| 3. Bloat | Body caps refused at the door, the brief's line and character caps, ranking; everything else is pulled |
| 4. Write-only | The brief is in every kickoff, for both providers, with no hook; the tools serve pull |
| 5. Fragmentation | One ref per fact; corrections supersede; a message links with `re=` instead of restating |
| 6. Spread of a wrong note (only a shared base has this) | A claim versus verified by a different role class; claims stay in their squad; directives only from an operator route; notes wrapped as data. Engram has a write-path poisoning guard, but its one writer limits the spread |

**What Almanac adds that file-based Engram cannot.** It has one writer. Engram's CONCEPTS.md entry
"hub-and-spoke memory reconciliation" asks for a hub, a librarian and a pending overlay for unmerged code. Here
the hub is Almanac's store, the librarian is D12, and the overlay is D8's `pending`. CONCEPTS.md argues for a git
remote over a bespoke server, on the grounds that a server either syncs dumbly (last write wins) or is smart,
and then the intelligence belongs in the reconciler. That holds here: the store never merges text, because each
object has one owner and an append-only log, and judgment stays with the librarian and the verifier. Beyond that,
attribution is stamped by the server rather than self-reported, verification crosses providers, and Gemini gets
the same read path.

**What it gives up.** Plain files in git that any session can grep and that travel with a clone, which D10's
export only partly restores. Also Engram's post-compaction reminder: a kickoff brief arrives once, so a long-lived
agent that compacts does not get it again (item 13's respawn with handoff is the answer). And content-addressed
freshness: Engram's manifest counts a revert to verified content as current again, while D8's ancestry and
file-diff check marks it `drifted`.

**Trust order, side by side.** Engram: code > fresh atlas and decisions > journal, with user directives
(`(per user, date)`) outranking inference. Almanac: code > directive > verified and fresh > claim. The directive is
Engram's user directive, given a route only the operator can call.

## 2. Design v2

v1 was reviewed by rev-gemini, and the architect ruled on each finding. The decisions below are v2.

**D1 One primitive, its own log.** A new `objects` table and a separate `object_log` table, with an optional
`message_id` when an entry came in through `mesh_send`. A ref is a kind letter plus the row id: `T-12` task,
`F-13` finding, `N-14` note.

**D2 Scope.** Tasks and findings belong to a squad. Notes belong to a project (the main checkout root, so every
worktree shares one set) and outlive squads.

**D3 Columns.** `objects`: `id, kind, project, squad_id, title, body, status, owner_session, parent_id` (task →
the task it is part of, so a squad's tasks form a tree; finding → task; note → the note it supersedes), `files` (repo-relative paths or globs, canonicalized to forward slashes relative to
the checkout root), `sha, branch, created_by` (session), `created_ts, updated_ts`, and `data` (JSON for
kind-specific fields). `object_log`: `id, object_id, ts, session, agent_name, role_id, provider, kind, body,
status_from, status_to, sha, message_id`. The status vocabulary per kind lives in code. A task is created with
`files`, which ROADMAP item 9 always had. When none are given, repo paths named in the task text are the
fallback.

**D4 Agent surface: four tools, explicit links only.**
- `mesh_object(ref)`: the object and its log tail.
- `mesh_objects(kind, status, owner, path, q)`: list and search (LIKE first, FTS5 when that falls short).
- `mesh_log(ref, body, kind?, status?, owner?)`: append, optionally changing status or owner.
- `mesh_file(kind, title, body, files?, owner?, parent?, status?)`: create. Filing with an owner is the assignment.

Stage 1's exact contract for these is in "Stage 1 spec: the plan view" below.

`mesh_send` gains `re="T-12"`, which logs the message on that object. Nothing else links: no scanning bodies for
refs. A status change made through `mesh_log` makes the hub send one notice to the object's creator. A plain
log entry notifies nobody.

**D5 Derived stats, stage 4.** Agents write only what cannot be derived: what was learned, dead ends (`dead`, or
`parked: retry when X`), and what comes next. Joining calls, usage, turns and commits onto a task waits until
stage 4. It needs three things that do not exist yet:
- `calls.path`: `calls` stores **no file path**. `cmd` is recorded only for shell tools, and then only its `head`
  and length (store.py:525, 561–565). Edit and Write inputs are dropped.
- `spawned_agents.base_sha`: the snapshot a worktree was cut from is not stored. `ensure_worktree` passes
  `snapshot_commit()` straight to `git worktree add` (spawner.py:268), and when the checkout was dirty, an extra
  "almanac: uncommitted work at spawn" commit sits under the agent's work.
- **One in-progress task per session**, so a call or a turn belongs to exactly one task. Gemini usage is written
  after the whole `agy` turn ends (gemini_host.py), so attribution by time is approximate for Gemini.

**D6 Read path.** Both kickoff builders add a brief. It holds your task ref and its log tail (a respawn with the
same ref is item 13's handoff), and the squad's open tasks as one-liners. From stage 3 it also holds up to N
notes that match the task's `files`, ranked exact file > glob > project scope, with project-scope notes held to
at most 2 slots. Each note carries a freshness marker. Notes are wrapped in the brief as data, not instructions.
The cap is about 40 lines **and** a character budget: Claude's kickoff is one argv item (spawner.py:545) and
shares Windows' 32,767-character command line with the charter. Everything else is pulled with the tools. This
works for Gemini and needs no hook.

**D7 Trust.** A note is born a `claim`. The door stamps it with session, role, provider, model, sha and branch;
the client sends none of them. `verified` requires a verifier of a different role class than the author: the
reviewer role or the operator. The provider is recorded and shown. Anyone may log a contest, which makes the note
`contested`. A note ends as `superseded` or `retracted`. Operator notes are born `directive`, and directives are
minted only on an `/api/operator/*` route, never on `/api/mesh/*`, so the token-file fallback in `mcp_mesh` cannot
mint one. Trust order: code > directive > verified and fresh > claim. A brief carries directives and verified
notes, plus claims from the reader's own squad only. A dead end leads the brief only once verified.

**D8 Freshness.** Computed on read (brief build, `mesh_object`), never on the 2-second poll. A note is `fresh`
when its sha is an ancestor of the main checkout's HEAD (`git merge-base --is-ancestor`) and
`git diff --name-only <sha>..HEAD` names none of its files. If that diff names any of them, the note is `drifted`
with those files named. If the sha is not an ancestor, the note is `pending on <branch>`, visible only to its
squad. HEAD always means the checkout's HEAD, never a branch name: the checkout is on `cross-model-mesh`, and
local `main` is stale. The git work is one `git diff --name-only` per distinct sha, cached by (sha, HEAD), with
globs matched by `fnmatch`. Beside `fresh`, a note shows how many commits have landed since it was verified, to
cover drift outside its files.
**Verifying re-stamps.** Worktree work is integrated with `git apply` (ROADMAP §12), so an agent's commits never
become ancestors of HEAD. A note signed on a branch therefore stays `pending` until someone verifies it. The door
stamps the verifier's own HEAD as the note's sha, so a reviewer on the main checkout verifying after integration
is what lifts `pending`. No separate mechanism.

**D9 Budgets and anchoring.** A note body is capped, and the door refuses an oversized one with "trim". A note
needs at least one file, or an explicit project scope. Corrections supersede; they do not append contradictions.

**D10 Portability, stage 4, on the operator's action only.** Verified and directive notes can be exported to
markdown in the repo. Only Almanac writes the files, each carries its sha, and an export runs only when no
worktree in the squad is live, because writing into the checkout would break `git apply --3way` for any
integration still pending. Knowledge then travels by git to another machine and is readable by sessions not
launched from Almanac. Tasks, findings and logs stay in the database.

**D11 Prerequisite.** Agent tokens carry the session id (ROADMAP "identity at the door is its handle"). Attribution
of log entries depends on it.

**D12 Later.** A librarian at squad end: one fresh agent proposes consolidated notes from the squad's task logs.

**Cut from Engram for v1:** metrics events and the scorecard, architecture diagrams, manifests, the status line,
monthly digests, wikilinks (refs replace them), skills and hooks.

### Changed after review

- **D1 overturned.** v1 logged into `messages` with an `object_id`, because attribution, journaling and the stream
  UI already exist there. But `messages` feeds topology nodes, flag closing, `mesh_wait` wakeups, the operator
  inbox and more than 20 queries in store.py. Log entries would have appeared as phantom messages in all of
  them, and `to_name` is `not null`. The smaller diff was in the wrong place, so v2 uses a separate `object_log`.
- **D4 body scanning cut.** `[TFN]-\d+` also matches `UTF-8` and refs quoted in prose. Only `re=` links. Report
  fan-out became one notice to the creator on a status change.
- **D5 moved to stage 4.** Edit paths are not recorded, the worktree base is not stored, and one session can hold
  two tasks or one turn can span two. Gemini usage also lags a whole turn.
- **D7 "a different session" replaced.** A second session of the same role proves nothing, so the verifier must
  hold a different role class. The door stamps every sha; clients never send one. Directives come only from an
  operator route.
- **D8 kept ancestry and dropped the content-diff idea.** Comparing content alone would call an unmerged
  branch's note on an untouched file `fresh`, leaking it across squads. It would also call integrated notes
  `drifted` once HEAD moves. Instead of a separate re-stamp step at integration, verification re-stamps.
- **D6 ranking added.** Wildcard and project-scope notes would starve file-specific ones. Tasks had no files to
  match against, so `files` was added.
- **D10 restricted.** An export dirties the checkout and breaks `apply --3way`, so it runs only on the operator's
  action with no live worktree.
- **Kept as v1:** stage 1 is tasks only. The review proposed bringing the notes brief forward; the architect
  rejected that, because the operator's ask is a reference an agent can look up and log to.

## 3. Seams

Where the work attaches in today's code:

- **Store.** `_migrate` (store.py:265), with additive DDL in `SCHEMA` (store.py:29). The add_dirs slice took schema
  **v9**, so `objects` and `object_log` are **v10**. Follow the existing pattern: `create table if not exists`,
  `pragma table_info` checks before `alter table`, and a `version < 10` block for backfill.
- **Door.** The GET and POST branches of `make_handler._dispatch` (door.py:1246–1360). A route reachable by an
  agent token must sit under `/api/mesh/` (`AGENT_PATHS`, web.py:64); a route minting directives must not. The
  caller is `self.agent` (a handle, see D11), and its squad is derived the way `/api/mesh/send` derives it.
- **MCP.** The tool list in mcp_mesh.py (entries at :27–200), the `MeshClient` methods (:238–330), `handle_rpc`
  (:333), the `--cli` verbs, and charter line 6, which names those verbs (spawner.py:157).
- **Kickoff and charter.** `build_mesh_charter` (spawner.py:~140–185) for the tool text. There are two kickoff
  builders, `launch_squad` (spawner.py:728–738) and `join_squad` (spawner.py:876–890). Both create the T object,
  with `files` from the member spec or the `mesh_spawn` body, and put its ref under "## Your Initial Assignment".
- **Messages.** `mesh_send(re=)` writes an `object_log` row with `message_id`. `messages` itself gets no new
  column.
- **Spawns (stage 4).** `spawned_agents.base_sha`, and `calls.path` filled from `input.file_path` with a one-time
  transcript re-read (the v6 pattern: `delete from files`).

## 4. Stages

| Stage | What | Size |
|---|---|---|
| 0 | Identity: agent tokens carry the session id. `web.agent_token` is called at spawner.py:422, before the session id is minted at :445, so reorder those first | S |
| 1 | The plan view: tasks only, as a tree per squad with owners. `objects`, `object_log`, the routes and four tools, `mesh_send(re=)`, assignment by filing with an owner, the status-change notice, both spawn paths file a T, the charter protocol, and PLAN at the top of MESH. Spec below | M |
| 2 | Findings as a kind with verdicts (item 14) | S–M |
| 3 | Notes, trust states, freshness, the brief's notes section | M |
| 4 | Derived stats (`calls.path`, `base_sha`, one in-progress task per session), export, librarian, respawn with handoff (item 13) | M |

Carried over from the earlier ROADMAP text of items 9 and 14:

- **Task statuses:** queued, in progress, in review, accepted, blocked. The task passed to `mesh_spawn` becomes the
  new member's first T. MESH gets a board grouped by owner, and heatmap stretches are labelled with their task.
- **Finding fields:** file:line, `bug` | `risk` | `nit`, confidence, and whether it has a repro. The architect
  records a verdict (`fix now` | `defer` | `reject`) with a reason. The fix links back when it lands, and a re-run
  marks it verified. Joined with usage and worktime, this gives tokens and minutes per accepted finding, acceptance
  rate by reviewer model, the share of nits, and how often a fix causes a regression.

## Stage 1 spec: the plan view

Goal: the operator sees the squad's plan and who is working on which part. This section is the contract between
a backend and a UI implementer working in parallel. It is a spec, not a build.

### Model

- **Tree.** A squad's tasks form a tree: top-level tasks have no parent, and `parent_id` makes parts of parts. The
  parent must be a task in the same squad. It is set only at creation, so there can be no cycles. Stage 1 accepts
  only `kind = task`; any other kind is refused with `400 "kind <k>: not yet"`.
- **Statuses:** `queued`, `in progress`, `in review`, `accepted`, `blocked`. When none is given, a new task is
  `in progress` if it has an owner and `queued` if not. Any status may follow any other; stage 1 enforces no
  transitions.
- **Owner.** A live or ended member of the task's squad, given by handle. The door resolves the handle to that
  squad's newest `spawned_agents` row for it, which is the session, and stores `owner_session`. An unknown handle is
  refused with `400`. Until stage 0 lands, the handle is the identity, with ROADMAP's reuse caveat.
- **Assignment.** Filing a task with an owner, or changing its owner, makes the hub send the owner one message
  from the caller: `"[T-12 assigned to you] <title>\n\n<body>\n\nFiles: <files>\nHand in with mesh_log(ref=\"T-12\",
  status=\"in review\")"`. The message is journaled, and an `object_log` row (`kind = assign`, `message_id`)
  links it. An owner that has ended gets the row and a journaled message, which is undelivered; the result says so
  (`delivered: false`).
- **Spawn paths.** Both `launch_squad` and `join_squad` file a T for a member that has a task: title is the first
  line of the task (cut at 120 characters), body is the whole text, the owner is the new member, status is
  `in progress`, and the creator is the caller (the operator, or the agent that called `mesh_spawn`). The ref goes
  in the kickoff under "## Your Initial Assignment (`T-12`)". A member launched with only the mission gets no T.
  **No assignment message is sent on this path**: the kickoff already carries the task, and a second prompt right
  after it restarts the newcomer's turn (the join notice skips the newcomer for this reason, spawner.py:915).
- **Charter protocol** (`build_mesh_charter`, every agent, about five lines): architects file each slice with
  `mesh_file(kind="task", owner=..., files=...)` instead of describing it in a message; owners hand in with
  `mesh_log(ref, status="in review", body=<files touched, test command and result, what is left>)`; log what
  was learned, dead ends (`dead:` or `parked: retry when ...`) and what comes next as you go; reply about a task
  with `mesh_send(..., re="T-12")`.
- **Who may write.** An agent reads, files and logs only in its own squad; any member may do so. Anyone may change
  status or owner in stage 1. The log records who did, and the view shows it. The operator names a squad
  explicitly; see the default rule under the API contract.
- **Status-change notice.** A status change, through `mesh_log` or `POST /api/mesh/object/log`, sends one hub
  message to the task's creator: `"[T-12 <from> → <to> by <agent>] <title>\n\n<body of the entry>"`. It goes to
  `operator` when the operator created the task, and it is not sent when the creator made the change. The notice is
  not linked as a second log row.

### API contract

All routes are under `/api/mesh/`, so agent tokens reach them (web.py:257) and so does the operator. Errors use
the existing shape: `400 {"error": ...}` for bad input, `404` for an unknown ref, `403` for another squad's
object.

**Squad default.** For an agent, the squad is its own (as in `/api/mesh/send`), and a `squad=` that differs is
refused with `403`. For the operator, `squad` is required, except that with exactly one live squad it defaults to
that squad (the `@squad` rule, door.py:746).

**Refs.** A ref is the kind letter plus the row id (`T-12`). Routes accept the ref or the bare id; responses always
carry both.

`GET /api/mesh/objects?squad=&kind=&status=&owner=&q=&limit=`. `status` takes a comma list, and `owner` takes a
handle or `none` for unowned tasks. `q` matches title and body with LIKE. `limit` defaults to 200, maximum 500.
Rows come in tree order: parents before their children, siblings by `id`.

```json
{"objects": [{
  "ref": "T-12", "id": 12, "kind": "task", "title": "...", "status": "in progress",
  "squad_id": "alpha-squad-563100", "parent": "T-3",
  "owner_name": "impl-claude", "owner_session": "8da9...", "owner_state": "busy",
  "files": ["almanac/store.py"], "created_by_name": "arch-claude",
  "created_ts": 1794415400.1, "updated_ts": 1794416200.5,
  "children": {"total": 3, "accepted": 1},
  "log_count": 4,
  "last_log": {"ts": 1794416200.5, "agent_name": "impl-claude", "kind": "status",
               "body": "first 200 characters", "status_to": "in review"}
}], "rev": 4711}
```

Two additions to the architect's draft contract. `owner_state` (`busy` | `idle` | `ended` | `null` when unowned) is
computed by the door from `mesh_peers()`; this keeps the view from joining against the roster, which lists live
peers only, so an ended owner is absent from it. `children` is computed server-side for `accepted n/m`. `rev` is
the newest `object_log.id`, for refresh (see the view).

`GET /api/mesh/object?ref=T-12&log_limit=20` (log_limit maximum 500) returns
`{"object": {...the row above, "body": "..."}, "log": [{"id", "ts", "agent_name", "role_id", "provider", "kind",
"body", "status_from", "status_to", "owner_to", "message_id"}]}`, with the log newest first.

`POST /api/mesh/object` takes `{kind, title, body, files?, owner?, parent?, status?, squad?}` and returns
`{"object": {...}, "delivered": true | false | null}` (null: no owner). `title` is required, 1–120 characters;
`body` is at most 8,000 characters, and an oversized one is refused with "trim". `files` is a list or one per
line, repo-relative, stored with forward slashes, at most 50. The creation writes a `kind = create` log row.

`POST /api/mesh/object/log` takes `{ref, body, kind?, status?, owner?}` and returns
`{"object": {...}, "entry": {...}, "delivered": ...}`. `kind` defaults to `note`, and becomes `status` or
`assign` when those change; the agent-facing kinds are `note`, `learned`, `dead`, `parked` and `next`. A body is
required unless status or owner changes. `delivered` refers to the assignment message, if any.

`POST /api/mesh/send` accepts `re` ("T-12"). The message is journaled as today, and one `object_log` row
(`kind = message`, `message_id`, body = first 200 characters) is added to the task. An unknown or foreign `re`
is refused with `400` before anything is sent.

`GET /api/mesh` gains `objects_rev`: the newest `object_log.id`, or 0.

**Tools** (mcp_mesh.py), one to one: `mesh_objects(squad?, kind?, status?, owner?, q?, limit?)` → GET objects;
`mesh_object(ref, log_limit?)` → GET object; `mesh_file(kind, title, body, files?, owner?, parent?, status?)` →
POST object; `mesh_log(ref, body, kind?, status?, owner?)` → POST object/log; `mesh_send` gains `re`. The `--cli`
verbs gain `objects | object | file | log`. Agents alive across the restart that ships this keep their old tool
list until respawned (ROADMAP "Left open"), so the CLI fallback is their route.

**Storage (schema v10).**

```sql
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
```

`created_by_name` and `agent_name` are stored as well as the session, because the view shows names, and an
operator row has no session.

### The view

PLAN sits at the top of the MESH tab, for the selected squad (`meshState.squadFilter`).

- **Header:** the squad's mission (its brief, collapsed to two lines with a toggle) and counts by status.
- **Rows:** one per task, indented by depth. Each row shows the ref, the title, a status pill and an owner chip
  coloured by `owner_state` (an `ended` owner is dimmed, and an unowned task says "unowned"). Then the file count,
  and the last log line with its age ("impl-claude · in review · 4m"). A parent shows `accepted n/m` from
  `children`.
- **By owner:** a toggle regroups the same rows under one heading per owner, with "unowned" last. The tree's
  indent is dropped in this mode and the parent ref is shown instead.
- **Detail:** a click opens the task (body, files, full log) through `GET /api/mesh/object`, in the same detail pane
  the peer context uses.
- **Refresh:** **Code check:** MESH has no refresh separate from the 2-second poll. `loadMesh()` runs every 2
  seconds on the MESH tab (app.js:2840), and the button calls it too. So PLAN loads when the tab opens and when the
  `objects_rev` that `loadMesh()` already receives from `/api/mesh` changes; owner chips follow the roster on every
  poll without a refetch. That is one integer per poll, plus a fetch only after a write.
- **Empty state:** "No tasks filed. The architect files them with mesh_file."
- **Operator actions:** none in stage 1. The view is read-only; the operator files and logs through the same
  routes (curl or a later form). A file form is the first stage-1.5 candidate.

### Order and parallel work

1. **Stage 0, identity** (small, first). Agent tokens carry the session: `agent.<name>.<session>.<sig>`, and the
   door's `self.agent_session`. **Code check:** `web.agent_token` is called at spawner.py:422, before the session
   id is minted at :445, so move the uuid up. `agent_of` (web.py:78) takes the name as everything between the
   first and last dot, so it would read `name.session` as the name: it must split three ways, and the signature
   must cover `agent:<name>:<session>`. Names and uuids contain no dots, so the split is unambiguous. Old
   two-part tokens keep parsing as handle-only until their agents end.
2. **Backend A:** store (v10 tables, the queries above) and the routes, with tests through the door handler like
   `test_squad_channel.py`.
3. **Backend B** (after A's store methods): tools and CLI verbs, `mesh_send(re=)`, both spawn paths, the
   charter lines.
   **Code check:** `launch_squad` builds every member's kickoff before any spawn, and the session id is minted
   inside `spawn_agent`. So the T is filed first, unowned (`queued`), with its ref in the kickoff. After a
   successful spawn, its owner is set to the new session and its status to `in progress`, without the
   assignment message. A spawn that fails leaves the T queued and unowned, which the plan shows honestly.
   `join_squad` works the same way.
4. **UI** in parallel with 2 and 3, against the JSON above. A fixture response is enough to start.

`messages`, flag closing, the inbox and the topology are untouched: `object_log` is separate (D1).

## 5. Open questions for the operator

1. Should verified knowledge live in the database only, or also be exported to markdown in the repo on your
   action? Export is recommended, with D10's restrictions.
2. Who may mark a claim verified? The reviewer role or the operator is recommended; the alternative is the
   operator only.
3. Should notes be per project only (recommended), or shared across projects too?
4. Should stages 0 and 1 be built now?
