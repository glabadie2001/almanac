# Almanac — mission control for Claude Code

Every Claude Code process on this PC, live, every chat you have ever had, and how your sessions spend tools and
tokens over time. One stdlib Python service (no dependencies), served on `127.0.0.1:8812`. A terminal-style page;
press `?` for keys.

Windows only for the live views (FLEET, GALLERY, SAY, KILL use the Win32 process table and console API).
CHATS and TELEMETRY only read `~/.claude/projects`.

## Quick start

Needs Python 3.10+ and Claude Code; nothing to install.

    git clone https://github.com/glabadie2001/almanac.git
    cd almanac
    python -m almanac --open          # start, and open the signed-in page in your browser
    python -m almanac --url           # print the sign-in link again

The first start makes a secret in `.almanac/token`. The page is reached through
`http://127.0.0.1:8812/login?token=<token>`, which sets a signed, HttpOnly cookie for a week; scripts send
`Authorization: Bearer <token>`. Only `/ident` and `/healthz` answer without it. The server binds loopback only,
refuses an unexpected `Host` (DNS rebinding) and refuses a cross-origin POST.

## Views

**FLEET** — every `claude.exe`, scanned every couple of seconds (`almanac/fleet.py`):

| role | what it is |
|---|---|
| session | a Claude Code session; joined to `~/.claude/sessions/<pid>.json` (name, busy/idle, folder, session id, Remote Control id) and its transcript |
| headless | a `claude -p` worker |
| daemon / pty-host | Claude Code's background supervisor and the console host of a `bg` session |
| desktop | the Claude desktop app; its Electron helpers fold under it |

Open a session to see its transcript tail, model, effort and token totals.

- **KILL** ends the process and its children (`taskkill /T /F`), after a fresh scan finds the same pid with the
  same start time, so a reused pid is never hit.
- **SAY** types one line and Enter into the session's console (`almanac/console.py`: AttachConsole +
  WriteConsoleInputW in a throwaway detached process), exactly as if typed. Newlines fold to spaces. A busy
  session queues it. Anything typed is acted on with that session's permissions, including slash commands.
- **REMOTE** opens the session's Remote Control page on claude.ai when it has one.

**GALLERY** — many live sessions tiled, each with its own SAY box; pin the ones you care about.

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
| `claude` | `~/.local/bin/claude.exe`, else `claude` on PATH | used to resume and to name chats |
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

## Tests

    python -m unittest discover tests

## License

MIT, see `LICENSE`.
