"""Settings (no secrets; the door token lives in <state>/token)."""
import copy
import json
import shutil
from pathlib import Path

DEFAULTS = dict(
    port=8812,                              # 127.0.0.1:<port>
    transcripts="~/.claude/projects",       # Claude Code's transcript folder
    interval=300,                           # seconds between transcript reads (>= 30)
    claude="~/.local/bin/claude.exe",       # the CLI that resumes chats and names them
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
    claude = Path(out["claude"]).expanduser()
    out["claude"] = str(claude) if claude.exists() else (shutil.which("claude") or str(claude))
    if not all(isinstance(h, str) and h for h in out["hosts"]):
        raise ValueError("hosts is a list of host:port strings")
    if not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
               for k, v in out["prices"].items()):
        raise ValueError("prices maps a model id to USD per million input tokens")
    return out
