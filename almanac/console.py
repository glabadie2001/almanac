"""Type into another process's console: `python -m almanac.console PID` with the text on stdin.

Windows lets a process attach to the console of another process the same user owns (AttachConsole) and
write key events into its input buffer (WriteConsoleInputW), which the program reads exactly as if the
keys were typed. Claude Code's terminal UI takes them like a person typing. The text goes in first;
Enter follows after a short pause so the UI does not read the burst as a paste with a newline in it.

Run it as its own short-lived process with no console of its own (the server uses DETACHED_PROCESS):
a process can attach to only one console, and the server must never attach to anyone's.
Exit codes: 0 typed, 2 bad input, 3 could not attach, 4 could not open or write the input buffer.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

KEY_EVENT = 0x0001
VK_RETURN = 0x0D
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
DETACHED_PROCESS = 0x00000008
MAX_CHARS = 4000


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_size", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


def flatten(text: str) -> str:
    """One line: a newline would be Enter, which submits half a message."""
    return " ".join(str(text).replace("\r", "\n").split())[:MAX_CHARS]


def records(text: str, enter: bool) -> list[INPUT_RECORD]:
    out = []
    units = text.encode("utf-16-le")
    chars = [units[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(units), 2)]
    for ch in chars + (["\r"] if enter else []):
        vk = VK_RETURN if ch == "\r" else 0
        for down in (True, False):
            rec = INPUT_RECORD()
            rec.EventType = KEY_EVENT
            rec.Event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, 0, ch, 0)
            out.append(rec)
    return out


def type_into(pid: int, text: str, *, settle: float = 0.35) -> None:
    """Attach to `pid`'s console and type `text`, then Enter. Raises OSError with a code attribute."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.FreeConsole()
    if not k32.AttachConsole(wintypes.DWORD(pid)):
        err = OSError(f"cannot attach to the console of pid {pid} (Windows error {ctypes.get_last_error()})")
        err.code = 3
        raise err
    try:
        handle = k32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if not handle or handle == INVALID_HANDLE:
            err = OSError(f"cannot open the console input of pid {pid} (Windows error {ctypes.get_last_error()})")
            err.code = 4
            raise err
        try:
            for batch, pause in ((records(text, False), settle), (records("", True), 0)):
                if not batch:
                    continue
                array = (INPUT_RECORD * len(batch))(*batch)
                written = wintypes.DWORD(0)
                if not k32.WriteConsoleInputW(handle, array, len(batch), ctypes.byref(written)) or written.value != len(batch):
                    err = OSError(f"wrote {written.value} of {len(batch)} key events (Windows error {ctypes.get_last_error()})")
                    err.code = 4
                    raise err
                time.sleep(pause)
        finally:
            k32.CloseHandle(handle)
    finally:
        k32.FreeConsole()


def send(pid: int, text: str, *, python: str = sys.executable, timeout: float = 20) -> tuple[bool, str]:
    """From the server: run this module detached so the attach happens in a throwaway process."""
    line = flatten(text)
    if not line:
        return False, "nothing to send"
    try:
        proc = subprocess.run([python, "-m", "almanac.console", str(int(pid))], input=line.encode("utf-8"),
                              capture_output=True, timeout=timeout, creationflags=DETACHED_PROCESS,
                              cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run the console helper: {exc}"
    if proc.returncode == 0:
        return True, f"typed {len(line)} characters and Enter"
    return False, (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [f"helper exited {proc.returncode}"])[-1]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python -m almanac.console PID < text", file=sys.stderr)
        return 2
    text = flatten(sys.stdin.buffer.read().decode("utf-8", "replace"))
    if not text:
        print("nothing to type", file=sys.stderr)
        return 2
    try:
        type_into(int(argv[0]), text)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return getattr(exc, "code", 4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
