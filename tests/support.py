"""Fixtures: sockets forbidden, synthetic transcripts, the real door in memory."""
import atexit
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
TMP = ROOT / ".test-tmp"
TMP.mkdir(exist_ok=True)
_network = patch.object(socket, "socket", side_effect=AssertionError("Tests must never open a socket"))
_network.start()
atexit.register(_network.stop)

TOKEN = "machine-token-" + "0" * 32
PORT = 8812


def assistant(session, ts, msg_id, blocks, usage=None):
    return {"type": "assistant", "sessionId": session, "timestamp": ts, "isSidechain": False, "version": "2.1.319",
            "message": {"id": msg_id, "model": "claude-test", "content": blocks,
                        "usage": usage or {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                           "cache_read_input_tokens": 890, "output_tokens": 20}}}


def tool_use(use_id, name, **inp):
    return {"type": "tool_use", "id": use_id, "name": name, "input": inp}


def result(session, ts, use_id, text, *, image=False):
    content = [{"type": "text", "text": text}] + ([{"type": "image", "source": {"data": "x" * 50000}}] if image else [])
    return {"type": "user", "sessionId": session, "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": content}]}}


def write_jsonl(path: Path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class MemorySocket:
    def __init__(self, wire):
        self.input, self.output = io.BytesIO(wire), io.BytesIO()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, timeout):
        pass

    def getsockname(self):
        return ("127.0.0.1", PORT)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()

    def request(self, handler, path, token=TOKEN, method="GET", body=None, headers=None):
        payload = json.dumps(body if body is not None else {}).encode() if method not in ("GET", "HEAD") else b""
        hdr = {"Host": f"127.0.0.1:{PORT}", "Content-Length": str(len(payload)), "Connection": "close"}
        if method not in ("GET", "HEAD"):
            hdr["Content-Type"] = "application/json"
        if token:
            hdr["Authorization"] = "Bearer " + token
        hdr.update(headers or {})
        wire = f"{method} {path} HTTP/1.1\r\n".encode() + b"".join(f"{k}: {v}\r\n".encode() for k, v in hdr.items()) + b"\r\n"
        wire += payload
        conn = MemorySocket(wire)
        handler(conn, ("127.0.0.1", 9999), SimpleNamespace(server_port=PORT, server_name="localhost"))
        head, _, raw_body = conn.output.getvalue().partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        out_headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        try:
            doc = json.loads(raw_body)
        except ValueError:
            doc = raw_body
        return status, out_headers, doc
