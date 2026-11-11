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

## 2. Design v2

v1 was reviewed by rev-gemini, and the architect ruled on each finding. The decisions below are v2.

**D1 One primitive, its own log.** A new `objects` table and a separate `object_log` table, with an optional
`message_id` when an entry came in through `mesh_send`. A ref is a kind letter plus the row id: `T-12` task,
`F-13` finding, `N-14` note.

**D2 Scope.** Tasks and findings belong to a squad. Notes belong to a project (the main checkout root, so every
worktree shares one set) and outlive squads.

**D3 Columns.** `objects`: `id, kind, project, squad_id, title, status, owner_session, parent_id` (finding → task,
note → the note it supersedes), `files` (repo-relative paths or globs, canonicalized to forward slashes relative to
the checkout root), `sha, branch, created_by` (session), `created_ts, updated_ts`, and `data` (JSON for
kind-specific fields). `object_log`: `id, object_id, ts, session, agent_name, role_id, provider, kind, body,
status_from, status_to, sha, message_id`. The status vocabulary per kind lives in code. A task is created with
`files`, which ROADMAP item 9 always had. When none are given, repo paths named in the task text are the
fallback.

**D4 Agent surface: four tools, explicit links only.**
- `mesh_object(ref)`: the object and its log tail.
- `mesh_objects(kind, status, owner, path, q)`: list and search (LIKE first, FTS5 when that falls short).
- `mesh_log(ref, body, kind, status?)`: append, optionally changing status.
- `mesh_file(kind, title, body, files?, parent?)`: create.

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
| 1 | Tasks only: `objects`, `object_log`, the four tools, `mesh_send(re=)`, status-change notice to the creator, both spawn paths create a T with `files` and put its ref and log tail in the kickoff, a board in MESH | M |
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

## 5. Open questions for the operator

1. Should verified knowledge live in the database only, or also be exported to markdown in the repo on your
   action? Export is recommended, with D10's restrictions.
2. Who may mark a claim verified? The reviewer role or the operator is recommended; the alternative is the
   operator only.
3. Should notes be per project only (recommended), or shared across projects too?
4. Should stages 0 and 1 be built now?
