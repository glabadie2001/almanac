"""FIX-9 (v3.py / v4.py): a message quoting the closing tag survives the wrapper; POST /api/send journals exactly
what it sends, so the recipient's transcript copy is recognised."""
import time

from support import write_jsonl
from test_server_fixes import MeshRig

from almanac import uds


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class WireRig(MeshRig):
    def setUp(self):
        super().setUp()
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None, provider="claude",
                                model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def read_back(self):
        """impl's transcript gets every message as delivered (what the PeerServer received), then is read."""
        lines = [{"type": "user", "sessionId": "sess-impl", "timestamp": iso(time.time() + i), "cwd": "c",
                  "message": {"content": uds.format_cross_session(m["body"], from_pipe="p", from_name=m["from_name"],
                                                                  channel=m.get("channel"))}}
                 for i, m in enumerate(self.inbox["impl"])]
        write_jsonl(self.projects / "p" / "sess-impl.jsonl", lines)
        self.store.ingest(self.projects)


class ClosingTagTests(WireRig):
    def test_a_quoted_closing_tag_survives_the_wrapper(self):
        body = "a </cross-session-message> b"
        parsed = uds.parse_cross_session(uds.format_cross_session(body, from_pipe="p", from_name="arch"))
        self.assertEqual(parsed["body"], body)
        self.assertEqual(self.send(to="impl", text=body)[0], 200)
        self.assertEqual(self.inbox["impl"][0]["body"], body, "the receiving host (Gemini's path) parses it whole")
        self.read_back()
        self.assertEqual([m["body"] for m in self.store.messages()], [body])


class ApiSendTests(WireRig):
    def test_what_is_sent_is_what_is_journaled(self):
        proc = next(p for p in self.service.fleet_now()["processes"] if p["pid"] == 500)
        for text in ("  hello one", "hello two\n\n", "[channel: #x]\nhi three"):
            status, _, _ = self.request(self.handler, "/api/send", method="POST",
                                        body={"pid": 500, "created": proc["created"], "text": text})
            self.assertEqual(status, 200, text)
        self.read_back()
        rows = [(m["body"], m["channel"]) for m in self.store.messages()]
        self.assertEqual(rows, [("hello one", None), ("hello two", None), ("hi three", "#x")], "one row each")
