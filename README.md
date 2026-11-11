# Almanac — cross-model mission control for Claude & Gemini agents

Every Claude Code and Gemini (`agy`) agent session on this PC, live, unified peer-to-peer communication over Claude
Code's native `uds-messaging` protocol, live Context & Contact Streams, multi-agent Squad & Role orchestration, every
chat you have ever had, and how your sessions spend tools and tokens over time. One stdlib Python service (no
dependencies), served on `127.0.0.1:8812`. A terminal-style page; press `?` for keys.

Windows only for the live views (FLEET, GALLERY, MESH, SAY, KILL use the Win32 process table, Named Pipes, and console API).
CHATS and TELEMETRY read `~/.claude/projects` and `.almanac/gemini/projects`.

## Quick start

Needs Python 3.11+ and Claude Code (`claude.exe`) and/or Antigravity Gemini CLI (`agy.exe`); nothing to install.

    git clone https://github.com/glabadie2001/almanac.git
    cd almanac
    ./run.sh                          # Git Bash / WSL: start Almanac + Mesh Hub and open browser
    .\run.ps1                         # PowerShell (or run.cmd): start Almanac + Mesh Hub and open browser
    ./run.sh url                      # print the sign-in link again
    ./run.sh mesh roster              # inspect live Claude & Gemini peers from the CLI
    ./run.sh test                     # run the offline unit test suite

The first start makes a secret in `.almanac/token`. The page is reached through
`http://127.0.0.1:8812/login?token=<token>`, which sets a signed, HttpOnly cookie for a week, named
`almanac_session_<port>` so that a second Almanac on the same host (another port) cannot sign this one's browser out;
scripts send `Authorization: Bearer <token>`. Only `/ident` and `/healthz` answer without it. The server binds
loopback only, refuses an unexpected `Host` (DNS rebinding) and refuses a cross-origin POST.

Each spawned agent gets its own bearer (`ALMANAC_TOKEN` in its environment). It opens `/api/mesh` and below only, and
it names its holder, so an agent cannot post as someone else or reach `/api/kill`, `/api/spawn`, `/api/operator/*`,
`/api/squad*` or `/api/health`. This stops accidents, not an adversary: an agent runs as your user. Nor is it
a privacy boundary: under `/api/mesh` an agent can read any session's context stream (your own chats included)
and any squad's messages, your inbox among them.

The header shows `[✉ n]` (unread messages to you; amber when one wants a reply, red when one is blocking) and
alert chips on every tab: `▲ blocked on operator 4m` when an agent cannot continue until you answer, and
`▲ gemini runner missing` / `▲ claude runner missing` when Almanac cannot find a CLI (click for where it looked).

## Views

**FLEET** — every `claude.exe` and managed Gemini (`agy.exe` / `almanac.gemini_host`) process, scanned every couple of seconds (`almanac/fleet.py`):

| role | what it is |
|---|---|
| session | a Claude Code or Gemini agent session; joined to `~/.claude/sessions/<pid>.json` (name, provider, role, squad, `messagingSocketPath`, busy/idle, folder, session id) and its transcript |
| headless | a `claude -p` or `agy -p` worker |
| daemon / pty-host | Claude Code's background supervisor and the console host of a `bg` session |
| desktop | the Claude desktop app; its Electron helpers fold under it |

Open a session to see its transcript tail (filter it: `all | dialog | contacts | tools`), provider, role, model,
effort, token totals and worktime (`31m work · 4m waiting · 12 turns`). Use **[+ SPAWN AGENT]** or
**[+ LAUNCH SQUAD]** directly from FLEET to launch single Claude/Gemini agents or coordinated cross-model squads with
optional `git worktree` isolation.

- **KILL** ends the process and its children (`taskkill /T /F`), after a fresh scan finds the same pid with the
  same start time, so a reused pid is never hit.
- **MODEL** lets you set per-member models when launching a squad (e.g. `opus-5-5`, `gemini-4-carbon-high`) or click any live session row's `MODEL` cell (`m` / **[MODEL ✎]**) to switch its model inline (`POST /api/model`).
- **SAY** delivers structured `<cross-session-message>` frames over Claude Code's native `uds-messaging` Named Pipe
  (`almanac/uds.py`) whenever the session advertises `messagingSocketPath` + `.key`, falling back to typing into the
  session's console (`almanac/console.py`: AttachConsole + WriteConsoleInputW) for legacy sessions.
- **REMOTE** opens the session's Remote Control page on claude.ai when it has one.

**GALLERY** — many live Claude and Gemini sessions tiled side-by-side, each with provider/UDS badges, its worktime and
its own SAY box; pin the ones you care about. One `all | dialog | contacts | tools` filter applies to every tile.
Show `all | pinned | busy | squad`. When a pinned or squad session ends, its tile stays (dashed, `ended hh:mm`, its
last output read once more) with **[CHAT ▸]** and **[DISMISS ✕]**; at most 12 are kept, across reloads. The folder
shows its last two segments, `⑂` marking a worktree.

**MESH** — cross-model orchestration over Claude Code's `uds-messaging` protocol (`almanac/uds.py`, `almanac/mcp_mesh.py`, `almanac/spawner.py`, `almanac/gemini_host.py`):

- **Unified `uds-messaging` Bus**: Almanac registers `almanac-hub` and every managed Gemini host (`almanac.gemini_host`)
  directly in `~/.claude/sessions/<pid>.json` + `<pid>.<sha256(lower(pipe))>.key` on `\\.\pipe\LOCAL\cc-msg-<hex>`.
  Native `claude.exe` sessions discover Gemini peers automatically through their built-in `SendMessage` tool.
- **Peer Context Streams**: Every agent (via MCP `mesh_context_stream`) and the operator can read the live normalized
  transcript stream (prompts, replies, tool calls, errors, peer messages) of any Claude or Gemini peer.
- **Contact Stream**: Real-time feed of all `<cross-session-message>` traffic across peers, `@squad`, `@all`, and
  `#channel` topics, indexed into SQLite (`messages`) with long-poll wakeup (`/api/mesh/wait` / `mesh_wait`).
- **Roles & Squads**: Reusable role definitions (`architect-claude`, `architect-gemini`, `implementer-claude`,
  `implementer-gemini`, `reviewer-claude`, `reviewer-gemini`, plus custom roles via **[ROLES ⚙]**) bundle provider,
  model, effort, workspace isolation (`shared` or `git worktree`), and system prompts, automatically wiring each
  spawned agent with `almanac.mcp_mesh` (`--mcp-config` for Claude, `--add-dir` plugin bundle for `agy`).
- **Flags that want an answer**: `mesh_send(..., needs="reply")` (wanted, not urgent) or `needs="blocking"` (the
  sender cannot go on). The message arrives with a `[needs: …]` line and stays open until its recipient sends the
  sender a direct message; a broadcast answers nothing. Open flags show on messages, in the squad strip
  (`impl-claude waits on arch-claude 2m (reply)`), as a passive `✉N` badge on an idle peer that owes an answer, and,
  for flags to you, in the header and the INBOX.
- **Who may page you**: a role's `may_page_operator` (architects by default; the **[ROLES ⚙]** checkbox) lets its
  agents `mesh_send(to="operator")`. Any agent may answer the message you last sent it, once. A refused agent is
  told which teammate to relay through.
- **Topology**: the selected squad as a fixed graph, not a physics one: columns operator | architect | implementer |
  reviewer | other, channels underneath, one curved edge per direction (width by count, amber/red with open flags).
  Click a node for its context stream, an edge to filter the contact stream to that pair. MESH opens on the newest
  squad with a live member.
- **Worktime heatmap**: per agent, when it worked (a 5-step ramp by events or output tokens per minute), waited in
  `mesh_wait`, or sat idle; a PARALLEL row and a summary of how long agents worked at once, alone, and in which
  pairs. `heat + events` overlays tool errors, file edits and each message (sender to recipient, coloured by flag).
  A turn runs from a prompt to the last record before the next one; time on a permission prompt counts as work.
- **Elastic squads**: a squad may let its architect add members while it runs. The operator sets the role it may
  add (one role, or `*` for any defined role) and how many (`spawn_limit`, 0 to 20), at launch ("architect may
  add … up to …") or later in the squad strip (**[LIMIT ✎]**). The architect calls `mesh_spawn(task=…)` (with
  `role=` under `*`); the role's own definition decides model, effort, workspace and permission mode, never the
  agent. The newcomer gets the squad's brief, its task and roster, and the squad hears who joined. The operator's
  own **[+ ADD MEMBER]** never uses the limit. A role may add members only with `may_spawn` (architects by default).
  With `*`, the architect may also add a role that can page you or add members itself (another architect); every
  such member still counts against the one limit. Handles are never reused within a squad, and `operator`,
  `almanac-hub`, `hub`, `peer`, `all` and `squad` are never an agent's handle.

**CHATS** — every chat Almanac has seen, searchable, readable, and resumable (`claude --resume` in the chat's own
folder, in a new console, optionally with Remote Control). Almanac mirrors `~/.claude/projects` into
`.almanac/archive` on every pass and never deletes, so a chat Claude Code has pruned can still be read and is
restored before it is resumed.

**Names** — ended chats with no name get one from Haiku (3–7 words), written back with the same records Claude
Code's own `/rename` writes, so `claude --resume` shows it too. A name you gave is never replaced; a live chat's
file is never touched. The call goes through your own `claude -p` login from an empty folder with no tools,
hooks, MCP servers or CLAUDE.md (about 1k tokens, ~$0.0002 each). Set `"naming": false` to turn it off. To name a
chat the moment it ends, add a SessionEnd hook that POSTs `{"id": <session id>, "after_exit": true}` to
`/api/name` with the bearer token.

**TELEMETRY** — every five minutes Almanac reads the transcripts from where it stopped and keeps its own rows in
`.almanac/almanac.db`, so history survives transcript cleanup: one row per tool call (purpose, leading word,
characters read back, wait) and per assistant message (tokens). Tiles compare this period with the previous one
of the same length. The "adoption" tile scores four shell habits (`rg` over recursive grep, a changed-tests
runner over whole suites, and two helper wrappers, `door` and `swap`); it reads near zero unless you use them.
Filter by provider (`all | claude | gemini`); **BY MODEL** lists each model with its sessions, output, context per
output token, KB per call and tool error rate against the period before. The error rate divides by calls whose
result arrived, never by all calls.

**LOG** — every kill, message, spawn and resume.

**INBOX** (key `7`) — what agents sent you, apart from their chatter: one thread per agent and squad, unread counts,
`BLOCKING 4m` / `REPLY` badges, and a reply box (with its own `needs` choice) that addresses the agent by session, so
a handle reused in another squad is never reached by mistake. **[RESOLVE ✓]** closes a flag by hand.
**[NOTIFY ◇]** asks for browser notifications (only from that click) and then sends one per blocking message.

## Config

`config.json` (every key optional):

| key | default | |
|---|---|---|
| `port` | `8812` | `127.0.0.1:<port>` |
| `transcripts` | `~/.claude/projects` | Claude Code's transcript folder |
| `interval` | `300` | seconds between transcript reads (≥ 30) |
| `claude` | `~/.local/bin/claude.exe`, else `claude` on PATH (looked up again on use) | used to spawn, resume, and name Claude sessions. Never a `.cmd` / `.bat` (npm's shim): cmd.exe would re-parse the prompts Almanac passes as arguments, so it is refused with that reason; point `claude` at `claude.exe` |
| `gemini` | `~/.gemini/bin/agy.exe`; if missing, the first `agy`/`gemini` (`.exe`/`.cmd`) in `~/.gemini/bin`, `%LOCALAPPDATA%\agy\bin`, `~/.gemini/antigravity/bin`, `%LOCALAPPDATA%\Programs\antigravity\{bin,resources\bin,resources\app\bin}`, `%ProgramFiles%\Antigravity\{bin,resources\bin}`, `%ProgramFiles%\agy\bin`, then PATH | used by `almanac.gemini_host` to spawn Gemini sessions. Looked up again on use (at most every 10 s), so an install made while Almanac runs is found on the next spawn; while none is found, Gemini spawns are refused and the header shows `▲ gemini runner missing` |
| `naming` | `true` | name ended chats with Haiku through `claude -p` |
| `naming_model` | `"haiku"` | any `--model` value the CLI accepts |
| `prices` | `{}` | `{"claude-opus-5-5": 5.0}` — USD per million input tokens, for the resume cost estimate |
| `hosts` | `[]` | extra `Host` values to accept, e.g. `"box.example:443"` behind a reverse proxy |

## Keeping transcripts

Claude Code deletes transcripts older than `cleanupPeriodDays` (30 by default). Almanac's archive keeps a copy
regardless; to keep the originals too, set `"cleanupPeriodDays": 3650` in `~/.claude/settings.json`. A chat
resumed from Almanac is started with that setting already.

## Other commands

    python -m almanac --config config.json --state .almanac --port 8812
    python -m almanac --ingest                     # one read, print the counts
    python -m almanac --seed tooluses.jsonl        # load an older inventory call list once
    python -m almanac.console PID < text           # type a line into a console (Windows)
    python -m almanac.mcp_mesh --cli roster        # inspect live cross-model mesh peers from CLI
    python -m almanac.mcp_mesh --cli context <peer># read a peer's normalized Context Stream
    python -m almanac.mcp_mesh --cli contacts      # read the mesh Contact Stream
    python -m almanac.mcp_mesh --cli send <to> <msg># send over uds-messaging (peer, @squad, @all, #ch); --needs reply|blocking
    python -m almanac.mcp_mesh --cli spawn <task>  # an architect adds a squad member (--role under '*', --name)
    python -m almanac.gemini_host --name gem-1 --cwd . --prompt "Audit auth"

## HTTP API

Every route is listed with its parameters at the top of `almanac/door.py`. The ones added most recently:

| route | who | what |
|---|---|---|
| `GET /api/health` | operator | runners found or missing and where Almanac looked, hub up, last ingest error |
| `GET /api/operator/inbox` | operator | messages to and from you, unread and open-flag counts (`limit=0`: counts only) |
| `POST /api/operator/read`, `/resolve` | operator | mark messages read; close a flag by hand |
| `POST /api/mesh/send` `needs` | both | `reply` / `blocking`; an agent reaches `operator` only if its role may page |
| `GET /api/worktime` | operator | work / wait / idle per agent, heat buckets, overlap, swimlane messages and marks |
| `GET /api/mesh/graph?squad=` | both | topology nodes (ended agents kept) and message edges with open flags |
| `POST /api/squad` | operator | `{id, spawn_role, spawn_limit}`: what the architect may add |
| `POST /api/squad/member` | operator | add a member to a running squad (never counts against the limit) |
| `POST /api/mesh/spawn` | agent | `{task, name?, role?}`: an architect adds a member within the limit |
| `GET /api/session(s)` `kinds=` | operator | the feed filtered by event kind before `limit` cuts it |
| `GET /api/report` `provider=` | operator | telemetry for one provider, with `models` and `providers` tables |

## Tests

    python -m unittest discover tests

## License

MIT, see `LICENSE`.
