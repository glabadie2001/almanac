"""Settings (no secrets; the door token lives in <state>/token)."""
import copy
import json
import os
import shutil
from pathlib import Path

DEFAULTS = dict(
    port=8812,                              # 127.0.0.1:<port>
    transcripts="~/.claude/projects",       # Claude Code's transcript folder
    interval=300,                           # seconds between transcript reads (>= 30)
    claude="~/.local/bin/claude.exe",       # the CLI that spawns/resumes Claude chats and names them
    gemini="~/.gemini/bin/agy.exe",         # the CLI that powers Gemini agents (`agy` or `gemini`)
    naming=True,                            # name ended chats with Haiku through `claude -p` (spends on your Claude login)
    naming_model="haiku",                   # any --model value the CLI accepts
    prices={},                              # {"claude-opus-5-5": 5.0}: USD per million input tokens, for resume estimates
    hosts=[],                               # extra Host values to accept, e.g. a reverse proxy name "box.example:443"
)


def load(path):
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Unknown setting or config is not an object")
    out = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if type(value) is not type(DEFAULTS[key]):
            raise ValueError(f"Invalid setting: {key}")
        out[key] = value
    if not 1 <= out["port"] <= 65535:
        raise ValueError("Invalid port")
    if out["interval"] < 30:
        raise ValueError("interval is at least 30 seconds")
    out["transcripts"] = str(Path(out["transcripts"]).expanduser())
    for kind, find in (("claude", find_claude), ("gemini", find_gemini)):
        # `<kind>` stays a string (the configured path when nothing is found); the Service re-probes on use
        out[f"{kind}_configured"] = out[kind]
        found, searched = find(out[kind])
        out[kind] = found or str(Path(out[kind]).expanduser())
        out[f"{kind}_searched"] = searched
    if not all(isinstance(h, str) and h for h in out["hosts"]):
        raise ValueError("hosts is a list of host:port strings")
    if not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
               for k, v in out["prices"].items()):
        raise ValueError("prices maps a model id to USD per million input tokens")
    return out


# a .cmd shim is fine for Gemini: Almanac starts it from gemini_host with fixed flags, and every prompt goes over stdin
GEMINI_NAMES = ("agy.exe", "agy.cmd", "gemini.exe", "gemini.cmd", "agy", "gemini")
CLAUDE_NAMES = ("claude.exe", "claude")
# never a Claude runner: cmd.exe re-parses a batch file's command line, so a prompt passed as an argument (the
# charter, a kickoff carrying an agent's task) is cut at its first newline and its `&` runs commands
SHIM_EXTS = (".cmd", ".bat")


def is_shim(path) -> bool:
    return str(path or "").strip().lower().endswith(SHIM_EXTS)


def _probe(configured, folders, names, path_names, *, which, refused=None) -> tuple[str | None, list[str]]:
    """The configured file, else the first of `names` in `folders`, else the first of `path_names` on PATH.
    Returns (path or None, every place looked, in order). With `refused` (a list), a .cmd / .bat hit is not
    returned but collected there."""
    searched: list[str] = []

    def usable(path) -> bool:
        if refused is not None and is_shim(path):
            refused.append(str(path))
            return False
        return True

    if configured:
        cand = Path(str(configured)).expanduser()
        searched.append(str(cand))
        if cand.is_file() and usable(cand):
            return str(cand), searched
    for folder in folders:
        if folder is None:
            continue
        searched.append(str(folder))
        for name in names:
            if (folder / name).is_file():
                return str(folder / name), searched
    searched.append(f"PATH ({', '.join(path_names)})")
    for name in path_names:
        hit = which(name)
        if hit and usable(hit):
            return hit, searched
    return None, searched


def _env_dir(env, key, *parts):
    base = (env or {}).get(key)
    return Path(base, *parts) if base else None


def find_gemini(configured, *, home=None, env=None, which=None) -> tuple[str | None, list[str]]:
    """Where the Gemini runner (`agy`, or the older `gemini`) is: the configured path, the installers' folders,
    then PATH. `home`, `env` and `which` default to this machine's; tests pass their own."""
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env
    folders = [
        home / ".gemini" / "bin",
        _env_dir(env, "LOCALAPPDATA", "agy", "bin"),
        home / ".gemini" / "antigravity" / "bin",
        _env_dir(env, "LOCALAPPDATA", "Programs", "antigravity", "bin"),
        _env_dir(env, "LOCALAPPDATA", "Programs", "antigravity", "resources", "bin"),
        _env_dir(env, "LOCALAPPDATA", "Programs", "antigravity", "resources", "app", "bin"),
        _env_dir(env, "ProgramFiles", "Antigravity", "bin"),
        _env_dir(env, "ProgramFiles", "Antigravity", "resources", "bin"),
        _env_dir(env, "ProgramFiles", "agy", "bin"),
    ]
    return _probe(configured, folders, GEMINI_NAMES, ("agy", "gemini"), which=which or shutil.which)


def find_claude(configured, *, home=None, env=None, which=None, shims=None) -> tuple[str | None, list[str]]:
    """Where the Claude Code CLI is: the configured path, `~/.local/bin`, then PATH. Never a .cmd / .bat (npm's
    shim): those are left out and, with `shims` (a list), collected there so the caller can say why."""
    home = Path(home) if home is not None else Path.home()
    return _probe(configured, [home / ".local" / "bin"], CLAUDE_NAMES, ("claude",), which=which or shutil.which,
                  refused=shims if shims is not None else [])
