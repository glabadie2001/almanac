"""Almanac's own copy of every Claude Code transcript, kept whatever Claude Code later deletes.

`sync(source, dest)` mirrors `~/.claude/projects` into `.almanac/archive/projects`: every file (main transcripts,
subagent transcripts, saved tool outputs). A transcript only ever grows, so a grown `.jsonl` gets just its new
bytes appended; a file that shrank or any other changed file is copied whole (the old copy kept beside it as
`.prev`). Nothing is ever deleted from the archive: a transcript Claude Code prunes stays here.

`restore(session_id, dest, source)` puts a session's files back under `~/.claude/projects` (only what is missing
or shorter there), so `claude --resume <id>` finds it again.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    st = src.stat()
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def sync(source: Path, dest: Path) -> dict:
    """Bring the archive up to date with `source`. Returns counts and bytes written."""
    source, dest = Path(source).expanduser(), Path(dest)
    stats = dict(files=0, new=0, appended=0, replaced=0, bytes=0, errors=0, seconds=0.0)
    started = time.monotonic()
    if not source.is_dir():
        return dict(stats, error=f"{source} is not a folder")
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            src = Path(dirpath) / name
            dst = dest / src.relative_to(source)
            stats["files"] += 1
            try:
                s = src.stat()
                try:
                    d = dst.stat()
                except FileNotFoundError:
                    _copy(src, dst)
                    stats["new"] += 1
                    stats["bytes"] += s.st_size
                    continue
                if s.st_size == d.st_size and s.st_mtime_ns == d.st_mtime_ns:
                    continue
                if name.endswith(".jsonl") and s.st_size > d.st_size:
                    with src.open("rb") as fh:
                        fh.seek(d.st_size)
                        extra = fh.read(s.st_size - d.st_size)
                    with dst.open("ab") as out:
                        out.write(extra)
                    os.utime(dst, ns=(s.st_atime_ns, s.st_mtime_ns))
                    stats["appended"] += 1
                    stats["bytes"] += len(extra)
                    continue
                if s.st_size == d.st_size and src.read_bytes() == dst.read_bytes():
                    os.utime(dst, ns=(s.st_atime_ns, s.st_mtime_ns))
                    continue
                shutil.copyfile(dst, dst.with_name(dst.name + ".prev"))
                _copy(src, dst)
                stats["replaced"] += 1
                stats["bytes"] += s.st_size
            except OSError:
                stats["errors"] += 1
    stats["seconds"] = round(time.monotonic() - started, 2)
    return stats


def find(session_id: str, dest: Path) -> Path | None:
    """The archived main transcript of a session: `<archive>/<project folder>/<id>.jsonl`."""
    for path in Path(dest).glob(f"*/{session_id}.jsonl"):
        return path
    return None


def restore(session_id: str, dest: Path, source: Path) -> dict:
    """Copy a session's archived files back into `source` where they are missing or shorter. Returns what moved."""
    main = find(session_id, dest)
    if main is None:
        raise FileNotFoundError(f"no archived transcript for session {session_id}")
    project = main.parent
    wanted = [main] + ([p for p in (project / session_id).rglob("*") if p.is_file() and not p.name.endswith((".prev", ".part"))]
                       if (project / session_id).is_dir() else [])
    moved = []
    for src in wanted:
        dst = Path(source).expanduser() / src.relative_to(dest)
        try:
            if dst.exists() and dst.stat().st_size >= src.stat().st_size:
                continue
        except OSError:
            pass
        _copy(src, dst)
        moved.append(src.relative_to(dest).as_posix())
    return dict(project=project.name, restored=moved)
