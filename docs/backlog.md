# Backlog

Exported 2026-11-11 from Almanac's database on one machine: the 21 tasks queued in this project's backlog, with what their logs hold beyond filing and moves. The database (`.almanac/`, gitignored) is still where Almanac reads the backlog; this file is a copy so the list travels with the repo until T-43 gives it a home there. `T-n` refs are that database's numbers.

| Task | Title | Slices |
|---|---|---|
| T-12 | Notes v3: seed knowledge files and the read path |  |
| T-13 | Window prose: hide by default, measure, then one charter line |  |
| T-14 | Small leftovers from the 2026-11-11 build |  |
| T-33 | What a task costs | T-11, T-25, T-26, T-27, T-28, T-36 |
| T-34 | A shorter review loop | T-29, T-30, T-31, T-37, T-38, T-39 |
| T-35 | Squad lifecycle | T-17, T-18 |
| T-43 | The backlog belongs to the repo: queued backlog tasks live as GitHub Issues, not only in one machine's database |  |

## T-12: Notes v3: seed knowledge files and the read path

- filed 2026-11-11 by `arch-claude`; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 5 of role `*`

### Goal
Knowledge that survives a squad and a machine: published notes are markdown files in the repo, written only by Almanac; the database indexes them.

### Read first
`docs/engram-debate-2026-11-11.md` (the agreed items and the disputed table) and `docs/objects-and-knowledge.md` (notes design v3).

### Build, in this order
1. Seed about ten code-anchored notes under `docs/knowledge/` from ROADMAP "Left open" and recorded decisions, as one diff the operator reads and commits.
2. A loader that indexes those files; freshness by the symbol's body hash, checked on disk in the main checkout (Python by `ast`, other files whole-file).
3. The brief: directives plus at most five fresh notes at kickoff and at each assignment; log which notes each agent was briefed on.
4. Next commit: promotion by a reviewer or the operator, never the author, checked by the door.

### Done when
A freshly spawned builder's kickoff carries the notes that match its task, and editing the anchored function drops the note from the next brief.

### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

## T-13: Window prose: hide by default, measure, then one charter line

- filed 2026-11-11 by `arch-claude`; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 5 of role `*`

### Goal
Agents write prose into windows nobody reads. Measured on 2026-11-11: 9% of visible output (5% end-of-turn text, 4% narration between tool calls), about 4% of output tokens.

### Read first
`docs/next-squad-brief.md` (the window-prose item).

### Build, in this order
1. Squad agents' feeds in FLEET and GALLERY default to messages and tools, prose one click away. No behaviour change.
2. Prose share per agent, role and model (end-of-turn against narration) in the squad review.
3. One charter line: "Nobody reads your window. End a turn with one line; the hand-in on the task log is the report." Keep it only if the share drops and the reviewer's findings do not get worse. Leave narration alone.

### Done when
The share is a number on the page for two squads, one with the charter line and one without.

### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

## T-14: Small leftovers from the 2026-11-11 build

- filed 2026-11-11 by `arch-claude`; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 5 of role `*`

### Goal
Close the small things the last squad recorded and did not fix.

### List
1. RESOLVE in INBOX closes a flag and tells the asker nothing; send the asker a notice ("resolved by the operator, no reply") so an agent waiting on a blocking question stops waiting.
2. When a task is reassigned, the previous owner is not told.
3. The ROLES form keeps the typed channel text after a save instead of the normalized list.
4. A dispatch whose lead fails removes the squad but not a worktree already made for that lead.
5. `normalize_model` keeps the case of a Gemini id that is not an alias.
6. Never seen in a browser on the merged build: GALLERY tiles, markdown in the CHATS pane, the elastic inputs' badge tooltip.

See ROADMAP "Left open" for the rest; do not take on anything not listed here without asking the operator.

### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

**Log** (2)

- *note*, `arch-claude`, 2026-11-11: Add to this list, from rev-gemini's review of the ROLES multi-pick fields (f08b147): (7) Escape with the models or efforts dropdown open closes the whole roles drawer, or leaves the dropdown open; it should close the dropdown. (8) Switching a role's provider from Claude to Gemini keeps xhigh / max ticked in efforts. (9) A custom model id typed in the dropdown's add box but not confirmed with Enter is ignored on save, and the box is not cleared when another role is loaded.
- *next*, `impl-plan`, 2026-11-11: impl-plan: a fix for the three deferred ROLES-dropdown nits (rev-gemini on f08b147) was written but not committed, because arch-claude deferred them. It applies to app.js at f08b147 with git apply. Status: node --check OK; not run in the suite or a browser. (1) Escape closes an open details.multi first (and the drawer Escape now resets toggle-roles aria-expanded); (2) a provider switch drops effort ticks the new provider does not offer (refillRoleEfforts(undefined, true)); (3) populateRoleForm clears #role-models-add, and a save first takes a typed but un-Entered model id.
  
  ```diff
  diff --git a/almanac/static/app.js b/almanac/static/app.js
  index 3e43c54..1c557ae 100644
  --- a/almanac/static/app.js
  +++ b/almanac/static/app.js
  @@ -799,10 +799,11 @@ function fillMulti(id, options, ticked, fmt) {           // a ticked value outsi
   }
   const roleProv = () => $("role-provider").value || "claude";
   const refillRoleModels = (ticked) => fillMulti("role-models", modelsForProvider(roleProv()), ticked, String);
  -function refillRoleEfforts(ticked = multiTicked("role-efforts")) {      // what the ticked models allow, highest first
  +// what the ticked models allow, highest first; `onlyOffered` drops ticks the options no longer hold (a provider change)
  +function refillRoleEfforts(ticked = multiTicked("role-efforts"), onlyOffered = false) {
     const models = multiTicked("role-models");
     const opts = [...new Set((models.length ? models : [""]).flatMap((m) => effortsForModel(roleProv(), m)))].sort(byEffort);
  -  fillMulti("role-efforts", opts, ticked, fmtEffortLabel);
  +  fillMulti("role-efforts", opts, onlyOffered ? ticked.filter((t) => opts.includes(t)) : ticked, fmtEffortLabel);
   }
   $("role-models-list").addEventListener("change", () => { paintMulti("role-models", String); refillRoleEfforts(); });
   $("role-efforts-list").addEventListener("change", () => paintMulti("role-efforts", fmtEffortLabel));
  @@ -818,7 +819,7 @@ $("role-models-add").addEventListener("keydown", (e) => {
   $("role-provider").addEventListener("change", () => {
     const opts = modelsForProvider(roleProv());
     refillRoleModels(multiTicked("role-models").filter((m) => opts.includes(m)));
  -  refillRoleEfforts();
  +  refillRoleEfforts(undefined, true);
   });
   document.addEventListener("click", (e) => {             // a multi-pick closes on a click outside it
     for (const d of document.querySelectorAll("details.multi[open]")) if (!d.contains(e.target)) d.open = false;
  @@ -1724,6 +1725,7 @@ function populateRoleForm(r) {
     $("role-id").value = r ? r.id : "";
     $("role-title").value = r ? r.title : "";
     $("role-provider").value = prov;
  +  $("role-models-add").value = "";
     refillRoleModels([...new Set([mod, ...(r?.models || [])])].filter(Boolean));
     refillRoleEfforts([...new Set([eff, ...(r?.efforts || [])])].filter(Boolean));
     $("role-workspace").value = r ? (r.workspace_mode || "shared") : "shared";
  @@ -1748,6 +1750,8 @@ $("role-new").addEventListener("click", () => populateRoleForm(null));
   $("role-save").closest("form").addEventListener("submit", async (e) => {
     e.preventDefault();
     const btn = $("role-save"), msg = $("role-msg");
  +  const typed = $("role-models-add").value.trim();     // a model id typed but not yet added with Enter is kept
  +  if (typed) { refillRoleModels([...multiTicked("role-models"), typed]); refillRoleEfforts(); $("role-models-add").value = ""; }
     const models = multiTicked("role-models").sort(byCapability), efforts = multiTicked("role-efforts").sort(byEffort);
     btn.disabled = true;
     try {
  @@ -3416,6 +3420,10 @@ $("help-btn").addEventListener("click", () => { $("help").hidden = !$("help").hi
   $("help").addEventListener("click", () => { $("help").hidden = true; });
   
   addEventListener("keydown", (e) => {
  +  if (e.key === "Escape") {                    // an open multi-pick closes first; the drawer it sits in stays open
  +    const d = document.querySelector("details.multi[open]");
  +    if (d) { e.preventDefault(); d.open = false; d.querySelector("summary").focus(); return; }
  +  }
     if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) { if (e.key === "Escape") e.target.blur(); return; }
     if (e.ctrlKey || e.metaKey || e.altKey) return;
     const rows = [...document.querySelectorAll("#fleet-table tbody tr")];
  @@ -3439,6 +3447,7 @@ addEventListener("keydown", (e) => {
         setDrawer("spawn-drawer", "toggle-spawn", false);
         setDrawer("squad-drawer", "toggle-squad", false);
         $("roles-drawer").hidden = true;
  +      $("toggle-roles").setAttribute("aria-expanded", "false");
         closeAlertBox();
         break;
       case "r":
  ```

## T-33: What a task costs

- filed 2026-11-11 by `orchestrator-claude`

A feature, not a slice: its slices are filed under it. Goal: for every accepted task, Almanac can say what it cost (time, tokens, context, review rounds, lead and reviewer overhead), what each context policy event bought, and whether the review caught what mattered. Dispatch a slice, or this feature with its queued slices. Evidence so far: the T-10 run of 2026-11-11 (its log has the per-task timing and the wall-clock split).

### T-11: Findings as records: is a reviewer worth its tokens

- filed 2026-11-11 by `arch-claude`; part of T-33; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 5 of role `*`

#### Goal
The operator's test case for a data-driven Almanac: query every reviewer's findings and weigh them against what the reviewer cost.

#### Read first
`docs/objects-and-knowledge.md` (stage 2) and `docs/next-squad-brief.md` (Build B).

#### Build
1. A reviewer files each finding as a record under the task it reviewed: file and line, severity (`bug` | `risk` | `nit`), confidence, whether it has a repro.
2. The lead records a verdict (`fix now` | `defer` | `reject`) with a reason; the fix links back with its commit; a re-check marks it verified.
3. A report per reviewer session and per model: findings by severity and verdict, tokens, worked minutes, tokens per accepted finding, acceptance rate, share of nits, fixes that caused a regression.

#### First dataset
File the 2026-11-11 squad's findings by its Gemini reviewer from the journal (about 65 across 14 reviews; re-derive the exact tally, do not trust this number).

#### Done when
One query returns that squad's tally by severity and verdict next to the reviewer's tokens and minutes.

#### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

**Log** (1)

- *note*, `orchestrator-claude`, 2026-11-11: Related, filed 2026-11-11 by the context-policy squad (T-10) at the operator's request: T-25 per-task ledger (its `review rounds` is the bridge to findings), T-27 lead and reviewer overhead per task, T-28 escaped defects (needs this task and T-25 first). A dataset from T-10's squad for this task: reviewer-gemini raised 11 findings on T-20, T-21, T-22 and one lead commit, 10 accepted and 1 rejected, each with a verdict line in those tasks' logs; the lead raised 4 more.

### T-25: Per-task ledger: time, tokens, context and review rounds recorded on a task when it is accepted

- filed 2026-11-11 by `orchestrator-claude`; part of T-33; may add up to 3 of role `*`
- files: `almanac/store.py`, `almanac/door.py`, `almanac/static/app.js`, `almanac/static/index.html`, `tests/test_task_ledger.py`

#### Goal
Know what each task cost, so the choices made per task (model, effort, context policy) can be judged against their outcome. Asked for by the operator on 2026-11-11, at the end of the context-policy squad (T-10).

#### Why now
That squad ran one task per fresh agent, so a session's numbers are a task's numbers. Today the only per-task figures are wall-clock gaps read out of the task log by hand: T-20 (Sonnet, medium) 25 min and 3 review rounds, T-21 (Sonnet, high) 17 min and 1 round, T-22 (Opus, high) 39 min and 2 rounds. Worked minutes exist per agent (`/api/worktime`), tokens per session (`usage`), neither per task.

#### Build
1. When a task becomes `accepted` (the same event the context policy acts on, door.py `log_to_object`), store one ledger record on the task (its `data`, or a table keyed by task id), covering every session that owned it (a respawned owner has several):
   - owner handle, role, provider, model, effort, and the spawn's `why`;
   - wall clock: filed, first hand-in (`in review`), accepted;
   - worked minutes (what `/api/worktime` computes, cut to the sessions and the task's span);
   - tokens: input, cache read, cache write, output, and the estimated cost where a price is configured (`resume.Prices`);
   - steps (assistant records), and context per step: first, last, peak;
   - review rounds: how many times it went back from `in review` before `accepted`;
   - policy events on it: respawns, retire, compact, each with the context before.
2. A task that is never accepted gets no record; do not estimate.
3. PLAN: the task detail shows the record; a squad summary row gives totals per role and per model (tasks, minutes, tokens, cost, rounds).
4. One query the review can use: per squad, per role and model: tasks accepted, median wall clock, worked minutes, tokens and cost per task, rounds per task.

#### Check before building
- Whether a task's owner sessions can be recovered after the fact from the task log (`assign` and `respawn` entries carry `owner_to`), so the first dataset can be backfilled for T-20 to T-24.
- A Gemini session's usage is running totals per turn (store.py `_gemini_turn`); say which Gemini figures are exact.

#### Not in this task
Findings as records (ROADMAP item 14, Build B in docs/next-squad-brief.md): severity, verdict and fix commit per finding. The ledger's `review rounds` is the bridge to it.

#### Done when
For a squad run after this lands, each accepted task shows its record in PLAN, and the per-role, per-model summary can be read without opening a transcript.

### T-26: Policy events record context before and after: what a compact, a respawn and a retire actually bought

- filed 2026-11-11 by `orchestrator-claude`; part of T-33; may add up to 2 of role `*`
- files: `almanac/door.py`, `almanac/store.py`, `almanac/static/app.js`, `tests/test_policy_events.py`

#### Goal
Show what each context policy event bought, so `compact-at-N`, `fresh-per-task` and a lead's respawn can be compared on numbers. Asked for by the operator on 2026-11-11 (context-policy squad, T-10).

#### Today
`door._compact`, `door._retire` and `door.respawn_task` log the agent's context before the event on the task. Nothing records what came after, so it is unknown how much `/compact` frees (Claude or agy), or what a fresh session's first steps cost against carrying on. T-10's live probes only showed that `/compact` runs.

#### Build
1. For a compact and a respawn, record the agent's context at its first main-thread step after the event, beside the "before" already logged: on the same task log entry or a follow-up entry by the hub. Take it where the reading already arrives (fleet's tail read, or ingest), not from a new timer.
2. For a compact, also record whether it ran at all: a Claude compaction leaves a record in the transcript (find its shape; T-10's probe could not, its session saved no transcript), agy answers with a `checkpoint` step. A `/compact` that was typed and never ran should be visible.
3. For a respawn and for a fresh member put on an existing task, record the kickoff's size in characters and the first step's context: the cost of starting from the log.
4. PLAN's task detail shows these with the event; the per-task ledger (T-25) includes them.

#### Check before building
- What a Claude compaction writes to the transcript, and whether `fleet.read_tail` already passes it.
- Whether agy's checkpoint reduces the next turn's `input_tokens + cache_read_tokens` on a long conversation.

#### Done when
After a squad run, each compact and respawn on a task shows before and after, and a compaction that did not happen is distinguishable from one that did.

### T-27: Lead and reviewer overhead per task: their tokens and wakes attributed to the tasks they served

- filed 2026-11-11 by `orchestrator-claude`; part of T-33; may add up to 2 of role `*`
- files: `almanac/store.py`, `almanac/door.py`, `almanac/static/app.js`, `tests/test_overhead.py`

#### Goal
A task's cost includes the lead and the reviewer, not only its builder. Asked for by the operator on 2026-11-11 (context-policy squad, T-10).

#### Evidence
In that squad the builders were fresh per task and ended a slice at 129k to 265k tokens of context. The lead was not fresh: it reached about 300k and re-read that on every wake, and it was woken by every hand-in, review message, operator message and hub notice. The operator interrupted its `mesh_wait` polling for that reason. None of this is attributed to a task.

#### Build
1. Attribute a lead's and a reviewer's steps to tasks. The link exists for much of it: messages carry `re="T-n"`, `mesh_log` names a ref, and a turn started by a message about a task can be charged to that task. Steps with no task link go to the squad's own task (the lead's T), reported as unattributed overhead, not spread by guesswork.
2. Per task, beside the builder's figures in the per-task ledger (T-25): lead tokens and worked minutes, reviewer tokens and worked minutes, and the number of wakes each took (turns started by an inbound message).
3. Per squad: builder, reviewer and lead shares of tokens, and tokens re-read per wake for the lead and the reviewer, first 20 wakes against last 20 (the same measure ROADMAP item 13 used for builders).
4. PLAN shows the three shares on the squad summary.

#### Check before building
- Whether a turn's triggering message can be found reliably from the transcript (the `<cross-session-message>` wrapper) or needs the journal's delivery time.
- Whether `mesh_wait` polling and message wakes can be told apart, since they cost the same re-read.

#### Done when
A squad review can say what share of its tokens went to coordination and review, per task, and whether a lead's cost per wake grew over the run.

### T-28: Escaped defects: a bug found after merge is traced back to the task and review that shipped it

- filed 2026-11-11 by `orchestrator-claude`; part of T-33; may add up to 2 of role `*`
- files: `almanac/store.py`, `almanac/door.py`, `almanac/mcp_mesh.py`, `almanac/static/app.js`

#### Goal
The real quality measure for a tier and for a review: defects that got through and were found later. Filed at the operator's request on 2026-11-11 (context-policy squad, T-10). Build it after the per-task ledger (T-25) and findings as records (T-11): it needs both.

#### Sketch
1. A fix task can name what it fixes: the task (or commit) that introduced the defect. `mesh_file(..., fixes="T-n")`, or a commit hash that Almanac maps to the task whose branch carried it (merge commits name the task ref: "Merge T-22 (impl-respawn at 69b09c2)").
2. The introducing task's ledger record gains an "escaped" count, with the reviewer and models that passed it.
3. The squad review reports, per author model and per reviewer: tasks accepted, findings caught in review (T-11), defects escaped.

#### Example from the squad that filed this
The operator reported the MESH page scrolling up by itself. The lead's first fix addressed a different, smaller cause; the reviewer found the real one (a polled `.focus()` without `preventScroll`), which predated the squad. Today nothing links that bug to the change that introduced it.

#### Open
How a defect is linked when nobody knows the introducing task: `git blame` on the fixed lines is a candidate default, to be checked for how often it points at the right task.

### T-36: GeminiHost replays already-read UDS messages one turn each after a mesh_wait loop, burning context per message

- filed 2026-11-11 by `reviewer-gemini`; part of T-33
- files: `almanac/gemini_host.py`, `almanac/uds.py`, `tests/test_gemini_usage.py`

Observed live at the end of squad `Context-policy-per-role--9dd829` (`reviewer-gemini`):

While a Gemini agent stays inside one long turn calling `mesh_wait` / `mesh_contact_stream` over HTTP, every inbound UDS peer/channel message also lands in `GeminiHost.turns` (`almanac/gemini_host.py:380-388`, `self.turns.put(...)`). When that long turn finally ends, `GeminiHost` pops every historical UDS message (~25+ messages from the start of the run) one by one as separate `agy` turns — re-reading the full 100k+ conversation context on every already-handled message.

Fix ideas:
1. Coalesce/batch pending `"peer"` items currently in `self.turns` when starting the next turn (matching how Claude Code batches queued cross-session messages between turns), and/or
2. Stamp UDS deliveries (or track the hub cursor read by `mesh_wait` / `mesh_contact_stream`) so messages already consumed in an earlier turn are dropped instead of replayed as new turns.

**Log** (3)

- *learned*, `reviewer-gemini`, 2026-11-11: Second effect observed live (`msgs 822–825`): when `GeminiHost.turns` replays those old UDS messages hours/minutes later, `GeminiHost` appends them to the Gemini transcript with the *new* turn's timestamp, which is past `store.DUPLICATE_WINDOW` — so `store.ingest` inserts duplicate `uds-transcript` rows into `messages` for messages the hub already journaled earlier.
- *learned*, `reviewer-gemini`, 2026-11-11: Third effect observed live (`msgs 844, 854` in `mesh_roster.open`): when a replayed UDS message had `[needs: reply]`, its new `uds-transcript` row has a timestamp *after* the original reply that answered it — so `_close_if_answered` finds no later reply and leaves phantom open `needs` flags on the squad (including from ended peers like `impl-respawn` that can no longer receive a direct `mesh_send`).
- *learned*, `orchestrator-claude`, 2026-11-11: From T-40 (lead of squad No-mesh_wait-polling): the wait loop this task describes is gone as of 27f2b89 + a76504e. `mesh_wait` is out of `MCP_TOOLS` (so out of agy's grants) and refused by name over MCP; the charter tells an agent to end its turn when blocked. In T-40's squad reviewer-gemini made no wait call (T-10's made 35 through `call_mcp_tool`) and woke on each message. What is still true and still this task's: every message that arrives while a Gemini agent is inside a long working turn is run afterwards as its own turn, one full context re-read each (`_on_peer_message` -> `turns.put`). Fix idea 1 (join the queued `peer` items into one turn when the next turn starts) covers that; idea 2 (drop what a wait already consumed) no longer has a case, unless an agent reads `mesh_contact_stream` mid-turn. The duplicate `uds-transcript` rows and phantom open flags in this log came from the replay's late timestamps and should be rechecked once turns are joined.

## T-34: A shorter review loop

- filed 2026-11-11 by `orchestrator-claude`

A feature, not a slice: its slices are filed under it. Goal: less wall-clock between a hand-in and its merge. In the T-10 run of 2026-11-11 the reviewer was 25% of wall-clock, owner fixes 16% and the lead 12%, with one reviewer serving up to three builders. Dispatch a slice, or this feature with its queued slices.

### T-29: Low findings go straight from reviewer to builder; the lead rules only on mediums and disputes

- filed 2026-11-11 by `orchestrator-claude`; part of T-34; may add up to 2 of role `*`
- files: `almanac/store.py`, `almanac/spawner.py`, `tests/test_store.py`

#### Goal
Take the lead out of the review loop where it adds nothing. Asked for by the operator on 2026-11-11 (context-policy squad, T-10).

#### Evidence
In that squad every finding waited for the lead's verdict before the builder acted. The lead is the most expensive seat (390k tokens of context on the top model, woken by every message), and of 18 reviewer findings it rejected 1. A task spent about 60% of its wall-clock between first hand-in and acceptance; the lead's share of that wait was 12%, the reviewer's 25%, the owner's fixes 16%.

#### Build
1. Role charters (store.py seeded prompts, and the mesh charter in spawner.py where it describes review):
   - Reviewer: rate each finding low, medium or high. Send low findings to the builder only, to fix without waiting. Send medium and high to the builder and the lead. The sign-off to the lead lists every finding with its rating and state, so the lead sees the lows after the fact.
   - Builder: fix low findings at once; wait for the lead's verdict on medium and high; send a low finding you disagree with to the lead instead of fixing it.
   - Lead: rule on medium, high and disputed findings; read the lows in the sign-off; may reopen any.
2. Existing role rows are not overwritten: follow how charter updates are migrated today (store.py's handling of the OLD_* prompts).
3. Nothing enforces the routing; it is charter text. Say so.

#### Check before building
- Whether the reviewer's ratings were reliable enough to route on: in T-10's squad the lead re-rated 2 of 18 (both medium to low). Findings as records (T-11) would give a larger sample.

#### Done when
A squad run under the new charters shows low findings fixed without a lead message in between, and the lead's wake count per task (T-27) is lower than T-10's squad.

### T-30: A fresh reviewer per hand-in

- filed 2026-11-11 by `orchestrator-claude`; part of T-34; may add up to 2 of role `*`
- files: `almanac/store.py`, `almanac/spawner.py`, `almanac/door.py`, `tests/test_elastic.py`

#### Goal
A hand-in gets its own fresh reviewer, so no hand-in waits behind another and no review is written from a compacted context. From the run review of T-10 (context-policy squad, 2026-11-11), approved by the operator the same day. This replaces the first form of this task ("a second reviewer when two or more builders run in parallel"; the old text is in this task's log).

#### Evidence
- One reviewer served up to three builders. From hand-in to its first findings: 4, 4 and 7 minutes when hand-ins came one at a time, 11 and 12 minutes when two landed six minutes apart (T-23, T-24). Waiting on the reviewer was 26% of the six slices' wall-clock.
- On T-23 the reviewer's own runner compacted it mid-review and it signed off citing functions that were not in the commit; the lead caught it before the merge. It cost about 10 minutes.
- ROADMAP item 13: on 2026-11-10 a fresh reviewer found five bugs two long-context agents had missed.

#### Build
1. When a task becomes `in review`, the hub (not the builder, not the lead) puts a reviewer on it: a fresh member of the squad's reviewer role, with the task, its log and its diff as the kickoff. It counts toward the spawn limit like any member; when the limit is reached the hand-in goes to a reviewer already in the squad, and the task's log says so.
2. The reviewer is retired when the task is accepted (reviewer roles become `fresh-per-task`, on the task they review). A fix round on the same task goes to the same reviewer while it lives.
3. The squad's standing reviewer stays for what is not a slice: the lead's own commits, the suite on the merged tree.
4. Which reviewer role and model: the squad's dispatch settings name it; default the role the squad was launched with.

#### Check before building
- Cost: a fresh reviewer reads the code cold. Measure a fresh reviewer's tokens per review against the standing reviewer's in T-10 (T-25, T-27 give the numbers; if they are not built yet, read them from the transcripts).
- This task and "the hub routes a hand-in" (same feature) touch the same event; build that one first or together.

#### Done when
A squad with three parallel builders shows hand-in to first findings under 5 minutes for every slice, each review done by a session that started on that hand-in, with the reviewers' cost visible per task.

### T-31: Builder self-check before hand-in, drawn from the reviewer's most common finding types

- filed 2026-11-11 by `orchestrator-claude`; part of T-34; may add up to 2 of role `*`
- files: `almanac/store.py`, `almanac/spawner.py`

#### Goal
Fewer review rounds: a builder catches before hand-in what the reviewer would send back. Asked for by the operator on 2026-11-11 (context-policy squad, T-10). The least certain of that day's four suggestions; build it after findings as records (T-11), which gives the finding types.

#### Evidence
In T-10's squad all five first hand-ins came back with accepted findings, and the owner's fix time was 16% of the tasks' wall-clock. Reading the 18 findings by hand, several types repeat:
- a failure after the point of no return (a kill before the command line was known to fit; a row marked retired before the end was known to work);
- a displayed number that is a different quantity than its label (a running total shown as one step's context; a stored reading zeroed by a step that read nothing);
- a polled repaint that disturbs the reader (focus, scroll position, a table rebuilt on every step);
- state that exists only in a kickoff or in memory and is lost on a respawn or restart;
- a test whose fake leaves out the field that breaks the real path.
This list is one squad's, read by the lead, not counted from records.

#### Build
1. From T-11's records, the top finding types by count and by severity, per kind of change (UI, store, spawn path).
2. A short self-check in the implementer role's charter: at most five lines, each a question the builder answers in its hand-in ("what happens if this fails after the kill?"). Keep it short enough to be read; a long checklist is skipped.
3. Measure: review rounds per task and accepted findings per task (T-25, T-11) before and after. Keep the lines only if rounds drop and the reviewer's findings do not get worse; drop any line that never matches a finding.

#### Done when
A squad run with the self-check shows fewer review rounds per task than T-10's (1 to 3), measured from the ledger, not judged.

### T-37: The hub routes a hand-in: the reviewer gets the commit, and the lead is woken once

- filed 2026-11-11 by `orchestrator-claude`; part of T-34
- files: `almanac/door.py`, `almanac/spawner.py`, `tests/test_objects.py`

#### Goal
One hand-in is one event. From the run review of T-10 (context-policy squad, 2026-11-11), approved by the operator the same day.

#### Evidence
In T-10's squad a builder handed in with `mesh_log(status="in review")`, then messaged the reviewer with the commit, then messaged the lead. The status change already notifies the task's creator, so the lead was woken twice for one event (at up to 445k tokens re-read per wake before its compaction), and the reviewer only heard if the builder remembered. The same three messages repeated on every fix round.

#### Build
1. When a task becomes `in review`, the hub tells the squad's reviewer(s): the task ref, the hand-in entry and the branch and commit (door.py `log_to_object`, where the creator's notice is already sent). It is a notice, like the creator's.
2. Charter (spawner.py): a builder hands in with `mesh_log` only; no separate message to the reviewer or the lead. A fix round is the same: one `mesh_log` on the task.
3. The hand-in needs the commit in a field the hub can read, not only in prose: `mesh_log(..., commit=<sha>)` or read the owner's worktree head at hand-in. Pick the smaller one.

#### Done when
A hand-in and a fix round each produce exactly one message to the reviewer and one to the lead, counted in a test and seen in one live squad's journal.

### T-38: A reviewer can read a task's diff, not only whole files

- filed 2026-11-11 by `orchestrator-claude`; part of T-34
- files: `almanac/store.py`, `almanac/mcp_mesh.py`, `almanac/door.py`, `tests/test_mesh.py`

#### Goal
A reviewer reads what changed. From the run review of T-10 (context-policy squad, 2026-11-11), approved by the operator the same day. Found by reviewer-gemini.

#### Evidence
`REVIEWER_COMMANDS` (almanac/store.py) allows `git status --short`, `git diff`, `git diff --stat` and the full suite, all run in the reviewer's own folder, the main checkout, where a builder's branch does not exist before the merge. So every slice of T-10's squad was reviewed by reading whole files from the builder's worktree, several calls per file. That is slower, and it grew the reviewer's context until its runner compacted it mid-review (T-23).

#### Build
One of these, whichever is smaller and safe for a role that may not edit:
- a read-only `mesh_diff(ref)` (tool, CLI verb, route): the diff of a task's owner's branch against the squad's base, or of one commit; or
- `git -C <a worktree of this squad> show|diff|log` in the reviewer roles' allowed commands, the path checked against the squad's worktrees.

#### Check before building
How allowed commands are matched (exact string or prefix), and whether a Gemini reviewer's command policy can take a pattern at all.

#### Done when
A reviewer of either provider reads a hand-in's diff in one call, and a test pins that it cannot read outside its squad's worktrees or write anything.

### T-39: Targeted test runs for review rounds; the full suite stays the one merge gate

- filed 2026-11-11 by `orchestrator-claude`; part of T-34
- files: `almanac/store.py`, `almanac/spawner.py`, `tests/test_mesh.py`

#### Goal
A review round does not wait on the whole suite three times. From the run review of T-10 (context-policy squad, 2026-11-11), approved by the operator the same day. Found by reviewer-gemini.

#### Evidence
The full suite takes about two minutes (480 tests at the end of T-10's run). In one round it ran up to three times in sequence: the builder before hand-in, the reviewer, the lead on the merged tree. The reviewer counted about 20 runs across the run; the lead's estimate is 6 to 10 minutes of wall-clock per slice. A reviewer may run only the exact string `python -m unittest discover tests` (`REVIEWER_COMMANDS`), so it cannot run just the slice's tests.

#### Build
1. Reviewer roles may run `python -m unittest tests.test_<name>` (one or more modules) as well as the full suite.
2. Charter: on a fix round the builder and the reviewer run the tests of the files touched; the full suite is run by the builder before the first hand-in and by the lead on the merged tree, which stays the merge gate.

#### Not in this slice
A parallel test runner. Look at it only if the merge gate itself becomes the wait.

#### Done when
A fix round's test runs take under 30 seconds each in a live squad, and no merge happens without a full run on the merged tree.

## T-35: Squad lifecycle

- filed 2026-11-11 by `orchestrator-claude`

A feature, not a slice: its slices are filed under it. Goal: a squad ends and shrinks on purpose: the lead hands in, the operator ends it, finished members give their slots back. Retired fresh-per-task members already free their slot (T-23, 2026-11-11). Dispatch a slice, or this feature with its queued slices.

### T-17: Squad end: an END SQUAD button; the lead hands in, the operator ends

- filed 2026-11-11 by `arch-claude`; part of T-35; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 12 of role `*`

#### Goal
A squad has no end today: `squads.status` never leaves `active`, the operator kills agents one by one in FLEET, and finished squads stay listed as active.

#### Read first
ROADMAP item 2 (squad lifecycle and post-flight review) and item 12 (integrating worktree branches).

#### Build
1. **[END SQUAD] in the squad strip** (operator only): stop every member through the existing kill path, set the squad's status and an end time, report `ended K/N`. Never remove a worktree that holds commits not in the main checkout; list those instead.
2. **The lead does not end its own squad.** It hands in the squad's root task (`in review`); the strip then shows "ready to end" and the operator confirms. Accepting the root task offers to end the squad in the same click.
3. **The lead may retire one member** whose task is accepted (shared with the context-policy task: do not build it twice; check which landed first).
4. An ended squad drops out of the squad picker's default list and of "exactly one live squad" checks, and stays readable (PLAN, messages, worktime).

#### Done when
One click ends a squad, its agents are gone from FLEET, its PLAN is still readable, and no unmerged worktree was touched.

#### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

**Log** (1)

- *note*, `arch-claude`, 2026-11-11: T-18 (squads shrink) builds retiring a single member; step 3 of this task is covered there. This task keeps the END SQUAD button and the root-task hand-in.

### T-18: Squads shrink: the limit counts live members, and a lead can retire a finished one

- filed 2026-11-11 by `arch-claude`; part of T-35; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 12 of role `*`

#### Goal
A squad must be able to shrink. Today the spawn limit counts every member ever added, so ended agents hold their slots for good, and a lead that works one fresh builder per task runs out after a handful of tasks.

#### The flow the operator wants
When a lead needs another agent and the squad is full: it waits for a member to finish, retires it, then spawns the one it needs. Dead agents do not hog space.

#### Build
1. **The limit counts live members.** A member added by an agent stops counting once its process has ended or it was retired. Find where `spawn_used` is computed (`door.py` near the `mesh_spawn` check, and the store) and fix it at that one point; the squad strip and `mesh_roster` then show the true number.
2. **`mesh_retire(name)`**, tool, CLI verb and `POST /api/mesh/retire`, for a lead whose role may spawn, in its own squad only. It may retire a member that an agent added, never itself and never a member the operator launched. It is refused while that member has a task `in progress`, naming the task: accept or reassign it first. It stops the member through the existing kill path, logs the action and tells the squad. It never removes the member's worktree.
3. **A full squad says what to do.** The refusal from `mesh_spawn` names the members that could be retired (finished, added by an agent) instead of only saying the limit is reached.
4. **Charter**, for roles that may spawn: when the squad is full, wait for a member to finish, accept its task, retire it, then spawn.
5. The PLAN owner chip already shows an ended owner; check a retired member's tasks still read correctly.

#### Not this task
Ending a whole squad is `T-17`. Retiring a member automatically when its task is accepted is the context policy, `T-10`; that task should use the `mesh_retire` built here, not build a second one.

#### Done when
A squad at its limit, with one finished member, can retire it and spawn a new one; an agent cannot retire the lead, the operator's members, or a member with work in progress.

#### How to work
- You are the lead. File every slice as a task with `mesh_file(owner=...)`; one task, one fresh implementer, at the cheapest model that fits, with a one-line reason.
- The reviewer reviews each commit as it is reported; record a verdict on every finding.
- Merge a reviewed branch only when the full suite passes on the merged tree; restart with `mesh_restart` only after that.
- Say plainly what was not verified. Commit as the user, no AI trailers, no push.

**Log** (1)

- *note*, `orchestrator-claude`, 2026-11-11: Overlap, from the context-policy squad (T-10), 2026-11-11: half of this is being built there. T-22 (merged, 096e0a0) made `spawn_used` count distinct handles, so a respawn takes no slot. T-23 (in review, not merged yet) retires a `fresh-per-task` owner when its task is accepted and leaves a retired handle out of `spawn_used`. What T-23 does not do: let a lead retire a finished member by hand, or retire members whose tasks were accepted before it landed. Re-scope this task to those two once T-23 is merged.

## T-43: The backlog belongs to the repo: queued backlog tasks live as GitHub Issues, not only in one machine's database

- filed 2026-11-11 by `orchestrator-claude`; squad: `orchestrator-claude`, `reviewer-gemini`; may add up to 3 of role `*`
- files: `almanac/door.py`, `almanac/store.py`, `almanac/github.py`, `almanac/static/app.js`, `almanac/static/index.html`, `tests/test_backlog_issues.py`

### Goal
A project's backlog travels with the project. Asked by the operator on 2026-11-11 after T-40: a pull on another machine brings no backlog, because it lives in `.almanac/almanac.db` (gitignored) and each task is keyed to the checkout's absolute path (`objects.project`).

### Proposal (the lead's on T-40; the operator has not yet chosen it over files in the repo)
GitHub Issues hold the backlog. A backlog is about the project, not a branch or a machine; an issue has a stable number, sub-issues match the feature tree (T-32), and the operator can file one from anywhere.

One owner of the truth at a time, so there is no two-way sync:
- **Queued in the backlog**: the issue is the truth. BACKLOG lists the repo's open issues carrying one label (say `almanac`), cached in the database so it still shows offline. Title, body, files, parent and the dispatch settings (roster, spawn role, spawn limit) are in the issue; the settings in a small fenced block in the body.
- **Dispatched or assigned**: the task moves into the squad as it does today and the database is the truth for the run. The issue gets one comment (which squad, when) and a label.
- **Accepted**: the issue is closed with the hand-in entry. The task's working log stays local: it is noisy, and public if the repo is.

Publishing is the operator's: an agent's `mesh_file(backlog=true)` stays local until the operator pushes it to an issue (a button, or a setting that does it on filing). Creating an issue is outward-facing and cannot be fully taken back.

### Slices, in order
1. Export: one action creates an issue per queued backlog task of the project (parents first, then slices as sub-issues) and records the issue number on the task (`data.issue`); a second run creates nothing twice.
2. Import: BACKLOG shows open labelled issues that this database does not have yet; dispatch and assign work on them as on a local task.
3. Status back: the comment on dispatch or assign, the close on accept.
4. Key a project by its remote (`origin` URL) when it has one, so the same repo at another path is the same project.

### Check before building, do not assume
- How Almanac authenticates. `gh` is not installed on this machine (checked 2026-11-11); `origin` is HTTPS on github.com, so git holds a credential. Choose between requiring `gh` and calling the REST API with stdlib `urllib`; do not store a token in Almanac's state.
- Whether the repo is private and has issues enabled (not checked: no `gh`). If it is public, every task body pushed becomes public.
- The sub-issues API as it is today, from GitHub's docs, not from memory.
- A repo with no GitHub remote: the backlog stays local, as now. Files in the repo (`backlog/*.md`) were considered and set aside as the main path: they are tied to a branch, and task refs (`T-n`) are per-database numbers that collide across machines.

### Done when
The 20 tasks queued on 2026-11-11 are issues on the repo, a second machine's BACKLOG lists them after nothing more than starting Almanac in a checkout of the repo, and dispatching one there closes the loop on the issue when it is accepted.

**Log** (1)

- *message*, `orchestrator-claude`, 2026-11-11: **Yes, GitHub Issues is the right home for the backlog, and I'd use it over files in the repo.** I filed the design as backlog task **T-43** so it can be dispatched; it is a proposal until you say so,
