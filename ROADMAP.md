# Almanac roadmap

Feature candidates from the alpha-squad review of 2026-11-10 (architect Claude, implementer Claude, reviewer
Gemini), ranked. Each one names the code it would build on, so it can be picked up cold. The bugs that review
found are fixed in the same change that added this file; what was left open is at the end.

Effort: S = an afternoon, M = a few days, L = a week or more.

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
    (`calls.seconds`), messages sent and received (`messages.squad_id`), first and last activity;
  - **edits and writes per agent**: `calls.tool` in `Edit`, `Write`, `NotebookEdit` and Gemini's
    `replace_file_content` / `write_to_file` / `multi_replace_file_content`, **plus shell calls that edit
    files**. The tool count alone undercounts: in the review squad both Claude agents also patched files
    through `python` scripts in Bash. `classify.family` already buckets shell commands, so count
    `calls.family = 'edit'`; check it catches heredoc patch scripts and `sed -i`.
  - flag a role doing another role's job: any edit by an agent whose role brief says it does not edit
    (architect, reviewer). In the review squad this would have read architect 16 / implementer 20 / reviewer 0
    edit-tool calls, which is the misallocation the operator had to spot by watching.
  - `git diff --stat` of each worktree against its snapshot commit. Run it on demand or at end, never on the
    2-second poll.
- Trigger an ingest when the review is opened; `Service.agents_live` already shortens the loop to 30 s while
  agents run, but a just-ended squad should not show stale numbers.
- UI: a squad strip above MESH with the brief (today it disappears after launch; `/api/mesh` already returns
  it), member state pills, running cost, `[PIN SQUAD]`, `[END SQUAD]`, and a passive badge on any member that
  is idle with an unanswered message. That badge replaces a background stall watchdog, which was proposed and
  set aside as false-positive prone; revisit only if the badge is not enough.
- Build the per-model rollup query here once; feature 6 is a second view of it.

## 3. Operator channel and message flags — M

Three operator requests that are one feature.

**A human-facing view apart from agent chatter.** `Service.mesh_send` already journals messages addressed to
`operator` (transport `hub`), but nothing surfaces them and the agent charter never mentions the address.

- An OPERATOR INBOX: messages to or from `operator`, separate from the agent-to-agent bus, with an unread
  count in the header.
- The MESH context view has `all | dialog | contacts | tools` kind filters; FLEET's session feed and GALLERY
  tiles do not. Give them the same filter so a human can read prompts and replies without tool noise.

**Which agents may address the operator.** Add `roles.may_page_operator`. Enforce it in `POST /api/mesh/send`
(the agent token now identifies the sender; its role comes from `spawned_agents`), and mention
`to="operator"` in the charter only for roles that have it. This is advisory against a determined agent: the
hub's named pipe does not check senders, and the master token is readable on disk.

**Flags for messages that need an answer.** Two levels, on any message, to a peer or to the operator:

- `reply`: needs an answer, not urgent.
- `blocking`: the sender, or the whole flow, cannot continue until it is answered.

Sketch: a `needs` column on `messages` (`null | reply | blocking`) and a `needs` argument on `mesh_send`.
Put a visible marker at the top of the delivered body, since Claude Code renders the
`<cross-session-message>` wrapper and extra attributes may not survive. A flagged message is open until its
recipient sends something back to its sender; add an explicit `reply_to` id only if that rule proves too
loose. In the UI a `blocking` message to the operator raises a header alert (and a browser notification) and
the squad strip reads "blocked on operator for 4m"; an open `reply` shows a quiet badge. Whether a role may
send `blocking` to the operator follows `may_page_operator`.

## 4. Mesh topology and work swimlane — M

The visual graph of who is talking to whom and who is doing what. Build it as a deterministic SVG inside MESH,
not a physics graph: squads cap at 20 agents, and a fixed layout by role (architect, implementers, reviewers)
shows handoffs that a bouncing layout hides. No dependencies; `createElementNS` and `textContent` only, like
the rest of `app.js`.

- Nodes: `squads[].agents` joined with `/api/mesh` `peers[]`, so an agent that exited stays as a dashed node.
  Show provider, worktree mark, busy state and the one-line `last_event`.
- Edges: `messages[]` from `/api/mesh`, width by count, highlighted when recent. Click a node to select the
  peer; click an edge to filter the contact stream.
- Swimlane: per-agent event ticks from `/api/sessions?pids=...`, coloured by the existing feed classes. That
  route only answers for live pids; fall back to `Service.mesh_context` for ended agents.
- Default it to the selected squad. Unscoped `@all` traffic on a busy machine is unreadable.

## 5. A/B squad compare — M, after feature 2

"Is one Opus architect with two Sonnet implementers better than three Sonnets on this task?" Worth building
only with these limits:

- One brief and one base commit, N squad configurations, each in its own worktrees (now keyed by squad, so
  arms cannot collide).
- **A check command is mandatory** (for example `python -m unittest`), run in each arm's worktree at the end.
  Without an objective check this only measures which squad is cheaper.
- Result table: pass or fail, cost, wall minutes, tokens, messages, diff size, straight from feature 2's
  review.
- One run per arm is noise. Offer "repeat k times" and do not print anything that looks like significance.
  Show a cost estimate before launch, since this multiplies spend.
- Needs named, saved squad rosters to compare; today the roster lives only in the launch form.
- Storage: `experiments(id, brief, base_sha, check_cmd)` and `squads.experiment_id`.

## 6. Telemetry by provider and model, with tool error rate — M

TELEMETRY blends every session together. `sessions.provider`, `usage.model` and `calls.is_error` are already
stored and `Store.report` exposes none of them. Add a provider filter and a per-model table (context per
output token, kB read back per call, output tokens, error rate). Legacy seeded rows have no session to join;
bucket them apart.

## 7. Gallery: keep ended tiles, worktree badge, squad filter — S

`loadGallery` removes a tile the moment its process leaves the fleet, so a crashed agent's last output is
gone before it can be read; `.gallery .box.gone` in `app.css` is styled and never used. Keep ended tiles for
pinned and squad sessions (cap them), show the last two path segments plus a worktree mark instead of a
right-truncated path, and add `busy` and `squad` to the show filter.

## 8. Keyboard and ARIA parity; inline model-edit race — S

Reported by the reviewer and not independently verified: `j`/`k` only move in FLEET; the tab list has no
arrow-key roving focus; `#help` is not a dialog; segmented controls lack initial `aria-pressed`. Committing an
inline model or effort edit disables the input, which blurs it and lets the 2-second `loadFleet` poll redraw
the table mid-save; hold an explicit editing flag instead of testing `document.activeElement`.

## Left open by the bug-fix pass

- `tests/test_chats.py` ArchiveTests failed once in 20 full runs (`archive.sync` reported 0 replaced, 1
  expected). Probably a transient Windows file lock on `.test-tmp`; not investigated.
- `uds.set_status` swallows `OSError`, so on Windows a registration rewrite that collides with a reader leaves
  a stale model or status for a Gemini agent.
- A `.env` already staged in the index still goes into a worktree snapshot commit; the exclusion applies to
  untracked files.
- Agent tokens stop accidents, not an adversary: an agent runs as the same user and can read `<state>/token`,
  and the hub's named pipe accepts any `from-name`.
- An agent can still direct-message any live session by name. Left alone on purpose: naming a peer is
  deliberate, and Claude Code's own `SendMessage` allows it regardless.
- The operator can post a `#channel` across squads when no squad is selected. Left alone on purpose:
  subscription is explicit.
