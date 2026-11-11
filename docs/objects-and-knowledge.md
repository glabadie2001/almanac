# Work objects and the Almanac Engram

Covers ROADMAP items 9 (task ledger), 10 (Engram for Almanac) and 14 (findings ledger). They share one primitive:
an addressable record that agents hold a reference to, look up, and log to.

## 1. Status (2026-11-11)

**No work object exists yet.** The plans for tasks-as-objects and an Almanac Engram, sent for planning on
2026-11-10, did not land. Nothing for them is on this machine, on origin (`main` and `cross-model-mesh`), or in the
mesh journal, and `~/.claude/plans/` holds nothing related. Almanac stores what it observes (session, call, turn,
usage) and what it coordinates (message, flag, squad, role). It does not store work.

### Object inventory

How close each concept is to an object an agent can hold a reference to. Paths are under `almanac/`.

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
| experiment | not stored; ROADMAP §7 proposes `experiments` | | | | | |

### Where a spawn task's text goes

1. Squad launch (`POST /api/squads`): `members[].task` → `prepared[].task` (spawner.py:708) → each teammate's charter
   roster line "— {task}" (spawner.py:180) → the member's kickoff, "## Your Initial Assignment" (spawner.py:732).
2. `mesh_spawn` and `POST /api/squad/member`: the door requires at least 20 characters → `join_squad(task=)`
   (spawner.py:826) → kickoff (spawner.py:879).
3. Transport: Claude gets the kickoff as an argv item or a stream-json stdin frame (spawner.py:535–545). Gemini gets
   it as `.almanac/spawns/<name>-<sid8>/initial_prompt.md` (spawner.py:605).
4. What outlives the session: `sessions.first_prompt` keeps the first **200 characters**, whitespace-collapsed
   (store.py:507), which is the "## Squad Mission" header, not the task. A `mesh_spawn` join notice to `@squad` keeps
   `task[:300]` (spawner.py:911); `actions.detail` says only "X joined S as R". `record_spawn` (store.py:1344) has no
   task column. The full text survives only in the raw transcript (and its archive copy).

## 2. Design draft v1

> **Draft, under review by rev-gemini.** Their findings will be folded in as a follow-up edit. Where the draft and
> the code disagree today, the disagreement is marked **Code check**.

**D1 One primitive.** A new `objects` table. Its log is the existing `messages` table with a nullable `object_id`
and an entry `kind`, not a second log table: attribution, journaling, the stream UI and its filters already exist
there. A ref is a kind letter plus the row id: `T-12` task, `F-13` finding, `N-14` note.

**D2 Scope.** Tasks and findings belong to a squad. Notes belong to a project (the main checkout root, so every
worktree shares one set) and outlive squads.

**D3 Columns.** `id, kind, project, squad_id, title, status, owner_session, parent_id` (finding → task, note →
the note it supersedes), `paths` (globs), `sha, branch, created_by` (session), `created_ts, updated_ts`, `data` (JSON
for kind-specific fields). The status vocabulary per kind lives in code.

**D4 Agent surface: four tools.**
- `mesh_object(ref)`: the object, its log tail and derived stats.
- `mesh_objects(kind, status, owner, path, q)`: list and search (LIKE first, FTS5 when that falls short).
- `mesh_log(ref, body, kind, status?)`: append, optionally changing status.
- `mesh_file(kind, title, body, paths?, parent?)`: create.

`mesh_send` gains `re="T-12"`, and a message body that names a ref is linked to it. A slice report is then one act:
the report is the task's log entry and is delivered to the architect and reviewer.
*Code check:* body-scan linking will also catch refs quoted in prose ("unlike T-12…"). Treat `re=` as the link and
body mentions as soft links, or link a body mention only when the ref is in the sender's squad.

**D5 Derived, never written.** For a task with an owner session and in-progress intervals, Almanac joins calls
(edits, errors), usage (tokens, cost), turns (worktime), linked messages and the worktree branch's commits. Agents
write only what cannot be derived: what was learned, dead ends (`dead`, or `parked: retry when X`), and what comes
next.
*Code check, edited files:* `calls` stores **no file path**. `cmd` is recorded only for shell tools, and then only
its `head` (leading word) and length (store.py:525, 561–565); Edit/Write inputs are dropped. "Files a task touched"
needs a new `calls.path` column filled from `input.file_path`, plus a one-time transcript re-read (the v6 pattern:
`delete from files`). The alternative is the branch diff below, which covers worktree agents only.
*Code check, branch commits:* the snapshot a worktree was cut from is not stored. `ensure_worktree` passes
`snapshot_commit()` straight to `git worktree add` (spawner.py:268). When the checkout was dirty, that snapshot is an
extra "almanac: uncommitted work at spawn" commit, so `git log <base>..<branch>` from any stored ref would
include it. Listing the branch's commits is cheap (one `git log`) once `spawned_agents` gets a `base_sha` column
written at spawn. Shared-checkout agents have no branch; for them, only `calls.path` works.

**D6 Read path.** Both kickoff builders add a brief: your task ref and its log tail (a respawn with the same ref is
item 13's handoff), the squad's open tasks as one-liners, and up to N notes whose paths intersect the task's paths,
dead ends first, each with a freshness marker. Hard cap about 40 lines; everything else is pulled with the tools.
Works for Gemini; needs no hook.
*Code check:* Claude's kickoff travels as one argv item (spawner.py:545), so a brief has to stay well under
Windows' 32,767-character command line together with the charter and other arguments. 40 lines fits; the cap
should be in characters, not only lines.

**D7 Trust.** A note is born a `claim`, signed with session, role, model, sha and branch. `verified` requires a
different session to re-check it against the code. Anyone may log a contest, which makes it `contested`. Then
`superseded` or `retracted`. Operator notes are born `directive`. Trust order: code > directive > verified and
fresh > claim. A brief carries directives and verified notes, plus claims from the reader's own squad only.
*Code check:* "a different session" is enforceable only after D11. Today the door knows the handle, and handles
repeat across squads.

**D8 Freshness.** Computed on read (brief build, `mesh_object`), never on the 2-second poll:
`git log <sha>..HEAD -- <paths>` in the main checkout gives fresh, or drifted with the files named. A sha that is
not an ancestor of the main checkout's HEAD makes the note `pending on <branch>`, visible only to its squad until
merged. No manifest sidecar in v1.
*Code check, two problems:*
1. "Merged" never happens today. Worktree work is integrated with `git apply` of `git diff <snapshot>..HEAD`
   (ROADMAP §12), so the agent's commits never become ancestors of the main checkout's HEAD, and a note signed in a
   worktree would stay `pending` forever. Either integration becomes a real merge or cherry-pick (item 12), or the
   integrator re-stamps pending notes to the integration commit, or freshness is checked with
   `git diff <sha> HEAD -- <paths>` (content, not ancestry), which works across `apply`.
2. The main checkout is not on `main`: it is on `cross-model-mesh` (local `main` is at 97a7c44). Use the
   checkout's HEAD, never a branch name.

**D9 Budgets and anchoring.** A note body is capped, and the door refuses an oversized one with "trim". A note needs
at least one path, or an explicit project scope. Corrections supersede; they do not append contradictions.

**D10 Portability.** Verified and directive notes are exported to markdown in the repo, written only by Almanac,
on demand and at squad end. Knowledge then travels by git to another machine and is readable by sessions not
launched from Almanac. Tasks, findings and logs stay in the database.

**D11 Prerequisite.** Agent tokens carry the session id (ROADMAP "identity at the door is its handle"). Attribution
of log entries depends on it.

**D12 Later.** A librarian at squad end: one fresh agent proposes consolidated notes from the squad's task logs.

**Cut from Engram for v1:** metrics events and the scorecard, architecture diagrams, manifests, the status line,
monthly digests, wikilinks (refs replace them), skills and hooks.

## 3. Seams

Where the work attaches in today's code:

- **Store.** `_migrate` (store.py:265); additive DDL goes in `SCHEMA` (store.py:29). The stamp
  `insert or replace into meta values ('schema', '8')` is near the end of `_migrate`, so this is **v9**. Follow the
  existing pattern: `create table if not exists`, `pragma table_info` checks before `alter table`, and a
  `version < 9` block for backfill.
- **Door.** The GET and POST branches of `make_handler._dispatch` (door.py:1246–1360). A route reachable by an
  agent token must sit under `/api/mesh/` (`AGENT_PATHS`, web.py:64). The caller is `self.agent` (a handle, see
  D11), and its squad is derived the way `/api/mesh/send` derives it.
- **MCP.** The tool list in mcp_mesh.py (entries at :27–200), the `MeshClient` methods (:238–330), `handle_rpc`
  (:333), the `--cli` verbs, and charter line 6, which names those verbs (spawner.py:157).
- **Kickoff and charter.** `build_mesh_charter` (spawner.py:~140–185) for the tool text. There are two kickoff
  builders, `launch_squad` (spawner.py:728–738) and `join_squad` (spawner.py:876–890). Both create the T object and
  put its ref under "## Your Initial Assignment".
- **Messages.** A nullable `messages.object_id`, set by `re=`. A flag could also be closed by naming the message
  it answers, which would fix the inferred closing at store.py:952.
- **Spawns.** `spawned_agents.base_sha` (D5) and, later, the task ref.

## 4. Stages

| Stage | What | Size |
|---|---|---|
| 0 | Identity: agent tokens carry the session id | S |
| 1 | Tasks: `objects`, `messages.object_id`, the four tools, both spawn paths create a T and put its ref in the kickoff, a board in MESH. Includes `calls.path` and `spawned_agents.base_sha` if D5's stats are wanted in v1 | M |
| 2 | Findings as a kind with verdicts (item 14) | S–M |
| 3 | Notes, trust states, freshness, the brief | M |
| 4 | Export, librarian, respawn with handoff (item 13) | M |

Carried over from the earlier ROADMAP text of items 9 and 14:

- **Task statuses:** queued, in progress, in review, accepted, blocked. The task passed to `mesh_spawn` becomes the
  new member's first T. MESH gets a board grouped by owner, and heatmap stretches are labelled with their task.
- **Finding fields:** file:line, `bug` | `risk` | `nit`, confidence, and whether it has a repro. The architect
  records a verdict (`fix now` | `defer` | `reject`) with a reason. The fix links back when it lands, and a re-run
  marks it verified. Joined with usage and worktime, this gives tokens and minutes per accepted finding, acceptance
  rate by reviewer model, the share of nits, and how often a fix causes a regression.

## 5. Open questions for the operator

1. Should durable knowledge live in the database only, or also be exported to markdown in the repo? Export is recommended.
2. Who may mark a claim verified: any other session (recommended), the reviewer role only, or only the operator?
3. Should notes be per project only (recommended), or shared across projects too?
4. Should stages 0 and 1 be built now?
5. (From the D8 check.) Should item 12 make integration a real merge, so that notes signed on a branch become
   fresh once integrated? Or should freshness compare content (`git diff`) instead of ancestry?
