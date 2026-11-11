# Almanac roadmap

Feature candidates from the alpha-squad review of 2026-11-10 (architect Claude, implementer Claude, reviewer
Gemini), ranked, and what the alpha-squad build of the same evening shipped. Each open item names the code it would
build on, so it can be picked up cold.

Effort: S = an afternoon, M = a few days, L = a week or more.

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
`POST /api/mesh/spawn`. Model, effort, workspace and permission mode always come from the role's definition, never
from the agent. The limit check and the spawn hold one lock. The operator sets both at launch or in the squad strip
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

## 9. Task ledger and board — M

Which agent is on which task. A small table (squad, title, owner, files, status: queued / in progress / in review /
accepted / blocked) written by the architect through a mesh tool. The task passed to `mesh_spawn` becomes the new
member's first entry; MESH gets a board by owner; heatmap stretches are labelled with their task. These should be
the same records as item 10's tasks.

## 10. Engram for Almanac — M to L

Decision recorded: native Engram (`D:\$Coding Projects\gskills-claude\engram`) was **not** installed in this repo.
Its SessionStart hook is harmless per agent, but its CLAUDE.md import tells every agent to write `.claude/memory`,
which contradicts the no-edit roles, gives a shared checkout several writers of one journal, and splits memory
across worktrees; Gemini agents get nothing; the headless namer in `.almanac/namer-cwd` may pick up the hook
(unverified). Design sketch: one writer (agents post journal entries through a mesh tool and Almanac writes them
with agent and squad attribution); a role-aware brief put into the kickoff by the spawner instead of a hook;
SHA-stamped atlas freshness and first-class dead ends kept from Engram; squad end (item 2) feeds the journal.

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

Evidence from this build: impl-claude's first slice ran 37 messages on 5.1M cache-read tokens (about 140k re-read
per step); its last 40 minutes ran 83 messages on 54M (about 650k per step). impl-gallery did a whole slice from a
fresh start in 38 messages and 4M. A long-lived worker pays for its history on every tool call, and the fresh
reviewer found five bugs that two long-context agents had missed.

Policy to build: implementers and reviewers are one task, one session (a fresh spawn with a self-contained brief;
elastic squads already works this way). The integrator and the architect keep continuity and compact at a slice
boundary once context passes a threshold (never mid-slice, never after every small fix); their plan and queue live
outside the context, so a clear is safe. Pieces:

- context size per agent in FLEET and on the heatmap (`sessions.context` is stored), with a warning threshold;
- a per-role `context_policy` (`fresh-per-task` | `compact-at-N%` | `none`);
- an architect action "retire and respawn with handoff";
- `/compact` typed at a slice boundary (Almanac already types `/model` into a console; untested).

Depends on 9 and 10 for the handoff. Item 10 (Engram for Almanac) should be designed around this.

## 14. Findings ledger: is review worth its tokens — M

Transcripts are kept (`.almanac/archive/projects`, `.almanac/gemini/projects`) and every mesh message is journaled,
so reviews can be read back. But a finding is prose in a message: no id, severity, verdict or link to its fix, and
a review done under a non-reviewer role is not marked as one.

Build:
- reviewers file each finding as a record through a mesh tool (file:line, `bug` | `risk` | `nit`, confidence, has
  a repro);
- the architect records a verdict (`fix now` | `defer` | `reject`) with a reason;
- the fix links back when it lands, and a re-run marks it verified;
- joined with usage and worktime: tokens and minutes per accepted finding, acceptance rate by reviewer model, share
  of nits, and how often a fix causes a regression.

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
  subscription is explicit.
- Worktime: time a tool sits on a permission prompt counts as work (the transcript cannot tell it from a slow
  tool), and a Gemini agent that waits through `call_mcp_tool(ToolName="mesh_wait")` is not seen as waiting.
- Never seen in a browser: the roster `✉N` badge, a real browser Notification, the elastic strip editor, the
  heatmap, the topology and the swimlane. Chrome automation refused the throwaway tabs; the data behind each is
  tested.
- An agent's MCP server keeps the tool list it started with: agents alive across a restart lack the new tools
  (`needs`, `mesh_spawn`) until respawned. The CLI fallback runs the code on disk and works.
- A server restarted mid-edit runs a mix of old API and new static files (the topology now says so instead of
  failing). Consider an API version stamp the page checks once.
- Review in this squad: the Gemini reviewer could not start at launch (no runner), so every slice was reviewed by
  the architect alone. A Claude review (`impl-telemetry`, read-only, after its own slice) then ran on the finished
  tree; its findings are fixed and covered by tests (`tests/test_review_fixes.py`, `test_fix8.py`, `test_fix9.py`).
  A Gemini review (rev-carbon, Gemini 4 Carbon, static only) ran on the final tree: nine groups of findings, mostly
  in the dashboard; fixed in FIX-10P / FIX-10U.
- An agent's identity at the door is its handle, not its session: with two live squads that reuse a handle (the
  default roster does), the sender's session and squad can bind to the other squad's agent (rev-carbon finding
  6.2). Tokens should carry the session id.
- A direct message with no squad id closes matching flags in any squad (rev-carbon finding 5.2). Accepted for now.
- Gemini agents cannot run arbitrary shell commands on this account: `--mode auto` is refused and the default mode
  denies every shell command, so a role's `allowed_commands` (exact command lines, agy allow rules) is the only
  way; `--dangerously-skip-permissions` (role permission mode `bypass`) is the other route.
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
