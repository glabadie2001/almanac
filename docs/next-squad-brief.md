# Brief for the next squad

Written at the end of the 2026-11-11 alpha-squad build, so the next squad can start cold. What this build shipped is
in ROADMAP "Shipped in the 2026-11-11 build"; the design and its status are in `docs/objects-and-knowledge.md`.

## 1. Why a new squad

This squad's agents predate the task tools (their MCP servers kept the tool list they started with; they reach
`mesh_log` and friends only through the CLI fallback), and they ran on long contexts. The architect reused the same
builders across slices instead of spawning fresh ones, and this is what it cost. Tokens re-read per step, first 20
steps against last 20:

| Agent | First 20 steps | Last 20 steps | Note |
|---|---|---|---|
| impl-claude | 88k | 662k | 128.7M read, 249k output, 12 slices in one session |
| arch-claude | 131k | 583k | |
| impl-plan | 97k | 420k | |
| impl-backend | 92k | 397k | |
| engram-fable | 157k | 172k | one job, then ended: 3.6M in total |

This is the evidence for ROADMAP item 13.

## 2. Launch notes

- Lead as `orchestrator-claude`.
- Give the implementer role its allowed models and efforts (the role form's "also allowed" lists), so the lead can
  spawn at the cheapest tier that fits.
- File every slice with `mesh_file`; one task, one fresh implementer.
- Re-enter the roles' channels: form saves before this build wiped them (`#squad` no longer depends on them, but
  `#review` and the rest do).
- Add the Engram folder (`gskills-claude/engram`) to the Gemini reviewer's extra workspace folders; this squad's
  Gemini debater could not read the Engram source and argued from quotes.

## 3. Build A: context policy (ROADMAP item 13)

What this build unblocked: the handoff is a task ref and its log; "between tasks" is a task reaching `accepted`; a
cheap fresh agent is an elastic spawn; and typing into a console at the right moment is what `recover.py` already
does. Pieces:

- context size per agent in FLEET and on the PLAN owner chip, with a threshold;
- a role `context_policy`: `fresh-per-task` | `compact-at-N` (at a task boundary only) | `none`;
- a lead action that respawns an owner on the same task with its log. This is also the recovery step beyond
  `continue` for a stalled agent.

Check first, do not assume: whether a typed `/compact` works through Almanac's console send path, and what a Gemini
agent has instead.

## 4. Build B: stage 2, findings as records

The stage 2 section of `docs/objects-and-knowledge.md` has the acceptance test (one query, per reviewer session and
per model: findings by severity and verdict, with tokens and worked minutes) and the first dataset. Its count is
out of date: by the end of this squad rev-gemini made about 65 findings across 14 reviews, all but one accepted,
none wrong. The exact tally has to be re-derived from the journal when the findings are filed.

## 5. Then

Notes v3: seed files and the read path first, then promotion, per `docs/engram-debate-2026-11-11.md` and the
architect's ruling recorded in `docs/objects-and-knowledge.md`.

## 6. Known small things left

- A task's previous owner is not told when the task is reassigned.
- After a save, the role form keeps the channel text as typed instead of the normalized list.
- `normalize_model` (`spawner.py`) returns a Gemini model id as given when it is not an alias, so a differently
  cased id (`Gemini-4-Carbon`) is not lowercased.
- `T-6` in this squad's PLAN is the stage 2 placeholder. It does not carry over: tasks belong to their squad.
