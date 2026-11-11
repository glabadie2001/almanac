"""Managed Gemini agent host (`python -m almanac.gemini_host`): wraps `agy.exe` in a native
Claude `uds-messaging` peer endpoint so Claude and Gemini agents orchestrate as equals.

* Registers `~/.claude/sessions/<pid>.json` + `<pid>.<sha256>.key` with `provider: "gemini"`,
  so Claude's built-in `SendMessage` discovers and messages Gemini agents by name out of the box.
* Listens on `\\\\.\\pipe\\LOCAL\\cc-msg-<hex>` (`almanac.uds.PeerServer`): inbound peer messages
  immediately wake an idle Gemini session into a new turn or queue cleanly while a turn is running.
* Streams `agy.exe -p --input-format stream-json --output-format stream-json` events in real time
  into `.almanac/gemini/projects/<proj>/<session_id>.jsonl` in Almanac's normalized transcript schema,
  making Gemini context streams live-readable in Fleet, Gallery, Chats, Telemetry, and `mesh_context_stream`.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from almanac import mcp_mesh, uds

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def project_slug(cwd: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in str(cwd))
    return cleaned.strip("-")[:80] or "workspace"


# agy has no --allowedTools; headless agy auto-denies any tool not under `permissions.allow`, and an agent that cannot
# call its own mesh tools cannot talk. The server id is `<plugin>_<server>` from spawner.stage_agy_mesh_bundle.
MESH_GRANTS = json.dumps({"permissions": {"allow": [f"mcp(almanac-mesh_almanac-mesh/{t['name']})" for t in mcp_mesh.MCP_TOOLS]}},
                         separators=(",", ":"))
MODE_FALLBACK = "is not available for this account"   # agy's warning when it ignores `--mode auto`


def grants(allowed_commands=()) -> str:
    """agy's `--settings`: the mesh tools, plus one `command(<line>)` allow rule per exact command line. agy matches
    those character for character (`command(git --version)` works, `command(git)` and `command(git *)` do not), and
    in its default mode, which is all this account gets, it denies every other shell command."""
    cmds = [str(c).strip() for c in allowed_commands or () if str(c).strip()]
    if not cmds:
        return MESH_GRANTS
    allow = json.loads(MESH_GRANTS)["permissions"]["allow"] + [f"command({c})" for c in cmds]
    return json.dumps({"permissions": {"allow": allow}}, separators=(",", ":"))


def map_agy_permission_flags(permission_mode: str) -> list[str]:
    if permission_mode == "bypass":
        return ["--dangerously-skip-permissions"]
    if permission_mode in ("accept-edits", "auto", "plan"):
        return ["--mode", permission_mode]
    return []


_AGY_EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<eff>low|med|medium|high|xhigh|max)$", re.IGNORECASE)
_AGY_EFFORT_MAP = {"low": "low", "med": "medium", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
_AGY_TIERED_FAMILIES = frozenset({
    "gemini-4-carbon",
    "gemini-4-argon",
    "gemini-3.8-flash",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
})


def split_agy_model_effort(model: str | None, effort: str | None = None) -> tuple[str | None, str | None]:
    """Separate base model and effort ('gemini-4-carbon-high' -> ('gemini-4-carbon', 'high'))."""
    raw_m = str(model or "").strip()
    emb_e: str | None = None
    if raw_m:
        m = _AGY_EFFORT_SUFFIX_RE.match(raw_m)
        if m:
            emb_e = _AGY_EFFORT_MAP.get(m.group("eff").lower())
            raw_m = m.group("base").strip()
    norm_e = _AGY_EFFORT_MAP.get(str(effort or "").strip().lower()) or emb_e
    return (raw_m or None), norm_e


def resolve_agy_wire_model(base_model: str | None, effort: str | None) -> tuple[str | None, str | None]:
    """Compose the exact `--model` and `--effort` flags expected by `agy.exe`."""
    base, eff = split_agy_model_effort(base_model, effort)
    if not base:
        return None, eff
    low = base.lower()
    if low == "gpt-oss-120b":
        return "gpt-oss-120b-medium", "medium"
    if low in _AGY_TIERED_FAMILIES or re.match(r"^gemini-\d+(?:\.\d+)?-(?:carbon|argon|flash)$", low):
        tier = eff if eff in ("low", "medium", "high") else ("high" if eff in (None, "xhigh", "max") else "high")
        return f"{base}-{tier}", tier
    return base, eff


def parse_agy_stream_event(event: dict) -> dict:
    """Normalize one NDJSON event emitted by `agy -p= --output-format stream-json` (nested or flat)."""
    etype = str(event.get("event") or event.get("type") or "").lower()
    if etype == "init":
        inner = event.get("init") if isinstance(event.get("init"), dict) else {}
        return dict(
            kind="init",
            conversation_id=(
                event.get("conversation_id")
                or event.get("conversationId")
                or inner.get("conversation_id")
                or event.get("session_id")
            ),
            model=(
                event.get("model")
                or event.get("model_name")
                or inner.get("model")
                or inner.get("model_name")
            ),
        )
    if etype == "step_update":
        su = event.get("step_update") if isinstance(event.get("step_update"), dict) else event
        step_type = str(su.get("step_type") or su.get("stepType") or "").lower()
        state = str(su.get("state") or "").upper()
        step_idx = su.get("step_index")
        conv_id = su.get("conversation_id") or event.get("conversation_id")
        if step_type == "user_input":
            return dict(kind="other", conversation_id=conv_id)
        tinfo = su.get("tool_info") or su.get("toolInfo") or su.get("tool_call") or {}
        has_tool = (
            step_type == "tool"
            or bool(su.get("tool_name"))
            or (isinstance(tinfo, dict) and bool(tinfo.get("tool_name") or tinfo.get("name")))
            or ("tool" in step_type)
        )
        if has_tool:
            if not isinstance(tinfo, dict):
                tinfo = {}
            tname = su.get("tool_name") or tinfo.get("tool_name") or tinfo.get("name") or step_type or "tool"
            params = tinfo.get("parameters") or tinfo.get("args") or tinfo.get("input") or {}
            output = (
                tinfo.get("output")
                if "output" in tinfo
                else (su.get("output") if "output" in su else tinfo.get("result"))
            )
            err_val = tinfo.get("error") or su.get("error")
            is_err = bool(tinfo.get("is_error") or err_val or state == "ERROR")
            det_id = (
                tinfo.get("id")
                or (
                    f"tu_{(str(conv_id)[:8] + '_') if conv_id else ''}{step_idx}"
                    if step_idx is not None
                    else f"tu_{secrets.token_hex(6)}"
                )
            )
            if state == "DONE" and output is None and not err_val:
                dur = su.get("duration_seconds")
                out_str = f"completed in {dur:.2f}s" if isinstance(dur, (int, float)) else "completed"
            elif output is not None or err_val:
                out_str = str(err_val or output)
            else:
                out_str = None
            return dict(
                kind="tool",
                phase="done" if state == "DONE" else ("start" if state == "ACTIVE" else "both"),
                conversation_id=conv_id,
                tool_id=str(det_id),
                tool_name=str(tname),
                tool_input=params if isinstance(params, dict) else {"description": str(params)},
                tool_output=out_str,
                is_error=is_err,
            )
        has_delta = any(k in su for k in ("text_delta", "delta"))
        text = su.get("text_delta") if "text_delta" in su else (su.get("delta") or su.get("text") or su.get("content") or "")
        if isinstance(text, dict):
            text = text.get("text") or ""
        usage_su = su.get("usage") if isinstance(su.get("usage"), dict) else {}
        return dict(
            kind="text",
            conversation_id=conv_id,
            text=str(text),
            is_delta=has_delta,
            model=su.get("model") or usage_su.get("model"),
        )
    if etype in ("assistant", "message"):
        msg = event.get("message") if isinstance(event.get("message"), dict) else event
        content = msg.get("content")
        if isinstance(content, str):
            return dict(kind="text", text=content, is_delta=False, model=msg.get("model"))
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            return dict(kind="text", text="".join(parts), is_delta=False, model=msg.get("model"))
    if etype == "result":
        res_obj = event.get("result") if isinstance(event.get("result"), dict) else {}
        usage_raw = (res_obj.get("usage") if res_obj else None) or event.get("usage") or {}
        if res_obj:
            text = res_obj.get("response") or res_obj.get("text") or res_obj.get("content") or ""
        else:
            text = event.get("result") or event.get("response") or event.get("text") or event.get("content") or ""
            if isinstance(text, dict):
                text = text.get("response") or text.get("text") or json.dumps(text)
        status_str = str(res_obj.get("status") or event.get("finished_reason") or event.get("finishedReason") or "")
        err_val = res_obj.get("error") or event.get("error")
        is_err = bool(
            event.get("is_error")
            or res_obj.get("is_error")
            or err_val
            or status_str.upper() in ("ERROR", "FAILED")
        )
        return dict(
            kind="result",
            conversation_id=res_obj.get("conversation_id") or event.get("conversation_id"),
            text=str(text),
            model=res_obj.get("model") or event.get("model") or usage_raw.get("model"),
            finished_reason=status_str or None,
            is_error=is_err,
            error=err_val,
            usage=dict(
                input_tokens=int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0),
                cache_creation_input_tokens=int(usage_raw.get("cache_creation_input_tokens") or 0),
                cache_read_input_tokens=int(usage_raw.get("cache_read_tokens") or usage_raw.get("cache_read_input_tokens") or 0),
                output_tokens=int(usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0),
            ) if usage_raw else None,             # a failed turn that reports no usage writes none, not zeros
        )
    return dict(kind="other")


def step_context(event: dict) -> int | None:
    """One step's own re-read (`input_tokens + cache_read_tokens`) from a `step_update` event's usage, else None.
    `result.usage` is the conversation's running total; each step's usage is that step's alone."""
    if str(event.get("event") or event.get("type") or "").lower() != "step_update":
        return None
    su = event.get("step_update") if isinstance(event.get("step_update"), dict) else event
    use = su.get("usage")
    if not isinstance(use, dict):
        return None
    try:
        return (int(use.get("input_tokens") or 0) + int(use.get("cache_read_tokens") or 0)) or None
    except (TypeError, ValueError):
        return None


RUNNER_EXITED = "runner_exited"     # agy's stdout closed before a `result`: the process died; not retried
RUNNER_ERROR = "runner_error"       # the turn raised (agy missing, a pipe that will not take the prompt); not retried


def turn_error(err, finished_reason=None) -> dict:
    """{error, text} for a failed agy turn. The class is agy's own code when the error carries one (`code`, `type`,
    `status`), else `gemini_error`; recover.py retries neither, since no saved transcript shows what agy reports."""
    code = None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or err.get("status")
        text = err.get("message") or err.get("text") or json.dumps(err)
    else:
        text = str(err or finished_reason or "agy reported an error")
    return dict(error=str(code) if code else "gemini_error", text=str(text)[:300])


class GeminiHost:
    """Owns one Gemini agent's `uds-messaging` pipe, turn queue, normalized transcript, and `agy` subprocess."""

    def __init__(
        self,
        *,
        name: str,
        cwd: str,
        transcript_path: Path,
        sessions_dir: Path,
        agy_bin: str = "agy",
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str = "auto",
        role_name: str | None = None,
        squad_id: str | None = None,
        channels: list[str] | None = None,
        system_prompt: str = "",
        add_dirs: list[str] | None = None,
        inbox_path: Path | None = None,
        in_memory_uds: bool = False,
        runner_fn=None,
        quiet: bool = False,
        allowed_commands: list[str] | None = None,
    ):
        self.name = name
        self.cwd = str(Path(cwd).resolve())
        self.transcript_path = Path(transcript_path)
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = Path(sessions_dir)
        self.agy_bin = agy_bin
        self.session_id = session_id or str(uuid.uuid4())
        self.conversation_id: str | None = None
        self.model, self.effort = split_agy_model_effort(model, effort)
        self.permission_mode = permission_mode
        self.role_name = role_name
        self.squad_id = squad_id
        self.channels = list(channels or [])
        self.system_prompt = system_prompt.strip()
        self.add_dirs = list(add_dirs or [])
        self.allowed_commands = [str(c) for c in allowed_commands or [] if str(c).strip()]
        self.mode_note: str | None = None
        self.quiet = quiet
        self._runner_fn = runner_fn
        self._turn_index = 0
        self._restart_after_turn = False
        self._tx_lock = threading.Lock()
        self.turns: "queue.Queue[dict | None]" = queue.Queue()
        self.stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.peer = uds.PeerServer(
            self.sessions_dir,
            name=self.name,
            session_id=self.session_id,
            cwd=self.cwd,
            provider="gemini",
            model=self.model or "gemini",
            effort=self.effort,
            role_name=self.role_name,
            squad_id=self.squad_id,
            channels=self.channels,
            transcript_path=str(self.transcript_path),
            inbox_path=inbox_path,
            on_message=self._on_peer_message,
            in_memory=in_memory_uds,
        )

    def _log(self, line: str) -> None:
        if not self.quiet:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except OSError:
                pass

    def _append_record(self, rec: dict) -> None:
        with self._tx_lock:
            with self.transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")

    def _write_title_once(self) -> None:
        if self._turn_index == 0:
            label = f"{self.name} ({self.role_name})" if self.role_name else self.name
            self._append_record({"type": "custom-title", "sessionId": self.session_id, "customTitle": label, "provider": "gemini"})

    def set_model(self, model: str | None, effort: str | None = None) -> None:
        next_m = (model or "").strip() or self.model
        next_e = (effort or "").strip() or (self.effort if effort is None else None)
        self.model, self.effort = split_agy_model_effort(next_m, next_e)
        self.peer.set_status(self.peer.status, model=self.model or "gemini", effort=self.effort)
        if self._proc is not None and self._proc.poll() is None:
            if self.peer.status == "idle":
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            else:
                self._restart_after_turn = True

    def _on_peer_message(self, msg: dict) -> None:
        if msg.get("channel") == "__almanac_control__":
            try:
                cmd = json.loads(msg.get("body") or "{}")
            except ValueError:
                cmd = {}
            if isinstance(cmd, dict) and cmd.get("op") == "set_model":
                self.set_model(cmd.get("model"), cmd.get("effort"))
                self._log(f"\n⚙ [model -> {self.model or 'default'} · effort -> {self.effort or 'default'}]")
            elif isinstance(cmd, dict) and cmd.get("op") == "compact":
                # door.context_boundary: agy's own /compact as a raw user turn, queued behind any turn in flight
                self._log("\n⚙ [compact]")
                self.turns.put({"kind": "compact", "raw_content": "/compact", "prompt_text": "/compact"})
            return
        wrapped = uds.format_cross_session(
            msg["body"],
            from_pipe=msg.get("from_pipe") or "peer",
            from_name=msg.get("from_name") or "peer",
            from_mode=msg.get("from_mode") or "prompting",
            channel=msg.get("channel"),
        )
        self._log(f"\n◂ [uds from {msg.get('from_name') or 'peer'}] {msg['body'][:240]}")
        self.turns.put({"kind": "peer", "raw_content": wrapped, "prompt_text": wrapped})

    def enqueue_prompt(self, text: str) -> None:
        if text and text.strip():
            self.turns.put({"kind": "prompt", "raw_content": text.strip(), "prompt_text": text.strip()})

    def _build_turn_prompt(self, prompt_text: str) -> str:
        if self._turn_index == 0 and self.system_prompt:
            return f"<system-role-instructions>\n{self.system_prompt}\n</system-role-instructions>\n\n{prompt_text}"
        return prompt_text

    def _ensure_stream_proc(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        argv = [self.agy_bin, "-p=", "--input-format", "stream-json", "--output-format", "stream-json"]
        wire_model, wire_effort = resolve_agy_wire_model(self.model, self.effort)
        if wire_model:
            argv += ["--model", wire_model]
        if wire_effort:
            argv += ["--effort", wire_effort]
        if self.conversation_id:
            argv += ["--conversation", self.conversation_id]
        argv += map_agy_permission_flags(self.permission_mode) + ["--settings", grants(self.allowed_commands)]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        env = dict(os.environ)
        env["ALMANAC_AGENT_NAME"] = self.name
        if self.squad_id:
            env["ALMANAC_SQUAD_ID"] = self.squad_id
        self._proc = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        # drain stderr (a full pipe would stall agy) and catch its mode fallback, so the roster can show it
        threading.Thread(target=self._read_stderr, args=(self._proc,), name=f"agy-stderr-{self.name}", daemon=True).start()
        return self._proc

    def _read_stderr(self, proc) -> None:
        stream = getattr(proc, "stderr", None)
        while stream is not None and not self.stop_event.is_set():
            try:
                line = stream.readline()
            except (OSError, ValueError):
                return
            if not isinstance(line, str) or not line:   # EOF (or not a text stream at all): stop
                return
            text = line.strip()
            if not text:
                continue
            self._log(f"  [agy] {text[:300]}")
            if MODE_FALLBACK in text and self.mode_note != text[:300]:
                self.note_mode(text[:300])

    def note_mode(self, note: str) -> None:
        """Publish agy's mode fallback in this host's session record (the roster's `mode_note`)."""
        self.mode_note = note
        self.peer.mode_note = note
        self.peer.set_status(self.peer.status)

    def _run_agy_turn(self, turn_prompt: str) -> tuple[str, list[dict], dict, str | None]:
        """Send one NDJSON user turn to `agy` and stream events until the terminal `result` event."""
        if self._runner_fn is not None:
            return self._runner_fn(turn_prompt, self)
        proc = self._ensure_stream_proc()
        assert proc.stdin is not None and proc.stdout is not None
        wire = json.dumps({"event": "user", "message": {"role": "user", "content": turn_prompt}}) + "\n"
        try:
            proc.stdin.write(wire)
            proc.stdin.flush()
        except OSError:
            self._proc = None
            proc = self._ensure_stream_proc()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(wire)
            proc.stdin.flush()

        self._turn_error = None
        self._step_context = None
        ended = False                   # a `result` event came: without one, agy's output closed mid-turn
        text_chunks: list[str] = []
        final_text = ""
        tool_events: list[dict] = []
        seen_tool_starts: set[str] = set()
        usage = None                    # agy's running totals for the conversation, when its result carries them
        seen_model = self.model

        while not self.stop_event.is_set():
            line = proc.stdout.readline()
            if not line:
                break
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                raw_ev = json.loads(trimmed)
            except ValueError:
                text_chunks.append(trimmed)
                continue
            if not isinstance(raw_ev, dict):
                continue
            ev = parse_agy_stream_event(raw_ev)
            self._step_context = step_context(raw_ev) or self._step_context      # the turn's newest step with usage
            if ev.get("conversation_id"):
                self.conversation_id = str(ev["conversation_id"])
            if ev["kind"] == "init":
                if ev.get("model"):
                    seen_model = str(ev["model"])
            elif ev["kind"] == "tool":
                tid = ev["tool_id"]
                if tid not in seen_tool_starts:
                    seen_tool_starts.add(tid)
                    tool_events.append(ev)
                    self._log(f"  ⚙ {ev['tool_name']}")
                    # Write live tool call into transcript immediately so peers tailing our context stream see it mid-turn
                    self._append_record(
                        {
                            "type": "assistant",
                            "provider": "gemini",
                            "sessionId": self.session_id,
                            "timestamp": iso_now(),
                            "cwd": self.cwd,
                            "effort": self.effort,
                            "message": {
                                "id": f"msg_{secrets.token_hex(8)}",
                                # the base id, as the turn-end usage record writes it: one BY MODEL row
                                "model": split_agy_model_effort(seen_model)[0] or "gemini",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": tid,
                                        "name": ev["tool_name"],
                                        "input": ev["tool_input"],
                                    }
                                ],
                            },
                        }
                    )
                if ev.get("phase") in ("done", "both") and ev.get("tool_output") is not None:
                    self._append_record(
                        {
                            "type": "user",
                            "provider": "gemini",
                            "sessionId": self.session_id,
                            "timestamp": iso_now(),
                            "cwd": self.cwd,
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tid,
                                        "is_error": ev.get("is_error", False),
                                        "content": ev["tool_output"],
                                    }
                                ]
                            },
                        }
                    )
            elif ev["kind"] == "text" and ev.get("text"):
                if ev.get("model"):
                    seen_model = str(ev["model"])
                if ev.get("is_delta"):
                    text_chunks.append(ev["text"])
                else:
                    final_text = ev["text"]
            elif ev["kind"] == "result":
                ended = True
                if ev.get("model"):
                    seen_model = str(ev["model"])
                if ev.get("is_error"):
                    self._turn_error = turn_error(ev.get("error"), ev.get("finished_reason"))
                if ev.get("text"):
                    final_text = ev["text"]
                elif self._turn_error:
                    final_text = f"[error] {self._turn_error['text']}"
                if ev.get("usage"):
                    usage = ev["usage"]
                break

        self._turn_cancelled = not ended and self.stop_event.is_set()
        if not ended and not self._turn_cancelled:
            self._turn_error = dict(error=RUNNER_EXITED, text="agy's output closed before the turn ended")
        reply = final_text.strip() or "".join(text_chunks).strip()
        return reply, tool_events, usage, seen_model

    def run_one_turn(self, item: dict) -> None:
        self._write_title_once()
        self.peer.set_status("busy")
        self._append_record(
            {
                "type": "user",
                "provider": "gemini",
                "sessionId": self.session_id,
                "timestamp": iso_now(),
                "cwd": self.cwd,
                "message": {"role": "user", "content": item["raw_content"]},
            }
        )
        compact = item.get("kind") == "compact"
        if compact:     # exactly `/compact`: no role wrap, and it is not a turn the system prompt still waits for
            full_prompt = item["prompt_text"]
        else:
            full_prompt = self._build_turn_prompt(item["prompt_text"])
            self._turn_index += 1
        self._turn_error, self._turn_cancelled = None, False
        try:
            reply, _, usage, seen_model = self._run_agy_turn(full_prompt)
            if seen_model:
                self.model, self.effort = split_agy_model_effort(seen_model, self.effort)
            if reply:
                self._log(f"\n✦ [{self.name}] {reply}\n")
            if self._turn_cancelled:
                return                                    # shutting down mid-turn: no reply, and no fake "(completed)"
            err = self._turn_error
            if err and not reply:
                reply = f"[error] {err['text']}"
            self._append_record(
                {
                    # a failed turn is marked the way Claude Code marks one, so fleet.read_tail sees the stall
                    **({"isApiErrorMessage": True, "error": err["error"]} if err else {}),
                    "type": "assistant",
                    "provider": "gemini",
                    "sessionId": self.session_id,
                    "timestamp": iso_now(),
                    "cwd": self.cwd,
                    "effort": self.effort,
                    "message": {
                        "id": f"msg_{secrets.token_hex(8)}",
                        "model": self.model or "gemini",
                        # agy answers /compact with a checkpoint step and an empty SUCCESS result: not a failure
                        "content": [{"type": "text", "text": reply or ("(compacted)" if compact else "(completed)")}],
                        "usage": usage,
                    },
                    # one step's re-read, beside the running totals in `usage` (which store._gemini_turn needs)
                    **({"context_tokens": self._step_context} if getattr(self, "_step_context", None) else {}),
                }
            )
        except Exception as exc:
            err_msg = f"Gemini runner error: {exc}"
            if isinstance(exc, FileNotFoundError):
                err_msg = (f"Gemini runner not found at {self.agy_bin}: install agy or set \"gemini\" in Almanac's "
                           f"config.json, then relaunch this agent ({exc})")
            self._log(f"\n✕ {err_msg}\n")
            # the same record a failed turn leaves, so a runner that cannot start is seen as stalled and escalated
            self._append_record(
                {
                    "isApiErrorMessage": True,
                    "error": RUNNER_ERROR,
                    "type": "assistant",
                    "provider": "gemini",
                    "sessionId": self.session_id,
                    "timestamp": iso_now(),
                    "cwd": self.cwd,
                    "message": {"id": f"msg_{secrets.token_hex(8)}", "model": "<synthetic>",
                                "content": [{"type": "text", "text": err_msg}]},
                }
            )
        finally:
            if self._restart_after_turn and self._proc is not None:
                self._restart_after_turn = False
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            self.peer.set_status("idle", model=self.model, effort=self.effort)

    def start(self) -> "GeminiHost":
        self.peer.start()
        self._log(
            f"═══ ALMANAC GEMINI AGENT // {self.name} "
            f"[{self.role_name or 'session'}] pid={os.getpid()} pipe={self.peer.pipe_path} ═══"
        )
        return self

    def serve_forever(self, *, interactive_stdin: bool = False) -> None:
        self.start()
        if interactive_stdin:
            def _read_console():
                for line in sys.stdin:
                    if self.stop_event.is_set():
                        break
                    if line.strip():
                        self.enqueue_prompt(line.strip())
            threading.Thread(target=_read_console, name="gemini-stdin", daemon=True).start()
        try:
            while not self.stop_event.is_set():
                try:
                    item = self.turns.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                self.run_one_turn(item)
        finally:
            self.close()

    def close(self) -> None:
        self.stop_event.set()
        self.turns.put(None)
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self.peer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Almanac Gemini uds-messaging agent host")
    parser.add_argument("--name", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--sessions-dir", type=Path, default=Path.home() / ".claude" / "sessions")
    parser.add_argument("--agy", default=str(Path.home() / ".gemini" / "bin" / "agy.exe"))
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--permission-mode", default="auto")
    parser.add_argument("--role", dest="role_name", default=None)
    parser.add_argument("--squad", dest="squad_id", default=None)
    parser.add_argument("--channel", action="append", dest="channels", default=[])
    parser.add_argument("--system-prompt-file", type=Path, default=None)
    parser.add_argument("--initial-prompt-file", type=Path, default=None)
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--add-dir", action="append", dest="add_dirs", default=[])
    parser.add_argument("--allow-command", action="append", dest="allowed_commands", default=[])
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)

    try:
        sys_prompt = args.system_prompt_file.read_text(encoding="utf-8") if args.system_prompt_file and args.system_prompt_file.exists() else ""
        init_prompt = args.initial_prompt_file.read_text(encoding="utf-8") if args.initial_prompt_file and args.initial_prompt_file.exists() else ""

        host = GeminiHost(
            name=args.name,
            cwd=args.cwd,
            transcript_path=args.transcript,
            sessions_dir=args.sessions_dir,
            agy_bin=args.agy,
            session_id=args.session_id,
            model=args.model,
            effort=args.effort,
            permission_mode=args.permission_mode,
            role_name=args.role_name,
            squad_id=args.squad_id,
            channels=args.channels,
            system_prompt=sys_prompt,
            add_dirs=args.add_dirs,
            allowed_commands=args.allowed_commands,
            inbox_path=args.inbox,
            quiet=args.headless,
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: host.close())
            except (ValueError, OSError):
                pass
        if init_prompt.strip():
            host.enqueue_prompt(init_prompt)
        host.serve_forever(interactive_stdin=not args.headless)
        return 0
    except Exception as exc:
        if args.log_file:
            try:
                args.log_file.parent.mkdir(parents=True, exist_ok=True)
                args.log_file.write_text(f"[ERROR] {type(exc).__name__}: {exc}\n", encoding="utf-8")
            except OSError:
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())

