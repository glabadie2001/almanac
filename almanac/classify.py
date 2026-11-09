"""What one shell call was for, and whether it used a shell helper (`rg`, `rt`, `door`, `swap`) or the old habit it replaces.

`family(cmd)` puts a Bash/PowerShell command into one bucket (tests, search, edit, http...). `adoption(cmd)`
scores four habits, each as 0 (neither), 1 (old way),
2 (helper) or 3 (both in one call):

* search: `rg` vs grep that is not fed by a pipe (recursive or over files)
* tests:  `rt` / `covenant test` / `vitest --changed` vs a whole-suite run
* http:   `door` vs curl to a local port or a urllib heredoc
* edit:   `swap` vs a python heredoc that rewrites a file or `cat > file <<`
"""
from __future__ import annotations

import re

FAMILIES = ("tests", "covenant", "build", "search", "range read", "read", "list", "git", "http", "edit",
            "python inline", "python script", "process", "other")
PAIRS = ("search", "tests", "http", "edit")

_LEAD = re.compile(r"^\s*(?:cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*)+")
_START = r"(?:^|[;&(\n]|&&|\|\|)\s*(?:timeout\s+\d+\s+)?"           # a command start, not after a pipe
_HEREDOC = re.compile(r"python[0-9.]*(?:\.exe)?[\"']?\s+(?:-[IXBu]\s+)*-\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)(?:\n\1|$)", re.S)

_TESTS = re.compile(r"-m\s+unittest|\bpytest\b|\bvitest\b|dotnet\s+test|tools/check\.sh|" + _START + r"rt\b|covenant\s+test")
_COVENANT = re.compile(r"-m\s+covenant\s+(?:check|list|summary|tighten|suites|react|seed)|-m\s+covenant\s*(?:$|[|;&2>])")
_BUILD = re.compile(r"npm\s+run\s+build|vite\s+build|dotnet\s+build|\btsc\b|npm\s+ci\b|npm\s+install")
_GIT = re.compile(r"^\s*git\b")
_RANGE = re.compile(r"\bsed\s+-n\s+['\"]?\d+,\d+p")
_SEARCH = re.compile(r"\b(?:grep|egrep|rg|Select-String)\b")
_READ = re.compile(r"\b(?:cat|head|tail|Get-Content|nl)\b")
_LIST = re.compile(r"\b(?:ls|dir|find|Get-ChildItem|tree|du|wc)\b")
_PROCESS = re.compile(r"\b(?:tasklist|netstat|Get-Process|Stop-Process|taskkill|Get-NetTCPConnection|Win32_Process)\b")
_PY_INLINE = re.compile(r"python[0-9.]*(?:\.exe)?[\"']?\s+(?:-[IXBu]\s+)*-c\b")
_PY_SCRIPT = re.compile(r"(?:^|[;&|\n]\s*)(?:\S*/)?(?:python[0-9.]*|py|uv\s+run\s+python)(?:\.exe)?[\"']?\s+(?:-[IXBu]\s+)*(?!-[cm]\b)[\"']?[\w./\\:$-]+\.py\b")
_PY_MODULE = re.compile(r"python[0-9.]*(?:\.exe)?[\"']?\s+(?:-[IXBu]\s+)*-m\s+\w")

_RG = re.compile(_START + r"rg\s")
_GREP_FILES = re.compile(_START + r"(?:grep|egrep)\s")
_RT = re.compile(_START + r"rt(?:\s|$)|-m\s+covenant\s+test\b|vitest\s+(?:run\s+)?--changed")
_WHOLE_SUITE = re.compile(r"unittest\s+discover|covenant\s+suites|\bpytest\b(?![^\n|;&]*\btest_\w+\.py)|"
                          r"vitest\s+run(?![^\n|;&]*(?:\.test\.|\.spec\.|--changed|\s-t\s))|dotnet\s+test|tools/check\.sh")
_DOOR = re.compile(_START + r"door(?:\s|$)|-m\s+garrison\s+door\b")
_CURL_LOCAL = re.compile(r"\b(?:curl|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b[^\n]*(?:127\.0\.0\.1|localhost)")
_SWAP = re.compile(_START + r"swap\s")
_CAT_WRITE = re.compile(r"\bcat\s*>{1,2}\s*\S+\s*<<")
_WRITES = re.compile(r"\.write_text\(|\.write_bytes\(|open\([^)]*['\"][wa]b?['\"]")


def strip_lead(cmd: str) -> str:
    """Drop the `cd "<project>" &&` prefix that starts most calls."""
    return _LEAD.sub("", cmd or "", count=1)


def heredoc_body(cmd: str) -> str | None:
    m = _HEREDOC.search(cmd or "")
    return m.group(2) if m else None


def head(cmd: str) -> str:
    """The leading word of the first real command (`grep`, `python`, `git`, `door`...)."""
    body = strip_lead(cmd)
    for line in body.split("\n"):
        line = re.sub(r"^\s*(?:[A-Z_][A-Z0-9_]*=\S*\s+)+", "", line).strip()
        if not line or line.startswith("#"):
            continue
        word = re.split(r"[\s;|&()]", line, maxsplit=1)[0].strip("\"'")
        word = re.sub(r".*[/\\]", "", word)
        return re.sub(r"\.exe$", "", word, flags=re.I)[:40] or "other"
    return "other"


def family(cmd: str) -> str:
    c = strip_lead(cmd)
    body = heredoc_body(c)
    if body is not None:
        if re.search(r"urllib|requests\.|http\.client", body):
            return "http"
        if _WRITES.search(body):
            return "edit"
        return "python inline"
    if _COVENANT.search(c):
        return "covenant"
    if _TESTS.search(c):
        return "tests"
    if _BUILD.search(c):
        return "build"
    if _SWAP.search(c) or _CAT_WRITE.search(c) or re.search(r"\bsed\s+-i\b|\bSet-Content\b", c):
        return "edit"
    if _DOOR.search(c) or re.search(r"\b(?:curl|Invoke-WebRequest|Invoke-RestMethod)\b", c):
        return "http"
    if _GIT.search(c):
        return "git"
    if _PY_INLINE.search(c):
        return "python inline"
    if _RANGE.search(c) and not _SEARCH.search(c):
        return "range read"
    if _SEARCH.search(c):
        return "search"
    if _PY_SCRIPT.search(c) or _PY_MODULE.search(c):
        return "python script"
    if _READ.search(c):
        return "read"
    if _LIST.search(c):
        return "list"
    if _PROCESS.search(c):
        return "process"
    return "other"


def _score(new: bool, old: bool) -> int:
    return (2 if new else 0) | (1 if old else 0)


def adoption(cmd: str) -> dict[str, int]:
    c = strip_lead(cmd)
    body = heredoc_body(c) or ""
    rewrites = bool(_WRITES.search(body)) and (".read_text(" in body or bool(re.search(r"open\([^)]*['\"]r", body)))
    return {
        "search": _score(bool(_RG.search(c)), bool(_GREP_FILES.search(c))),
        "tests": _score(bool(_RT.search(c)), bool(_WHOLE_SUITE.search(c))),
        "http": _score(bool(_DOOR.search(c)), bool(_CURL_LOCAL.search(c)) or bool(re.search(r"urllib|requests\.", body))),
        "edit": _score(bool(_SWAP.search(c)), rewrites or bool(_CAT_WRITE.search(c))),
    }
