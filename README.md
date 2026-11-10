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
`http://127.0.0.1:8812/login?token=<token>`, which sets a signed, HttpOnly cookie for a week; scripts send
`Authorization: Bearer <token>`. Only `/ident` and `/healthz` answer without it. The server binds loopback only,
refuses an unexpected `Host` (DNS rebinding) and refuses a cross-origin POST.

## Views

**FLEET** — every `claude.exe` and managed Gemini (`agy.exe` / `almanac.gemini_host`) process, scanned every couple of seconds (`almanac/fleet.py`):

| role | what it is |
|---|---|
| session | a Claude Code or Gemini agent session; joined to `~/.claude/sessions/<pid>.json` (name, provider, role, squad, `messagingSocketPath`, busy/idle, folder, session id) and its transcript |
| headless | a `claude -p` or `agy -p` worker |
| daemon / pty-host | Claude Code's background supervisor and the console host of a `bg` session |
| desktop | the Claude desktop app; its Electron helpers fold under it |

Open a session to see its transcript tail, provider, role, model, effort and token totals. Use **[+ SPAWN AGENT]** or
**[+ LAUNCH SQUAD]** directly from FLEET to launch single Claude/Gemini agents or coordinated cross-model squads with
optional `git worktree` isolation.

- **KILL** ends the process and its children (`taskkill /T /F`), after a fresh scan finds the same pid with the
  same start time, so a reused pid is never hit.
- **MODEL** lets you set per-member models when launching a squad (e.g. `opus-5-5`, `gemini-4-carbon-high`) or click any live session row's `MODEL` cell (`m` / **[MODEL ✎]**) to switch its model inline (`POST /api/model`).
- **SAY** delivers structured `<cross-session-message>` frames over Claude Code's native `uds-messaging` Named Pipe
  (`almanac/uds.py`) whenever the session advertises `messagingSocketPath` + `.key`, falling back to typing into the
  session's console (`almanac/console.py`: AttachConsole + WriteConsoleInputW) for legacy sessions.
- **REMOTE** opens the session's Remote Control page on claude.ai when it has one.

**GALLERY** — many live Claude and Gemini sessions tiled side-by-side, each with provider/UDS badges and its own SAY box; pin the ones you care about.

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

**LOG** — every kill, message and resume.

## Config

`config.json` (every key optional):

| key | default | |
|---|---|---|
| `port` | `8812` | `127.0.0.1:<port>` |
| `transcripts` | `~/.claude/projects` | Claude Code's transcript folder |
| `interval` | `300` | seconds between transcript reads (≥ 30) |
| `claude` | `~/.local/bin/claude.exe`, else `claude` on PATH | used to spawn, resume, and name Claude sessions |
| `gemini` | `~/.gemini/bin/agy.exe`, else `agy`/`gemini` on PATH | used by `almanac.gemini_host` to spawn Gemini sessions |
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
    python -m almanac.mcp_mesh --cli send <to> <msg># send over uds-messaging (peer, @squad, @all, #ch)
    python -m almanac.gemini_host --name gem-1 --cwd . --prompt "Audit auth"

## Tests

    python -m unittest discover tests

## License

MIT, see `LICENSE`.
