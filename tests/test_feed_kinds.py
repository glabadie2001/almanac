"""Slice 3B: FLEET and GALLERY feeds filter by event kind on the server, before the limit cuts them, so a prompt
buried under a long run of tool calls still shows when the operator asks for dialog only."""
import json

from support import TOKEN, Fixture, assistant, result, tool_use, write_jsonl
from test_fleet import FakeMachine

from almanac.door import Service, make_handler
from almanac.fleet import Fleet
from almanac.store import Store


class FeedKindTests(Fixture):
    def setUp(self):
        super().setUp()
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "100.json").write_text(json.dumps({
            "pid": 100, "sessionId": "sess-a", "cwd": "D:\\x", "status": "busy", "name": "a", "kind": "interactive"}),
            encoding="utf-8")
        records = [{"type": "user", "timestamp": "t0", "message": {"content": "please fix the bug"}}]
        for i in range(30):
            records.append(assistant("sess-a", f"t{i}a", f"m{i}", [tool_use(f"u{i}", "Bash", command=f"echo {i}")]))
            records.append(result("sess-a", f"t{i}b", f"u{i}", f"out {i}"))
        write_jsonl(home / "projects" / "D--x" / "sess-a.jsonl", records)
        machine = FakeMachine()
        store = Store(self.root / "a.db")
        self.addCleanup(store.close)
        self.service = Service(store, self.projects, 300, fleet=Fleet(home, table=machine.table, facts=machine.facts,
                                                                       lines=machine.lines))
        self.handler = make_handler(self.service, TOKEN)

    def test_a_kind_filter_reaches_past_the_limit(self):
        _, _, plain = self.request(self.handler, "/api/session?pid=100&limit=5")
        self.assertEqual(len(plain["events"]), 5)
        self.assertNotIn("you", {e["kind"] for e in plain["events"]})
        _, _, dialog = self.request(self.handler, "/api/session?pid=100&limit=5&kinds=you,claude")
        self.assertEqual([(e["kind"], e["text"]) for e in dialog["events"]], [("you", "please fix the bug")])
        _, _, tools = self.request(self.handler, "/api/session?pid=100&limit=4&kinds=tool")
        self.assertEqual([e["kind"] for e in tools["events"]], ["tool"] * 4)

    def test_the_gallery_route_filters_too(self):
        _, _, doc = self.request(self.handler, "/api/sessions?pids=100,999&limit=3&kinds=you")
        found, gone = doc["sessions"]
        self.assertEqual([e["kind"] for e in found["events"]], ["you"])
        self.assertTrue(gone["gone"])
