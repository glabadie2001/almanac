"""Name Claude Code chats with Haiku, through `claude -p`, and write the name back where Claude Code shows it.

`digest(path)` boils a transcript down to what a name needs (folder, Claude's own auto-title, the opening and the
latest requests, the last replies), a few KB however long the chat ran. `Namer.name(digest)` asks Haiku for a
3–7 word name. `write_back(path, session_id, name)` appends the same two records Claude Code's own `/rename`
writes (`custom-title` and `agent-name`), so the resume picker and Remote Control show it too. It never
overwrites a name: a transcript that already has a `custom-title` (the user's, or an earlier one of ours) is
left alone, and a live session's transcript is never touched (Claude Code is still appending to it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

MODEL = "haiku"
SYSTEM = ("You name Claude Code chat sessions for a list their owner scans to find and resume work. "
          "Reply with the name only: 3 to 7 words, Title Case, no quotes, no trailing punctuation. "
          "Name the concrete work and its project (for example \"Billing Service Retry Backoff\" or "
          "\"Almanac Resume Button Fix\"), not the process (never \"Coding Session\" or \"Help With Code\").")


def _text(content) -> str | None:
    if isinstance(content, str):
        return content
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            return block["text"]
    return None


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def digest(path: Path) -> dict:
    """{cwd, auto_title, custom_title, first: [...], last: [...], replies: [...]} from one pass over the file."""
    out = dict(cwd=None, auto_title=None, custom_title=None, first=[], last=[], replies=[])
    prompts: list[str] = []
    replies: list[str] = []
    with Path(path).open("rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "ai-title" and rec.get("aiTitle"):
                out["auto_title"] = str(rec["aiTitle"])
            elif kind == "custom-title" and rec.get("customTitle"):
                out["custom_title"] = str(rec["customTitle"])
            if rec.get("isSidechain"):
                continue
            if rec.get("cwd") and not out["cwd"]:
                out["cwd"] = rec["cwd"]
            text = _text((rec.get("message") or {}).get("content"))
            if not text or text.startswith("<"):
                continue
            if kind == "user":
                prompts.append(_clip(text, 400))
            elif kind == "assistant":
                replies.append(_clip(text, 300))
    out["first"] = prompts[:4]
    out["last"] = [p for p in prompts[-4:] if p not in prompts[:4]]
    out["replies"] = replies[-2:]
    return out


def prompt(d: dict) -> str:
    parts = ["Name this Claude Code chat from the summary below. Reply with the name only.",
             f"Folder: {d.get('cwd') or 'unknown'}"]
    if d.get("auto_title"):
        parts.append(f"Claude Code's own short title: {d['auto_title']}")
    parts.append("Opening requests:\n" + "\n".join(f"- {p}" for p in d.get("first") or ["(none)"]))
    if d.get("last"):
        parts.append("Latest requests:\n" + "\n".join(f"- {p}" for p in d["last"]))
    if d.get("replies"):
        parts.append("Claude's last replies:\n" + "\n".join(f"- {r}" for r in d["replies"]))
    return "\n\n".join(parts)[:6000]


def clean(reply: str) -> str:
    line = next((l for l in str(reply or "").splitlines() if l.strip()), "")
    line = re.sub(r"^(name|title)\s*:\s*", "", line.strip(), flags=re.I).strip(" \"'`*.#")
    return _clip(line, 70)


class ClaudeCLI:
    """`.chat(model, messages)` -> {content, usd} through the local `claude -p`, on whatever login that CLI has.
    No tools, no hooks, no MCP, no CLAUDE.md, no transcript: run from an empty folder with only `local` settings,
    the call costs ~1k tokens (measured 2026-11-09: $0.0002 on Haiku) and leaves nothing behind for Almanac to
    read or name. (`--bare` would be leaner still but accepts only an API key, never a claude.ai login.)"""

    def __init__(self, claude: str = "claude", timeout: float = 120, run=None, workdir: Path | None = None,
                 budget_usd: float = 0.05):
        self.claude, self.timeout, self.run = claude, timeout, run or subprocess.run
        self.workdir, self.budget_usd = workdir, budget_usd

    def chat(self, model, messages, **_options):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        argv = [self.claude, "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
                "--tools", "", "--setting-sources", "local", "--strict-mcp-config",
                "--settings", json.dumps({"disableAllHooks": True}), "--max-budget-usd", str(self.budget_usd),
                "--system-prompt", system]
        env = {k: v for k, v in os.environ.items()
               if not (k == "CLAUDECODE" or k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_")))}
        cwd = None
        if self.workdir is not None:
            Path(self.workdir).mkdir(parents=True, exist_ok=True)
            cwd = str(self.workdir)
        done = self.run(argv, input=user.encode("utf-8"), capture_output=True, timeout=self.timeout, env=env, cwd=cwd,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (done.stdout or b"").decode("utf-8", "replace")
        if done.returncode != 0:
            err = (done.stderr or b"").decode("utf-8", "replace").strip() or out.strip()
            raise RuntimeError(f"claude -p exited {done.returncode}: {err[:300]}")
        doc = json.loads(out)
        if doc.get("is_error"):
            raise RuntimeError(f"claude -p: {str(doc.get('result'))[:300]}")
        return {"content": doc.get("result") or "", "usd": float(doc.get("total_cost_usd") or 0.0)}


class Namer:
    """`name(digest)` -> (name, usd). `client` is anything with `.chat(model, messages, **options)`."""

    def __init__(self, client=None, model: str = MODEL, claude: str = "claude", workdir: Path | None = None):
        self._client, self.model, self.claude, self.workdir = client, model, claude, workdir

    @property
    def client(self):
        if self._client is None:
            self._client = ClaudeCLI(self.claude, workdir=self.workdir)
        return self._client

    def name(self, d: dict) -> tuple[str, float]:
        if not d.get("first") and not d.get("auto_title"):
            raise ValueError("nothing in this chat to name it by")
        reply = self.client.chat(self.model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(d)}],
                                 max_tokens=300, temperature=0.2)
        name = clean((reply or {}).get("content"))
        if len(name) < 3:
            raise ValueError(f"Haiku gave no usable name: {reply!r}"[:200])
        return name, float((reply or {}).get("usd") or 0.0)


def write_back(path: Path, session_id: str, name: str) -> bool:
    """Append Claude Code's rename records unless the transcript already carries a custom title. True if written."""
    path = Path(path)
    if digest(path).get("custom_title"):
        return False
    with path.open("rb") as fh:
        fh.seek(0, 2)
        ends_clean = fh.tell() == 0 or (fh.seek(-1, 2) is not None and fh.read(1) == b"\n")
    with path.open("ab") as fh:
        if not ends_clean:
            fh.write(b"\n")         # never fuse our record onto a half-written last line
        for rec in ({"type": "custom-title", "customTitle": name, "sessionId": session_id},
                    {"type": "agent-name", "agentName": name, "sessionId": session_id}):
            # Claude Code's own compact form, so a search for its records finds ours too
            fh.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return True
