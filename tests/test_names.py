import json
import time
from unittest import mock

from support import TOKEN, Fixture, write_jsonl

from almanac import namer
from almanac.door import Service, make_handler
from almanac.store import Store
from test_chats import OTHER, SID, chat_records


class FakeClient:
    def __init__(self, reply="Name: \"Archive Every Chat Forever.\"\nextra line"):
        self.reply, self.calls = reply, []

    def chat(self, model, messages, **options):
        self.calls.append((model, messages, options))
        return {"content": self.reply, "usd": 0.0004}


class NamerTests(Fixture):
    def test_digest_prompt_and_clean(self):
        path = self.projects / "p" / f"{SID}.jsonl"
        recs = [{"type": "user", "sessionId": SID, "cwd": "D:/x", "message": {"content": f"request {i}"}} for i in range(10)]
        recs += [{"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
                 {"type": "user", "isSidechain": True, "message": {"content": "subagent chatter"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "done " * 200}]}},
                 {"type": "ai-title", "aiTitle": "Requests galore", "sessionId": SID}]
        write_jsonl(path, recs)
        d = namer.digest(path)
        self.assertEqual((d["cwd"], d["auto_title"], d["custom_title"]), ("D:/x", "Requests galore", None))
        self.assertEqual(d["first"], ["request 0", "request 1", "request 2", "request 3"])
        self.assertEqual(d["last"], ["request 6", "request 7", "request 8", "request 9"])
        self.assertLessEqual(len(d["replies"][0]), 300)
        p = namer.prompt(d)
        self.assertIn("Requests galore", p)
        self.assertNotIn("subagent chatter", p)
        self.assertEqual(namer.clean('Title: "Bank Grid Fix."'), "Bank Grid Fix")
        self.assertEqual(namer.clean("\n\n**Almanac Resume Button**\nwhy: ..."), "Almanac Resume Button")

    def test_name_through_client(self):
        fake = FakeClient()
        name, usd = namer.Namer(fake).name({"first": ["build it"], "cwd": "D:/x"})
        self.assertEqual((name, usd), ("Archive Every Chat Forever", 0.0004))
        model, messages, options = fake.calls[0]
        self.assertEqual((model, messages[0]["role"], options["max_tokens"]), ("haiku", "system", 300))
        with self.assertRaises(ValueError):
            namer.Namer(fake).name({"first": [], "auto_title": None})
        with self.assertRaises(ValueError):
            namer.Namer(FakeClient("")).name({"first": ["x"]})

    def test_claude_cli_client(self):
        from types import SimpleNamespace
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, input=kw["input"], env=kw["env"])
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "Fix The Thing", "total_cost_usd": 0.001}).encode(), stderr=b"")
        with mock.patch.dict("os.environ", {"CLAUDECODE": "1", "CLAUDE_CODE_X": "1"}):
            reply = namer.ClaudeCLI("claude.exe", run=run).chat("haiku", [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
        self.assertEqual(reply, {"content": "Fix The Thing", "usd": 0.001})
        argv = seen["argv"]
        self.assertEqual(argv[:4], ["claude.exe", "-p", "--model", "haiku"])
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--max-budget-usd"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "local", "no user CLAUDE.md, hooks or MCP")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "S")
        self.assertEqual(seen["input"], b"U")
        self.assertFalse({"CLAUDECODE", "CLAUDE_CODE_X"} & set(seen["env"]), "never looks nested")
        fail = lambda argv, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"not logged in")
        with self.assertRaisesRegex(RuntimeError, "not logged in"):
            namer.ClaudeCLI(run=fail).chat("haiku", [{"role": "user", "content": "U"}])

    def test_write_back_appends_rename_records_once_and_never_fuses_a_line(self):
        path = self.projects / "p" / f"{SID}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"type": "user", "message": {"content": "hi"}}')          # last line not terminated
        self.assertTrue(namer.write_back(path, SID, "Hello Chat"))
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["type"], "user", "the half-written line stays its own line")
        self.assertEqual([json.loads(l)["type"] for l in lines[1:]], ["custom-title", "agent-name"])
        self.assertEqual(namer.digest(path)["custom_title"], "Hello Chat")
        self.assertFalse(namer.write_back(path, SID, "Another Name"), "a named chat is never renamed")


class NamingServiceTests(Fixture):
    def setUp(self):
        super().setUp()
        now = time.time()
        self.cwd = str(self.root)
        write_jsonl(self.projects / "D--x" / f"{SID}.jsonl", chat_records(SID, self.cwd, now - 7200, title="Auto Title"))
        renamed = chat_records(OTHER, self.cwd, now - 7000) + [{"type": "custom-title", "customTitle": "Mine", "sessionId": OTHER}]
        write_jsonl(self.projects / "D--x" / f"{OTHER}.jsonl", renamed)
        self.store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(self.store.close)
        self.fake = FakeClient("Archive Every Chat")
        self.service = Service(self.store, self.projects, 300, fleet=None, state=self.root / "state", namer=namer.Namer(self.fake))
        self.service.ingest()
        self.handler = make_handler(self.service, TOKEN)

    def test_store_knows_custom_titles_and_what_is_unnamed(self):
        rows = {c["id"]: c for c in self.store.chats()}
        self.assertEqual((rows[OTHER]["title"], rows[OTHER]["title_source"]), ("Mine", "renamed"))
        self.assertEqual((rows[SID]["title"], rows[SID]["title_source"]), ("Auto Title", "auto"))
        self.assertEqual([c["id"] for c in self.store.unnamed(time.time())], [SID], "your rename is never queued")
        self.assertEqual(self.store.unnamed(time.time() - 3 * 3600), [], "not quiet long enough yet")
        self.assertEqual(self.store.titles([SID, OTHER, "nope"]), {SID: "Auto Title", OTHER: "Mine"})

    def test_name_one_writes_back_and_the_title_follows(self):
        r = self.service.name_one(SID)
        self.assertEqual((r["ok"], r["name"], r["written_back"]), (True, "Archive Every Chat", True))
        self.assertEqual(namer.digest(self.projects / "D--x" / f"{SID}.jsonl")["custom_title"], "Archive Every Chat")
        self.service.ingest()
        row = {c["id"]: c for c in self.store.chats()}[SID]
        self.assertEqual((row["title"], row["title_source"]), ("Archive Every Chat", "haiku"))
        self.assertEqual(self.service.name_one(SID)["skipped"], "already named")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.service.name_one(OTHER)["skipped"], "already named")

    def test_live_chats_are_left_alone(self):
        with mock.patch("almanac.door._live_sessions", return_value={SID: 99}):
            r = self.service.name_one(SID)
        self.assertEqual((r["ok"], r["error"]), (False, "still running; named when it ends"))
        self.assertEqual(self.fake.calls, [])

    def test_failures_are_recorded_and_retried_a_day_later(self):
        self.service.namer = namer.Namer(FakeClient(""))
        self.assertFalse(self.service.name_one(SID)["ok"])
        self.assertEqual(self.store.unnamed(time.time()), [], "not retried within a day")
        self.store.db.execute("update sessions set named_at = ? where id = ?", (time.time() - 2 * 86400, SID))
        self.assertEqual([c["id"] for c in self.store.unnamed(time.time())], [SID])

    def test_endpoint(self):
        status, _, doc = self.request(self.handler, "/api/name", method="POST", body={"id": SID})
        self.assertEqual((status, doc["name"]), (200, "Archive Every Chat"))
        status, _, doc = self.request(self.handler, "/api/name", method="POST", body={"id": "../x"})
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/name", method="POST", body={"all": True})
        self.assertEqual((status, doc["queued"]), (202, 0), "everything here is named now")
        status, _, doc = self.request(self.handler, "/api/names")
        self.assertEqual(status, 200)
