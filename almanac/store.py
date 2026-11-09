"""Almanac's own record of every Claude Code session on this PC, kept after Claude Code prunes the transcripts.

Ingest reads `~/.claude/projects/**/*.jsonl` from where it last stopped (each file is append-only; a file that
shrank is read again from the top, and every row is keyed so a re-read is a no-op). From each transcript it keeps:

* `calls`  — one row per tool call: tool, time, for a shell call its family, leading word, length and adoption
  scores, and once the result arrives its size (chars of text read back) and the wait in seconds;
* `usage`  — one row per assistant message: model and token counts (input, cache creation, cache read, output);
* `sessions` — first/last time, project folder, sidechain or not.

`seed_legacy` loads an inventory's call list (tool, command, time; no sizes or waits) so the weeks
before Almanac existed stay on the chart after their transcripts are gone. Live rows win: both share a natural
key of (timestamp, tool, command).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from almanac import classify

SCHEMA = """
create table if not exists files (path text primary key, size integer, mtime_ns integer, offset integer);
create table if not exists sessions (id text primary key, project text, first_ts real, last_ts real,
                                     sidechain integer default 0, version text);
create table if not exists calls (natural text primary key, tool_use_id text unique, session text, ts real,
                                  tool text, family text, head text, cmd_len integer,
                                  search integer default 0, tests integer default 0, http integer default 0,
                                  edit integer default 0, out_chars integer, seconds real, is_error integer,
                                  source text default 'live');
create index if not exists calls_ts on calls(ts);
create table if not exists usage (message_id text primary key, session text, ts real, model text,
                                  input integer, cache_create integer, cache_read integer, output integer);
create index if not exists usage_ts on usage(ts);
create table if not exists meta (key text primary key, value text);
create table if not exists actions (id integer primary key autoincrement, ts real, action text, pid integer,
                                    session text, name text, ok integer, detail text, who text);
"""
SHELL = ("Bash", "PowerShell")


def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def natural_key(ts: str | None, tool: str, cmd: str | None) -> str:
    return hashlib.sha1(f"{ts}|{tool}|{cmd or ''}".encode("utf-8", "replace")).hexdigest()


IMAGE_CHARS = 6000   # an image costs ~1,600 tokens however large its base64 is; ~4 chars a token


def result_chars(content) -> int:
    """Characters of text a tool result put back into context; an image counts as IMAGE_CHARS."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content or []:
        if isinstance(part, dict):
            if part.get("type") == "text":
                total += len(part.get("text") or "")
            elif part.get("type") == "image":
                total += IMAGE_CHARS
    return total


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute("pragma journal_mode=wal")
        self.last_ingest: dict = {}
        self._migrate()

    def _migrate(self):
        """v2 (2026-11-08): sessions learn title, cwd, last model and context size. Existing rows only get them
        from a full re-read, so v2 forgets the read offsets once; every row is keyed, so the re-read is a no-op
        for calls and usage."""
        have = {r[1] for r in self.db.execute("pragma table_info(sessions)")}
        for column, kind in (("title", "text"), ("first_prompt", "text"), ("cwd", "text"), ("model", "text"),
                             ("context", "integer"), ("messages", "integer default 0"), ("custom_title", "text"),
                             ("name", "text"), ("name_usd", "real"), ("named_at", "real"), ("name_error", "text")):
            if column not in have:
                self.db.execute(f"alter table sessions add column {column} {kind}")
        row = self.db.execute("select value from meta where key = 'schema'").fetchone()
        if not row or int(row[0]) < 3:       # v3: custom titles (the user's /rename, or ours) are read too
            self.db.execute("delete from files")
            self.db.execute("insert or replace into meta values ('schema', '3')")
        self.db.commit()

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ ingest

    def ingest(self, root: Path) -> dict:
        """Read every transcript under `root` from where the last pass stopped. Returns what changed."""
        started = time.monotonic()
        root = Path(root).expanduser()
        stats = dict(files=0, read=0, calls=0, results=0, messages=0, bytes=0)
        if not root.is_dir():
            self.last_ingest = dict(stats, error=f"{root} is not a folder", at=time.time())
            return self.last_ingest
        with self.lock:
            known = {p: (s, m, o) for p, s, m, o in self.db.execute("select path, size, mtime_ns, offset from files")}
            for path in sorted(root.rglob("*.jsonl")):
                stats["files"] += 1
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                size, mtime, offset = known.get(key, (0, 0, 0))
                if st.st_size == size and st.st_mtime_ns == mtime:
                    continue
                if st.st_size < offset:
                    offset = 0          # rewritten or truncated: read it again; keys keep this idempotent
                new_offset = self._read_file(path, root, offset, stats)
                self.db.execute("insert or replace into files values (?, ?, ?, ?)", (key, st.st_size, st.st_mtime_ns, new_offset))
                stats["read"] += 1
            self.db.commit()
        self.last_ingest = dict(stats, at=time.time(), seconds=round(time.monotonic() - started, 2))
        self.db.execute("insert or replace into meta values ('last_ingest', ?)", (json.dumps(self.last_ingest),))
        self.db.commit()
        return self.last_ingest

    def _read_file(self, path: Path, root: Path, offset: int, stats: dict) -> int:
        project = path.relative_to(root).parts[0]
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return offset
        stats["bytes"] += end + 1
        for raw in data[:end + 1].splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self._record(rec, project, stats)
        return offset + end + 1

    def _record(self, rec: dict, project: str, stats: dict):
        kind = rec.get("type")
        session = rec.get("sessionId") or ""
        if kind == "custom-title" and session and rec.get("customTitle"):
            self.db.execute("insert into sessions (id, project, custom_title) values (?, ?, ?) "
                            "on conflict(id) do update set custom_title = excluded.custom_title",
                            (session, project, str(rec["customTitle"])[:200]))
            return
        if kind == "ai-title" and session and rec.get("aiTitle"):
            self.db.execute("insert into sessions (id, project, title) values (?, ?, ?) "
                            "on conflict(id) do update set title = excluded.title", (session, project, str(rec["aiTitle"])[:200]))
            return
        if kind not in ("assistant", "user"):
            return
        ts_raw = rec.get("timestamp")
        ts = parse_ts(ts_raw)
        main = not rec.get("isSidechain")
        if session and ts:
            self.db.execute(
                "insert into sessions (id, project, first_ts, last_ts, sidechain, version, cwd) values (?, ?, ?, ?, ?, ?, ?) "
                "on conflict(id) do update set first_ts = min(coalesce(first_ts, excluded.first_ts), excluded.first_ts), "
                "last_ts = max(coalesce(last_ts, excluded.last_ts), excluded.last_ts), version = coalesce(excluded.version, version), "
                "cwd = coalesce(excluded.cwd, cwd)",
                (session, project, ts, ts, int(not main), rec.get("version"), rec.get("cwd") if main else None))
        msg = rec.get("message") or {}
        content = msg.get("content")
        if main and session and kind == "user":
            text = content if isinstance(content, str) else next(
                (b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"), None)
            if text and not str(text).startswith("<"):
                self.db.execute("update sessions set first_prompt = coalesce(first_prompt, ?) where id = ?",
                                (" ".join(str(text).split())[:200], session))
        if kind == "assistant":
            usage = msg.get("usage")
            if main and session and usage and msg.get("model") not in (None, "<synthetic>"):
                self.db.execute("update sessions set model = ?, context = ? where id = ?", (
                    msg.get("model"), int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0), session))
            if usage and msg.get("id"):
                self.db.execute("insert or replace into usage values (?, ?, ?, ?, ?, ?, ?, ?)", (
                    msg["id"], session, ts, msg.get("model"), int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0),
                    int(usage.get("output_tokens") or 0)))
                stats["messages"] += 1
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool = block.get("name") or "?"
                cmd = (block.get("input") or {}).get("command") if tool in SHELL else None
                self._call(ts_raw, ts, session, tool, block.get("id"), cmd)
                stats["calls"] += 1
        else:
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result" or not block.get("tool_use_id"):
                    continue
                self.db.execute(
                    "update calls set out_chars = ?, is_error = ?, seconds = case when ts is not null and ? is not null "
                    "then max(0, ? - ts) end where tool_use_id = ?",
                    (result_chars(block.get("content")), int(bool(block.get("is_error"))), ts, ts, block["tool_use_id"]))
                stats["results"] += 1

    def _call(self, ts_raw, ts, session, tool, tool_use_id, cmd, source="live"):
        row = dict(natural=natural_key(ts_raw, tool, cmd), tool_use_id=tool_use_id, session=session, ts=ts, tool=tool,
                   family=None, head=None, cmd_len=None, search=0, tests=0, http=0, edit=0, source=source)
        if cmd is not None:
            row.update(family=classify.family(cmd), head=classify.head(cmd), cmd_len=len(cmd), **classify.adoption(cmd))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        if source == "live":
            # a legacy row for the same call steps aside for the live one, which has sizes and waits
            self.db.execute("delete from calls where natural = ? and source = 'legacy'", (row["natural"],))
            self.db.execute(f"insert or ignore into calls ({cols}) values ({marks})", tuple(row.values()))
        else:
            self.db.execute(f"insert or ignore into calls ({cols}) values ({marks})", tuple(row.values()))

    def seed_legacy(self, path: Path) -> int:
        """Load the inventory's `tooluses.jsonl` ({tool, cmd, proj, ts}). Returns rows added."""
        added = 0
        with self.lock:
            before = self.db.execute("select count(*) from calls").fetchone()[0]
            for raw in Path(path).read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                self._call(r.get("ts"), parse_ts(r.get("ts")), f"legacy:{r.get('proj', '')}", r.get("tool") or "?",
                           None, r.get("cmd"), source="legacy")
            self.db.commit()
            added = self.db.execute("select count(*) from calls").fetchone()[0] - before
        return added

    # ------------------------------------------------------------------ report

    def report(self, days: int = 30, bucket: str = "day", now: float | None = None) -> dict:
        """Everything the dashboard draws: tiles (this period vs the one before), a series per bucket,
        the family table and the tool table. `days` 0 means everything."""
        if bucket not in ("day", "week"):
            raise ValueError("bucket is day or week")
        now = now or time.time()
        with self.lock:
            first = self.db.execute("select min(ts) from calls").fetchone()[0]
            span = days * 86400 if days > 0 else max(86400, now - (first or now) + 86400)
            start, prev = now - span, now - 2 * span
            calls = self.db.execute("select ts, tool, family, search, tests, http, edit, out_chars, seconds, session, source "
                                    "from calls where ts >= ? order by ts", (prev,)).fetchall()
            usage = self.db.execute("select ts, input, cache_create, cache_read, output, session from usage where ts >= ?",
                                    (prev,)).fetchall()
            totals = self.db.execute("select count(*), sum(source = 'legacy'), count(distinct session) from calls").fetchone()
            last = self.db.execute("select value from meta where key = 'last_ingest'").fetchone()
        cur_calls = [c for c in calls if c[0] >= start]
        old_calls = [c for c in calls if c[0] < start]
        cur_usage = [u for u in usage if u[0] >= start]
        old_usage = [u for u in usage if u[0] < start]
        return {
            "range": dict(days=days, bucket=bucket, start=start, end=now),
            "coverage": dict(first=first, calls=totals[0] or 0, legacy_calls=totals[1] or 0, sessions=totals[2] or 0),
            "last_ingest": json.loads(last[0]) if last else None,
            "tiles": tiles(cur_calls, old_calls, cur_usage, old_usage),
            "series": series(cur_calls, cur_usage, start, now, bucket),
            "families": family_table(cur_calls, old_calls),
            "tools": tool_table(cur_calls),
        }

    def log_action(self, action: str, pid: int, row: dict | None, ok: bool, detail: str, who: str = "") -> dict:
        entry = dict(ts=time.time(), action=action, pid=pid, session=(row or {}).get("session_id"),
                     name=(row or {}).get("name") or (row or {}).get("role"), ok=bool(ok), detail=detail[:500], who=who)
        with self.lock:
            self.db.execute("insert into actions (ts, action, pid, session, name, ok, detail, who) values (?, ?, ?, ?, ?, ?, ?, ?)",
                            (entry["ts"], action, pid, entry["session"], entry["name"], int(entry["ok"]), entry["detail"], who))
            self.db.commit()
        return entry

    def actions(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select ts, action, pid, session, name, ok, detail, who from actions order by id desc limit ?",
                                   (limit,)).fetchall()
        return [dict(ts=r[0], action=r[1], pid=r[2], session=r[3], name=r[4], ok=bool(r[5]), detail=r[6], who=r[7]) for r in rows]

    def session_usage(self, session: str) -> dict:
        with self.lock:
            row = self.db.execute("select count(*), sum(input), sum(cache_create), sum(cache_read), sum(output) "
                                  "from usage where session = ?", (session,)).fetchone()
            calls = self.db.execute("select count(*), sum(out_chars) from calls where session = ?", (session,)).fetchone()
        return dict(messages=row[0] or 0, input=row[1] or 0, cache_create=row[2] or 0, cache_read=row[3] or 0,
                    output=row[4] or 0, calls=calls[0] or 0, read_back_kb=round((calls[1] or 0) / 1000, 1))

    def chats(self, query: str = "", limit: int = 300) -> list[dict]:
        """Every main session Almanac has seen, newest first: title, folder, model, context, replies, output tokens."""
        like = f"%{query.strip()}%" if query.strip() else "%"
        with self.lock:
            rows = self.db.execute(
                "select s.id, s.project, s.title, s.first_prompt, s.cwd, s.model, s.context, s.first_ts, s.last_ts, s.version, "
                "count(u.message_id), coalesce(sum(u.output), 0), s.custom_title, s.name, s.name_error "
                "from sessions s left join usage u on u.session = s.id "
                "where s.id not like 'legacy:%' and s.first_ts is not null and s.cwd is not null "
                "and (coalesce(s.title, '') like ? or coalesce(s.first_prompt, '') like ? or coalesce(s.cwd, '') like ? or s.id like ? "
                "or coalesce(s.custom_title, '') like ? or coalesce(s.name, '') like ?) "
                "group by s.id order by s.last_ts desc limit ?", (like, like, like, like, like, like, limit)).fetchall()
        out = []
        for r in rows:
            title, source = next(((t, src) for t, src in ((r[12], "renamed"), (r[13], "haiku"), (r[2], "auto"), (r[3], "prompt")) if t),
                                 ("(untitled)", None))
            if r[12] and r[13] and r[12] == r[13]:
                source = "haiku"             # the custom title is the one we wrote back
            out.append(dict(id=r[0], project=r[1], title=title, title_source=source, auto_title=r[2], first_prompt=r[3],
                            cwd=r[4], model=r[5], context=r[6] or 0, first=r[7], last=r[8], version=r[9], replies=r[10],
                            output=r[11], custom_title=r[12], name=r[13], name_error=r[14]))
        return out

    def titles(self, ids: list[str]) -> dict[str, str]:
        """{session id: the name to show}: custom title (yours or ours) > Haiku name > Claude Code's auto title."""
        ids = [i for i in ids if i][:200]
        if not ids:
            return {}
        with self.lock:
            rows = self.db.execute(f"select id, custom_title, name, title from sessions where id in ({','.join('?' * len(ids))})",
                                   ids).fetchall()
        return {r[0]: r[1] or r[2] or r[3] for r in rows if r[1] or r[2] or r[3]}

    def unnamed(self, quiet_since: float, limit: int = 20) -> list[dict]:
        """Chats with neither a custom title nor a Haiku name that have been quiet since `quiet_since`, newest first;
        a chat whose naming failed is retried after a day."""
        with self.lock:
            rows = self.db.execute(
                "select id, project, cwd from sessions where id not like 'legacy:%' and cwd is not null and first_ts is not null "
                "and custom_title is null and name is null and last_ts < ? and (name_error is null or named_at < ?) "
                "order by last_ts desc limit ?", (quiet_since, time.time() - 86400, limit)).fetchall()
        return [dict(id=r[0], project=r[1], cwd=r[2]) for r in rows]

    def set_name(self, session: str, name: str | None, usd: float = 0.0, error: str | None = None) -> None:
        with self.lock:
            self.db.execute("update sessions set name = coalesce(?, name), name_usd = coalesce(name_usd, 0) + ?, "
                            "named_at = ?, name_error = ? where id = ?", (name, usd, time.time(), error, session))
            self.db.commit()

    def chat(self, session_id: str) -> dict | None:
        found = [c for c in self.chats(session_id, 5) if c["id"] == session_id]
        return found[0] if found else None

    def counts(self, since: float) -> dict:
        with self.lock:
            calls, sessions = self.db.execute("select count(*), count(distinct session) from calls where ts >= ?",
                                              (since,)).fetchone()
            chars = self.db.execute("select avg(out_chars) from calls where ts >= ? and out_chars is not null",
                                    (since,)).fetchone()[0]
        return dict(calls=calls or 0, sessions=sessions or 0, kb_per_call=round((chars or 0) / 1000, 2))


# ---------------------------------------------------------------------- report pieces (plain functions, testable)

def _ratio(a, b):
    return round(a / b, 3) if b else None


def _kb_per_call(rows):
    sized = [r[7] for r in rows if r[7] is not None]
    return _ratio(sum(sized) / 1000, len(sized))


def _ctx_per_out(usage):
    out = sum(u[4] for u in usage)
    return _ratio(sum(u[1] + u[2] + u[3] for u in usage), out)


def _shell_min_per_session(rows):
    shell = [r for r in rows if r[1] in SHELL and r[10] == "live"]
    waits = [r[8] for r in shell if r[8] is not None and r[8] < 3600]
    sessions = {r[9] for r in shell}
    return _ratio(sum(waits) / 60, len(sessions))


def _adoption(rows):
    """Share of the four habits done the new way: new / (new + old), counting each call once per pair."""
    new = old = 0
    for r in rows:
        for score in r[3:7]:
            new += bool(score & 2)
            old += bool(score & 1)
    return _ratio(new, new + old)


def _pair_share(rows, index):
    new = sum(bool(r[index] & 2) for r in rows)
    old = sum(bool(r[index] & 1) for r in rows)
    return _ratio(new, new + old) if new + old else None


def tiles(cur, old, cur_usage, old_usage) -> list[dict]:
    return [
        dict(id="kb_per_call", label="KB read back per tool call", value=_kb_per_call(cur), previous=_kb_per_call(old),
             unit="KB", better="down"),
        dict(id="ctx_per_out", label="Context tokens per output token", value=_ctx_per_out(cur_usage),
             previous=_ctx_per_out(old_usage), unit="×", better="down"),
        dict(id="shell_min", label="Shell wait per session", value=_shell_min_per_session(cur),
             previous=_shell_min_per_session(old), unit="min", better="down"),
        dict(id="adoption", label="Helper adoption", value=_adoption(cur), previous=_adoption(old), unit="%", better="up"),
    ]


def _bucket_key(ts: float, bucket: str) -> str:
    day = datetime.fromtimestamp(ts).date()
    if bucket == "week":
        day = day - timedelta(days=day.weekday())
    return day.isoformat()


def series(calls, usage, start, end, bucket) -> list[dict]:
    keys = []
    step = timedelta(days=7 if bucket == "week" else 1)
    day = datetime.fromisoformat(_bucket_key(start, bucket))
    last = datetime.fromisoformat(_bucket_key(end, bucket))
    while day <= last:
        keys.append(day.date().isoformat())
        day += step
    by_calls = {k: [] for k in keys}
    by_usage = {k: [] for k in keys}
    for c in calls:
        by_calls.setdefault(_bucket_key(c[0], bucket), []).append(c)
    for u in usage:
        by_usage.setdefault(_bucket_key(u[0], bucket), []).append(u)
    out = []
    for k in keys:
        rows, us = by_calls.get(k, []), by_usage.get(k, [])
        shell = [r for r in rows if r[1] in SHELL]
        families: dict[str, int] = {}
        for r in shell:
            families[r[2] or "other"] = families.get(r[2] or "other", 0) + 1
        out.append(dict(
            bucket=k, calls=len(rows), shell_calls=len(shell), sessions=len({r[9] for r in rows}),
            kb_per_call=_kb_per_call(rows), out_kb=round(sum(r[7] or 0 for r in rows) / 1000, 1),
            ctx_per_out=_ctx_per_out(us), output_tokens=sum(u[4] for u in us),
            context_tokens=sum(u[1] + u[2] + u[3] for u in us),
            shell_min=round(sum(r[8] for r in shell if r[8] is not None and r[8] < 3600) / 60, 1),
            families=families, adoption={p: _pair_share(rows, 3 + i) for i, p in enumerate(classify.PAIRS)},
            legacy=sum(r[10] == "legacy" for r in rows)))
    return out


def family_table(cur, old) -> list[dict]:
    table: dict[str, dict] = {}
    for r in cur:
        if r[1] not in SHELL:
            continue
        row = table.setdefault(r[2] or "other", dict(family=r[2] or "other", calls=0, previous=0, waits=[], chars=[]))
        row["calls"] += 1
        if r[8] is not None and r[8] < 3600:
            row["waits"].append(r[8])
        if r[7] is not None:
            row["chars"].append(r[7])
    for r in old:
        if r[1] in SHELL:
            table.setdefault(r[2] or "other", dict(family=r[2] or "other", calls=0, previous=0, waits=[], chars=[]))["previous"] += 1
    out = []
    for row in table.values():
        waits, chars = row.pop("waits"), row.pop("chars")
        row.update(median_s=round(statistics.median(waits), 1) if waits else None,
                   total_min=round(sum(waits) / 60, 1), out_kb=round(sum(chars) / 1000, 1),
                   kb_per_call=round(sum(chars) / 1000 / len(chars), 2) if chars else None)
        out.append(row)
    return sorted(out, key=lambda r: (-r["calls"], r["family"]))


def tool_table(cur) -> list[dict]:
    table: dict[str, dict] = {}
    for r in cur:
        row = table.setdefault(r[1], dict(tool=r[1], calls=0, out_kb=0.0))
        row["calls"] += 1
        row["out_kb"] += (r[7] or 0) / 1000
    for row in table.values():
        row["out_kb"] = round(row["out_kb"], 1)
    return sorted(table.values(), key=lambda r: -r["calls"])[:15]
