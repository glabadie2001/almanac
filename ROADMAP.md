# Almanac roadmap

Feature candidates from the alpha-squad review of 2026-11-10 (architect Claude, implementer Claude, reviewer
Gemini), ranked, and what the alpha-squad builds of 2026-11-10 and 2026-11-11 shipped. Each open item names the code it would
build on, so it can be picked up cold.

Effort: S = an afternoon, M = a few days, L = a week or more.

## Shipped in the 2026-11-11 build

Design and status: `docs/objects-and-knowledge.md`; the Engram debate: `docs/engram-debate-2026-11-11.md`.

**9. Tasks as objects, and the plan view.** Stage 0: agent tokens carry the session, so a reused handle no longer
binds to another squad's agent. Stage 1: schema v10 `objects` and `object_log`; tasks (`T-12`) form a tree per
squad with an owner and a status (queued, in progress, in review, accepted, blocked); `mesh_file`, `mesh_log`,
`mesh_object`, `mesh_objects` and their CLI verbs; `mesh_send(re=)` logs a message on a task; filing with an owner
is the assignment, delivered as one message; a status change notifies the creator; both spawn paths file the new
member's task and put its ref in the kickoff; the charter teaches the protocol. MESH opens with PLAN: the squad's
mission, counts by status, the task tree with owner state, regrouped by owner on a toggle, a task's log on a
click. Hub messages that report on work are **notices** (`messages.notice`): they close no flag and grant no
reply pass.

**Stalled agents recover.** A squad agent idle on an API error (`isApiErrorMessage`) is stalled: FLEET, GALLERY
and MESH show it. On a transient error (`server_error`, `overloaded_error`, `timeout`) Almanac sends `continue`
after 20, 60 and 180 seconds, then escalates once to the operator as a blocking inbox message and to the agent
that added it; an escalation closes itself when the agent works again and survives a restart. Only agents Almanac
spawned into an active squad are touched. `auto_continue` in `config.json` turns it off. Gemini turns that fail
are marked the same way (`gemini_error`, `runner_exited`, `runner_error`).

**Orchestrator role and a safe restart.** `orchestrator-claude` ships with `may_restart`; `mesh_restart` (and
`POST /api/mesh/restart` for the operator) runs a preflight on a copy of the database, tells every live squad,
and exits 75; `run.ps1`, `run.cmd` and `run.sh` start Almanac again. Refused while a headless Claude agent is
live, and without a launcher.

**Markdown in messages and chats**, rendered without ever dropping text; hostile input stays text.

**Gemini usage per turn.** agy reports the conversation's running totals in every result (measured: they carry on
across a relaunch with `--conversation`); ingest stores each turn's own counts, and one re-read repaired history.
rev-gemini's session read 19.3M input, 336M cache-read and 2.26M output; it is 1.08M, 20.3M and 134k.

**Elastic model and effort.** A role may allow more than one model and effort; the ROLES form picks them from
two multi-select dropdowns, and the first ticked (by capability) is the role's default. An agent adding a member
through `mesh_spawn` picks from that set and gives a reason (`why`); when the role offers more than one, it must name
the model and effort. The choice is kept on the new member's task. The operator's own spawns keep their free
overrides.

**Backlog tasks and one-click dispatch** (the operator's ask). A task can be filed to the project's backlog with no
squad yet, carrying the squad to launch (roster, spawn role and limit). MESH lists queued backlog tasks in a BACKLOG
box; one confirmed click launches the squad, and the task becomes its lead's task, so nothing is filed twice. Only
the operator dispatches; agents may file, read and log to their project's backlog. A lead that fails to start
starts nobody else and leaves the task queued; a hand-in on a task filed outside the squad notifies the operator.

**Also:** a Gemini role's `add_dirs` (extra workspace folders, `--add-dir`); `#squad` reaches every member of the
sender's squad (role-form saves had wiped every role's channels, so it reached nobody); a role saved from the form
keeps what the form does not send (`permission_mode` was reset too); the role form edits channels, allowed
commands, folders and `may_restart`.

## Shipped in the 2026-11-10 build

**3. Operator channel and message flags.** Messages carry `needs` (`reply` / `blocking`), open until the recipient
sends the sender a direct message (schema v5: `needs`, `answered_ts`, `answered_by`, `read_ts`). Roles gain
`may_page_operator`; an agent may always answer the operator's last message once. The INBOX tab (threads per agent
and squad, reply by session id, resolve by hand, opt-in browser notifications), the header `[✉ n]` badge and the
`blocked on operator` alert. FLEET and GALLERY got the `all | dialog | contacts | tools` filter, applied server side.
Not done: `needs` is not offered in FLEET's and GALLERY's SAY boxes (the route takes it).

**4. Mesh topology and work swimlane.** `GET /api/mesh/graph` and a fixed-column SVG (operator | architect |
implementer | reviewer | other, channels on a rail), edges by direction with counts and open flags; a click filters
the contact stream to a pair. The swimlane sits on the worktime heatmap (`heat + events`): tool errors, file edits
and messages per agent. MESH opens on the newest squad with a live member. Not done: never looked at in a browser
(see below); no auto-layout for more than 20 agents (a hint instead).

**6. Telemetry by provider and model, with tool error rate.** Built by `impl-telemetry` in a worktree and applied to
the main checkout. A provider filter, a BY MODEL table (sessions, output, context per output token, KB per call,
error rate, each against the previous period; unattributed and legacy rows apart), an error-rate tile and trend.
`calls.model` (schema v6) attributes each call to the model that issued it, also across a mid-session switch.

**7. Gallery: ended tiles, worktree badge, squad filter.** Built by `impl-gallery` in a worktree and applied to
the main checkout. Pinned and squad tiles stay when their session ends (dashed, `ended hh:mm`, the last output
fetched once more through the context stream, `[CHAT ▸]` / `[DISMISS ✕]`, at most 12, kept across reloads); the
folder shows its last two segments with `⑂` for a worktree (`/api/fleet` rows now carry `worktree_path`); the show
filter adds `busy` and `squad`. Not done: never looked at in a browser.

**Worktime tracking and the squad heatmap** (the operator's own ask). Schema v6 `turns`, read from the transcripts:
a turn runs from a prompt to the last record before the next one, time in `mesh_wait` is waiting, the rest of a
turn is work, and the remainder of an agent's span is idle. `GET /api/worktime` gives per-agent seconds, per-bucket
heat, and exact overlap (seconds with k agents working, pairs, solo time, peak). MESH draws it as a heatmap with a
PARALLEL row; FLEET, GALLERY and the MESH context head show `31m work`.

**Elastic squads.** Schema v7: a squad's `spawn_role` (one role, or `*` for any defined role) and `spawn_limit`
(0 to 20) let an agent whose role has `may_spawn` (architects by default) add members through `mesh_spawn` /
`POST /api/mesh/spawn`. Workspace and permission mode always come from the role's definition, never from the
agent; so did model and effort until 2026-11-11, when a role may list other allowed ones (see that build). The limit
check and the spawn hold one lock. The operator sets both at launch or in the squad strip
and adds members freely with **[+ ADD MEMBER]**. The newcomer's kickoff carries the brief, its task, who added it,
and for a worktree its branch; the squad hears who joined.

**Runner lookup and the missing-runner alert.** `config.find_gemini` / `find_claude` probe the configured path,
the installers' folders and PATH, again on use (at most every 10 s), so an install made while Almanac runs is
found on the next spawn. A Gemini spawn without a runner is refused with where Almanac looked; a squad still
launches its other members. `GET /api/health`, header chips, two boot lines (`READY, WITH WARNINGS.`) and
`(runner missing)` in the spawn drawers.

**Port-named session cookie.** `almanac_session_<port>`: a second Almanac on the same host no longer signs the
first one's browser out (browsers scope cookies by host, not port).

**Worktrees in a repo that ignores its state folder.** `snapshot_commit` no longer names an ignored `.almanac`
in a pathspec (git refused it, so worktree agents never started in this very repo); git's real error leads the
message; one member that cannot start no longer orphans a squad launch, and `POST /api/spawn` answers 409, not 500.

Also: the agent charter's CLI fallback runs with the agent's own token (it used to point at the door's master token
file), and `mesh_roster` lists the role ids an agent may name.

## Candidates

## 1. Launch drawer: per-member task and permission mode — S

The backend already takes both and the forms hide them.

- `spawner.launch_squad` turns `members[].task` into a "Your Initial Assignment" section; `POST /api/spawn`
  and each squad member accept `permission_mode`. Neither field exists in `#spawn-form`, `#squad-form`,
  `#role-form` or `addSquadMemberRow` (`static/app.js`).
- Add a permission select (`plan`, `accept-edits`, `auto`, `bypass`; mark `bypass` as dangerous) and a task
  input per member row.
- `seedDefaultSquadMembers` hardcodes handles. Changing a row's role leaves the handle alone, which is how the
  review squad got a Claude agent called `impl-gemini`. Re-derive the handle from the role unless it was typed.
- Gemini: `agy` reports "--mode auto is not available for this account" and falls back to its default mode.
  Show that in the drawer rather than letting it pass silently.

## 2. Squad lifecycle and post-flight review — M to L

A squad has no end and no account of itself: `squads.status` never leaves `active`, there is no end time, and
"what did this squad cost and who did what" needs the transcripts.

- `POST /api/squads/<id>/end`: stop the agents through the existing kill path, set status and an end time,
  report `ended K/N`. Never delete a worktree with uncommitted work.
- `GET /api/squads/<id>/review`: per agent and in total, joined from `spawned_agents.session_id`:
  - tokens and cost (`usage`, `config.prices`), tool calls, tool errors (`calls.is_error`), shell wait
    (`calls.seconds`), messages sent and received (`messages.squad_id`), first and last activity, and now the
    worktime (`Store.worktime`) and who added whom (`spawned_agents.spawned_by`);
  - **edits and writes per agent**: `calls.tool` in `store.EDIT_TOOLS` **plus shell calls that edit files**
    (`calls.family = 'edit'`; check it catches heredoc patch scripts and `sed -i`). The swimlane already draws
    these marks; the review needs the counts.
  - flag a role doing another role's job: any edit by an agent whose role brief says it does not edit
    (architect, reviewer).
  - `git diff --stat` of each worktree against its snapshot commit. Run it on demand or at end, never on the
    2-second poll.
- Trigger an ingest when the review is opened; a just-ended squad should not show stale numbers.
- UI: the squad strip above MESH exists now (brief, open waits, elastic limit); add member state pills, running
  cost, `[PIN SQUAD]` and `[END SQUAD]`. The passive "owes an answer" badge exists (item 3).
- Build the per-model rollup query here once; item 6's `Store.report` models table is the starting point.

## 5. A/B squad compare — M, after feature 2

"Is one Opus architect with two Sonnet implementers better than three Sonnets on this task?" Worth building
only with these limits:

- One brief and one base commit, N squad configurations, each in its own worktrees (now keyed by squad, so
  arms cannot collide).
- **A check command is mandatory** (for example `python -m unittest`), run in each arm's worktree at the end.
  Without an objective check this only measures which squad is cheaper.
- Result table: pass or fail, cost, wall minutes, tokens, messages, diff size, straight from feature 2's
  review; worktime and overlap from `/api/worktime`.
- One run per arm is noise. Offer "repeat k times" and do not print anything that looks like significance.
  Show a cost estimate before launch, since this multiplies spend.
- Needs named, saved squad rosters to compare; today the roster lives only in the launch form.
- Storage: `experiments(id, brief, base_sha, check_cmd)` and `squads.experiment_id`.

## 8. Keyboard and ARIA parity; inline model-edit race — S

Reported by the reviewer and not independently verified: `j`/`k` only move in FLEET; the tab list has no
arrow-key roving focus; `#help` is not a dialog; segmented controls lack initial `aria-pressed` (the feed filters
now set it). Committing an inline model or effort edit disables the input, which blurs it and lets the 2-second
`loadFleet` poll redraw the table mid-save; hold an explicit editing flag instead of testing
`document.activeElement`.

## 10. Engram for Almanac — M to L

Decision recorded: native Engram (`D:\$Coding Projects\gskills-claude\engram`) was **not** installed in this repo.
Its SessionStart hook is harmless per agent, but its CLAUDE.md import tells every agent to write `.claude/memory`,
which contradicts the no-edit roles, gives a shared checkout several writers of one journal, and splits memory
across worktrees; Gemini agents get nothing; the headless namer in `.almanac/namer-cwd` may pick up the hook
(unverified). The Almanac-scoped replacement (notes as objects with one writer, trust states, freshness on read,
a capped kickoff brief, markdown export) is drafted in [docs/objects-and-knowledge.md](docs/objects-and-knowledge.md),
stages 3 and 4.

## 11. Native Claude and Gemini features where they fit — M

Native subagents for file-level fan-out inside one implementer (their sidechain transcripts are ingested, but they
are not rows in FLEET or the heatmap); Workflows for fixed pipelines (review, then verify); roles exported as native
agent types; Stop / SessionStart hooks reporting state to the hub instead of transcript tailing. Each needs a note
on what visibility Almanac loses.

## 12. Integrating worktree branches — M, part of item 2

Today the integrator applies `git diff <snapshot>..HEAD` by hand (`git apply`, falling back to `--reject` and hand
resolution; `--3way` refuses while the main checkout has unstaged work). Item 2's review should own this: a diff
stat per worktree, an apply or merge button, and never deleting a worktree with unapplied work.

## 13. Context policy per role — M

Evidence: on 2026-11-10 impl-claude's first slice re-read about 140k tokens per step and its last 40 minutes about
650k; a fresh impl-gallery did a whole slice in 4M, and a fresh reviewer found five bugs two long-context agents had
missed. On 2026-11-11 the architect reused builders across slices again; tokens re-read per step, first 20 steps
against last 20: impl-claude 88k to 662k (128.7M read, 12 slices in one session), arch-claude 131k to 583k,
impl-plan 97k to 420k, impl-backend 92k to 397k, against engram-fable, one job then ended, 157k to 172k.

What the 2026-11-11 build unblocked: the handoff is a task ref and its log (item 9); "between tasks" is a task
reaching `accepted`; a cheap fresh agent is an elastic spawn with the role's allowed models and efforts; typing
into a console at the right moment is what `recover.py` already does. Pieces:

- context size per agent in FLEET and on the PLAN owner chip, with a threshold (`sessions.context` is stored);
- a per-role `context_policy`: `fresh-per-task` | `compact-at-N` (at a task boundary only) | `none`;
- a lead action that respawns an owner on the same task with its log, which is also the recovery step beyond
  `continue` for a stalled agent.

Check first: whether a typed `/compact` works through Almanac's console send path, and what a Gemini agent has
instead. The next squad's brief is `docs/next-squad-brief.md`.

## 14. Findings ledger: is review worth its tokens — M

Transcripts are kept (`.almanac/archive/projects`, `.almanac/gemini/projects`) and every mesh message is journaled,
so reviews can be read back. But a finding is prose in a message: no id, severity, verdict or link to its fix, and
a review done under a non-reviewer role is not marked as one.

Design: findings are a kind of work object (`F-13`, parent a task) with verdicts; see
[docs/objects-and-knowledge.md](docs/objects-and-knowledge.md), stage 2.

First data point, this build's one review (impl-telemetry, Opus 5.5, read-only): 19 findings, 16 fixed with tests,
3 documented and not fixed, 0 rejected. One finding (N1) was a regression introduced by fixing an earlier one. The
session cost about 76k output tokens and 15 minutes of worked time, part of which was its own slice. Old reviews
can be indexed after the fact with a model pass over the saved transcripts, less reliably.

## Left open

- `tests/test_chats.py` ArchiveTests failed once in 20 full runs (`archive.sync` reported 0 replaced, 1
  expected). Probably a transient Windows file lock on `.test-tmp`; not investigated.
- `uds.set_status` swallows `OSError`, so on Windows a registration rewrite that collides with a reader leaves
  a stale model or status for a Gemini agent.
- A `.env` already staged in the index still goes into a worktree snapshot commit; the exclusion applies to
  untracked files.
- Agent tokens stop accidents, not an adversary: an agent runs as the same user and can read `<state>/token`,
  and the hub's named pipe accepts any `from-name`. `mcp_mesh._resolve_auth` still falls back to that token file
  when `ALMANAC_TOKEN` is unset, so an agent that clears its environment acts as the operator (and could pass the
  spawn limit that way).
- An agent can still direct-message any live session by name. Left alone on purpose: naming a peer is
  deliberate, and Claude Code's own `SendMessage` allows it regardless.
- The operator can post a `#channel` across squads when no squad is selected. Left alone on purpose:
  subscription is explicit. `#squad` is the exception: it means `@squad` (every member, subscribed or not), so
  with no squad selected it needs exactly one live squad, like `@squad`.
- Worktime: time a tool sits on a permission prompt counts as work (the transcript cannot tell it from a slow
  tool), and a Gemini agent that waits through `call_mcp_tool(ToolName="mesh_wait")` is not seen as waiting.
- Never seen in a browser: the roster `✉N` badge, a real browser Notification, the elastic strip editor, the
  heatmap, the topology and the swimlane. Chrome automation refused the throwaway tabs; the data behind each is
  tested.
- An agent's MCP server keeps the tool list it started with: agents alive across a restart lack the new tools
  (`needs`, `mesh_spawn`) until respawned. The CLI fallback runs the code on disk and works.
- `mesh_restart` refuses while any headless Claude agent is live: its stdin is a pipe Almanac holds
  (`spawner.py`, `service.headless_procs`), so it would end with Almanac. Console Claude agents and Gemini hosts
  have their own consoles and survive. There is no override yet; one would need a headless agent whose stdin is not
  Almanac's (a named pipe it reopens, or the hub's uds pipe alone).
- A restart relies on the launcher (`run.ps1` / `run.cmd` / `run.sh`, exit code 75, `ALMANAC_LAUNCHER=1`).
  A terminal that runs Almanac inside a kill-on-close job object would kill the agents only when that job closes;
  the launcher's loop keeps it open across a restart, but closing the terminal ends them as before.
- A dispatch whose lead fails to start removes the squad but not a worktree already made for that lead.
- A backlog task is a row in this machine's database; another machine does not see the backlog. Only a brief written
  into the repo (such as `docs/next-squad-brief.md`) travels by git.
- Not seen working against the real thing: auto-continue has never met a really stalled agent (only an injected
  transcript record through the real recover code); `mesh_restart` has never restarted the live hub (only stub
  interpreters and the preflight); a Gemini error class is never retried, for lack of a single saved agy error
  sample (`recover.TRANSIENT`); whether agy's totals ever reset is unknown, and a drop is taken as a reset.
- Seen in a browser only on the rig door (`door_rig.py`, a fake process table, no live agents), 2026-11-11: FLEET
  and MESH stall pills with their `since` tooltips from the real recover pass, the header alert and the INBOX
  escalation thread, PLAN with a real task tree, markdown in the contact stream, and the ROLES form saving
  `may_restart`, channels and folders (and showing the refusal for a missing one). Not seen: GALLERY tiles, the
  CHATS markdown pane, PLAN's by-owner toggle and task pane, a restart from the page. After a save the role form
  keeps the text as typed rather than the normalized channels.
- A server restarted mid-edit runs a mix of old API and new static files (the topology now says so instead of
  failing). Consider an API version stamp the page checks once.
- Review in this squad: the Gemini reviewer could not start at launch (no runner), so every slice was reviewed by
  the architect alone. A Claude review (`impl-telemetry`, read-only, after its own slice) then ran on the finished
  tree; its findings are fixed and covered by tests (`tests/test_review_fixes.py`, `test_fix8.py`, `test_fix9.py`).
  A Gemini review (rev-carbon, Gemini 4 Carbon, static only) ran on the final tree: nine groups of findings, mostly
  in the dashboard; fixed in FIX-10P / FIX-10U.
- A direct message with no squad id closes matching flags in any squad (rev-carbon finding 5.2). Accepted for now.
- Gemini agents cannot run arbitrary shell commands on this account: `--mode auto` is refused and the default mode
  denies every shell command, so a role's `allowed_commands` (exact command lines, agy allow rules) is the only
  way; `--dangerously-skip-permissions` (role permission mode `bypass`) is the other route.
- Headless `agy` denies `read_file` outside the workspace; a Gemini role's `add_dirs` passes `--add-dir` per
  folder, which makes each part of the agent's workspace (edits included, as its mode allows), not read-only.
  `read_file(<target>)` allow rules through `--settings` (beside the command rules) were not tried.
- Gemini shell calls are recorded without their command: ingest reads `input.command` (store.py:614), but Gemini's
  `run_command` carries `CommandLine` (fleet.py:266), so `family`, `head` and the adoption scores are empty for
  them. Found by reading (rev-gemini), not reproduced on a transcript; not fixed.
- An agent token can read any session's context stream (`/api/mesh/context?peer=<any session id>` falls back to
  any stored transcript, the operator's own chats included) and any squad's messages, the operator's inbox among
  them. Same advisory model as the tokens; restricting agents to their own squad's peers is the fix if this ever
  needs to hold.
- With `spawn_role` `*` an agent may add a role that can page the operator or add members itself (an architect).
  It stays inside the one limit, and the UI says so on the `any role` option.
- An npm-installed Claude exposes only a `.cmd` shim; Almanac refuses it, because cmd.exe re-parses a batch file's
  command line and the charter and an agent-written task travel as arguments. Passing the charter and the kickoff
  by file would lift that.
- A turn stretches to any later main-thread record with no gap cap, so a transcript with no prompt records
  (imported or headless runs) can show hours of "work" across a silence. A cap needs care not to cut a genuinely
  long tool call.
