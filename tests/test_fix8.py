"""FIX-8, from the reviewer's second pass (v2.py, b1.py): a one-line flagged message keeps its words; a typed channel
line on a direct message; a stale cookie before a fresh one; and never launching Claude through a .cmd shim."""
import time
from unittest import mock

from support import TOKEN, Fixture, runner_probe, write_jsonl
from test_server_fixes import MeshRig

from almanac import config, spawner, uds, web
from almanac.door import Service, make_handler, resume_chat
from almanac.fleet import Fleet
from almanac.store import Store


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SplitNeedsTests(Fixture):
    def test_one_rule_for_the_tag(self):
        canonical = uds.needs_marker("blocking", "arch") + "body"
        cases = {
            "[needs: reply] can you look at foo.py?": ("reply", "can you look at foo.py?"),
            canonical: ("blocking", "body"),
            "[needs: reply]\nbody": ("reply", "body"),
            "[needs: blocking] first line\nsecond": ("blocking", "first line\nsecond"),
            "[WIP] x": (None, "[WIP] x"),
            "[needs: reply]": ("reply", ""),
        }
        for body, want in cases.items():
            self.assertEqual(uds.split_needs(body), want, body)
            parsed = uds.parse_cross_session(uds.format_cross_session(body, from_pipe="p", from_name="arch"))
            self.assertEqual((parsed["needs"], parsed["body"]), want, f"the reader applies the same rule: {body!r}")


class OneLinerTests(MeshRig):
    def setUp(self):
        super().setUp()
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None, provider="claude",
                                model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def read_back(self, got):
        wire = uds.format_cross_session(got["body"], from_pipe="p", from_name=got["from_name"], channel=got.get("channel"))
        write_jsonl(self.projects / "p" / "sess-impl.jsonl", [{"type": "user", "sessionId": "sess-impl",
                    "timestamp": iso(time.time()), "cwd": "c", "message": {"content": wire}}])
        self.store.ingest(self.projects)

    def test_a_one_line_flag_keeps_its_words(self):
        status, _, res = self.send(to="impl", text="[needs: reply] can you look at foo.py?")
        self.assertEqual((status, res["message"]["needs"], res["message"]["body"]), (200, "reply", "can you look at foo.py?"))
        got = self.inbox["impl"][0]
        self.assertEqual(got["body"], uds.needs_marker("reply", "operator") + "can you look at foo.py?")
        self.read_back(got)
        self.assertEqual([(m["needs"], m["body"]) for m in self.store.messages()], [("reply", "can you look at foo.py?")])

    def test_a_bracketed_body_is_left_alone(self):
        _, _, res = self.send(to="impl", text="[WIP] x")
        self.assertEqual((res["message"]["needs"], res["message"]["body"]), (None, "[WIP] x"))

    def test_a_typed_channel_line_is_the_channel(self):
        status, _, res = self.send(to="impl", text="[channel: #impl]\nhello")
        self.assertEqual((status, res["message"]["channel"], res["message"]["body"]), (200, "#impl", "hello"))
        self.read_back(self.inbox["impl"][0])
        self.assertEqual([(m["channel"], m["body"]) for m in self.store.messages()], [("#impl", "hello")])


class RepeatedCookieTests(Fixture):
    def test_a_stale_value_before_the_good_one(self):
        token, name = "x" * 40, web.cookie_name(8812)
        good = web.new_session(token)
        self.assertTrue(web.valid_session(f"{name}=1.bad; {name}={good}", token, name=name))
        self.assertFalse(web.valid_session(f"{name}=1.bad; {name}=2.worse", token, name=name))


class ShimTests(Fixture):
    """b1.py: cmd.exe re-parses a batch file's command line, so prompts passed as arguments could run commands."""

    def setUp(self):
        super().setUp()
        self.bin = self.root / "npm"
        self.bin.mkdir()
        self.shim = self.bin / "claude.cmd"
        self.shim.write_text("@echo off\n", encoding="utf-8")
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)

    def service(self, claude="claude.exe"):
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        probe = dict(home=self.root / "home", env={}, which=lambda n: str(self.shim) if n == "claude" else None)
        return Service(self.store, self.projects, 300, fleet=Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {}),
                       state=self.root / "state", claude=claude, gemini="agy.exe", probe=probe)

    def test_a_cmd_on_path_is_not_a_claude_runner(self):
        shims = []
        found, searched = config.find_claude("claude.exe", home=self.root / "home", which=lambda n: str(self.shim), shims=shims)
        self.assertIsNone(found)
        self.assertEqual(shims, [str(self.shim)])
        r = self.service().runners()["claude"]
        self.assertFalse(r["ok"])
        self.assertIn(f"found {self.shim}, a .cmd shim", r["reason"])
        self.assertNotIn("claude.cmd", config.CLAUDE_NAMES)
        self.assertIn("agy.cmd", config.GEMINI_NAMES, "Gemini's prompts go over stdin: its shim is fine")

    def test_spawn_and_resume_refuse_it_and_nothing_is_launched(self):
        for service in (self.service(), self.service(claude=str(self.shim))):    # found on PATH, or configured
            handler = make_handler(service, TOKEN)
            repo = self.root / "ws"
            repo.mkdir(exist_ok=True)
            with mock.patch.object(spawner.subprocess, "Popen") as popen:
                status, _, res = self.request(handler, "/api/spawn", method="POST",
                                              body={"cwd": str(repo), "role_id": "architect-claude", "agent_name": "a"})
            self.assertEqual(status, 409)
            self.assertIn(".cmd", res["error"])
            popen.assert_not_called()
            ok, message, _ = resume_chat(service, "00000000-0000-4000-8000-000000000000")
            self.assertFalse(ok)
            self.assertIn(".cmd", message)

    def test_a_configured_cmd_path_is_refused_by_the_probe(self):
        self.assertIsNone(config.find_claude(str(self.shim), home=self.root / "home", which=lambda n: None)[0])
        self.assertTrue(config.is_shim("C:/x/CLAUDE.BAT") and not config.is_shim("C:/x/claude.exe"))
