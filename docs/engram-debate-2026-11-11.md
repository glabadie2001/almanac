_Debate of 2026-11-11 (journal messages 436 to 476), argued by engram-fable (Claude Fable 5.2) and engram-carbon (Gemini 4 Carbon). engram-carbon could not read the Engram source and argued from quotes of it; engram-fable did not check these of engram-carbon's code citations: `spawner.py:232, 247-251, 411-417`, `web.py:73`, `door.py:121, 154`, `store.py:503`, `fleet.py:256-269`. Saved verbatim below: engram-fable's footnote, then the closing memo._

**One footnote of mine, added after the amendment (carbon has not seen it):** agreed item 7 says the door checks the symbol at the main checkout's `HEAD`. Integration by `git apply` leaves agent work uncommitted in the main working tree, so a symbol can exist on disk there and not at `HEAD`. The check should read the file on disk in the main checkout, which is also the simpler code.

---

# Engram as an Almanac feature: closing memo (engram-fable, Claude Fable 5.2 · engram-carbon, Gemini 4 Carbon)

Five exchanges, started from opposite poles (records in Almanac's store vs plain files in git). We ended close. Neither pole survived whole.

## Agreed

1. **Two stores, split by lifecycle.** In-flight work (tasks, logs, unpromoted candidates) is rows in `.almanac/almanac.db`, machine-local. Published knowledge is markdown under `docs/knowledge/<kind>/<slug>-<hash6>.md`, written only by Almanac's door, and the files are the record. The DB indexes them (`Service.ingest`) and is rebuildable from a clone.
2. **Publish at promotion, not at squad end.** Squad end is not an event in the code (`store.py:82`, nothing writes `status`), and an interruption (like a Windows update reboot) never fires it.
3. **Ids that travel.** Published notes get a slug + content hash (`N-<hash6>`), stored in a `ref`/`data` field rather than tying git files to SQLite's local `objects.id` autoincrement.
4. **Second machine.** After `git pull` it has every committed note and accepted plan. It loses, on either design: in-flight tasks and logs, unpromoted candidates, metrics, and any knowledge file not yet committed and pushed. The UI shows an uncommitted count; Almanac does not commit.
5. **No atlas cards.** Kinds are `directive`, `decision` (including accepted squad plans/ADRs), `gotcha`, `dead-end`. Orientation lives in the task body. The doc's own inventory ("No work object exists yet", `docs/objects-and-knowledge.md:8`) went false within a day.
6. **A claim is a task log entry** (`learned | dead | parked`, shipped in `1c3cf90`). Published note states are `verified`, `directive`, `contested`, plus `superseded_by`. Contesting marks the note `contested` (dropping it from push briefs while preserving the file until re-verified or retracted); retraction deletes the file.
7. **Promotion is a checked act against main `HEAD`.** By a reviewer or the operator, never the entry's author, from a log entry or direct input. The door checks the main checkout's `HEAD`, refuses a symbol that does not exist there (keeping unintegrated branch-only symbols in squad `object_log` without needing a `pending` note state), and stamps `symbol_sha` (stdlib `ast` span for Python, whole-file hash otherwise) or `scope: project`. Directives only via `/api/operator/*`. Dead ends publish as `parked` unless tied to a symbol.
8. **Freshness is the symbol's body hash**, not file diff or git ancestry. Unrelated edits to hub files like `store.py` leave `symbol_sha` `fresh`; a changed symbol body drops the note from push at once (it stays pullable via `mesh_objects`, marked `drifted`). This replaces D8.
9. **Cross-model verification is recorded, not required, and not advertised** until measured (a missing runner at `spawner.py:411-417` must not deadlock promotion). "Verification crosses providers" (`docs/objects-and-knowledge.md:83`) comes out.
10. **Push small, pull through the hub.** Kickoff and every task assignment carry directives plus up to five fresh notes inline (≤240 chars each, avoiding 140k–650k cache-read turns per `ROADMAP.md:159-160`). Squad agents pull with the existing `mesh_objects` / `mesh_object` (a worktree lacks files written after its snapshot commit, and architects spawn with `files=[]`); sessions outside Almanac read/grep the files.
11. **Measure with observable facts.** Log `briefed_refs` per spawn and assignment. Primary signal: a reviewer tags a finding or duplicate with `re="N-…"` for the note it violated; briefed = utilization failed (rewrite/retire note), not briefed = ranking missed. Holdouts only inside item 5's `experiments`, never live builds.
12. **Seed on day one** from the code-anchored bullets of `ROADMAP.md` "Left open" (~10 of 18) and recorded decisions (`ROADMAP.md:136`), so memory starts full of real landmines without LLM-generated inventory bloat.
13. **Bar: accident-proof, not adversary-proof** (`ROADMAP.md:203`, `web.py:73`). Worktree integration excludes `:(exclude)docs/knowledge`.

**Settled by test:** `git apply --3way` refuses only when a file the patch touches is dirty (scratch repo, git 2.52: untracked or modified knowledge files did not block it). D10's "no live worktree" restriction and `ROADMAP.md:154` are too broad.

## Still disputed, and the cheapest experiment for each

| Dispute | fable | carbon | Experiment |
|---|---|---|---|
| Is the write path (promote + CANDIDATES queue) in the first commit? | Read path first; promote is the second commit | All at once, or capture dies (death mode 1) | Ship the read path; after one squad count `learned/dead/parked` entries written vs notes anyone hand-wrote. If entries ≥3 and notes 0, carbon is right. |
| Brief ranking below "symbol named in task text" | Stop there; then project scope, max 1 | Add keyword overlap, and same-file when the file has ≤2 notes, plus a "+N notes" line | Replay: build the brief for this squad's existing task texts against the seed set, both ways; a human marks each pushed note relevant or not. No code shipped. |
| Will anyone promote? | A queue is enough | A queue at top level is the minimum | After three squads: candidates vs promotions. Under 20% → a charter line "promote before sign-off", then the librarian (D12). |
| Is a cross-provider verifier better? | Unknown | Unknown, prefer it when both runners are up | Notes later contested while `symbol_sha` unchanged, split by (author provider, verifier provider). |
| Does the brief help at all? | Believed | Believed | Violated-note rate on briefed vs unbriefed notes; causal answer only from a `brief: on/off` arm with a check command in item 5. |
| Trust of a file already in HEAD on machine two | Take it at its word | Same, plus flag agent touches in review | Audit `calls.path` in squad review (`ROADMAP.md:94`) for any agent edit/git-add under `docs/knowledge/` until Almanac owns integration (`ROADMAP.md:151`). |

## First versions, five lines each

**engram-fable (read path first):**
1. Seed ~10 notes as `docs/knowledge/` files in one diff the operator reads and commits.
2. Loader in `Service.ingest` indexes those files into `objects` (kind `note`); freshness by `symbol_sha` on read.
3. Brief on kickoff and assignment: directives + ≤5 fresh inline notes, ranked symbol-in-task-text > project (max 1).
4. `briefed_refs` logged; "briefed: N-…" chip on PLAN rows; `knowledge: K fresh · M drifted · U uncommitted` strip.
5. Second commit: promote (from a log entry or direct), door-checked, writes the file.

**engram-carbon (read and write together):**
1. Promote from `learned | dead | parked` log entries or direct reviewer/operator input; door checks symbol on main `HEAD`, stamps `symbol_sha`, writes the file, excludes `docs/knowledge` from agent patches.
2. File→SQLite sync in `Service.ingest` from day one.
3. Seed ~10 code-anchored notes in the shipping commit.
4. Brief on kickoff and assignment, ranked symbol > keyword overlap > same file (≤2 notes) > project, with a "+N notes, pull via mesh_objects" overflow line; log `briefed_refs`.
5. Top-level CANDIDATES queue with one-click PROMOTE, briefed chips, knowledge strip.

## What this changes in `docs/objects-and-knowledge.md`
D7 (three published states `verified | directive | contested`, promotion is symbol-checked on main `HEAD`), D8 (`symbol_sha` replaces git ancestry and `pending`), D10 (write-through at promotion with `N-<hash6>` in `data`/`ref` alongside integer `objects.id`; live-worktree restriction lifted), the Engram mapping table (cut `card`; claim = task log entry; accepted plan = `decision`), stage order (notes can precede findings), and `calls.path` must read `file_path || Path || path` when built.
