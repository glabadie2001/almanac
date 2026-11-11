"""Slice G: where Almanac finds the Gemini and Claude runners, live re-probing, /api/health, refusing a Gemini spawn
with no runner, and (fold-in) flagged messages sent straight to the hub or through /api/send."""
import json
from pathlib import Path
from unittest import mock

from support import TOKEN, Fixture, runner_probe
from test_server_fixes import MeshRig

from almanac import config, spawner, uds, web
from almanac.door import Service, make_handler
from almanac.fleet import Fleet
from almanac.store import Store


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class FindTests(Fixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.local = self.root / "local"
        self.env = {"LOCALAPPDATA": str(self.local)}

    def find(self, configured="~/nowhere/agy.exe", which=lambda n: None, env=None):
        return config.find_gemini(configured, home=self.home, env=self.env if env is None else env, which=which)

    def test_the_second_place_wins_over_the_fourth(self):
        second = touch(self.local / "agy" / "bin" / "agy.exe")
        touch(self.local / "Programs" / "antigravity" / "bin" / "agy.exe")
        found, searched = self.find()
        self.assertEqual(found, str(second))
        self.assertEqual(searched[1:3], [str(self.home / ".gemini" / "bin"), str(self.local / "agy" / "bin")])

    def test_the_fourth_place_and_the_older_cli_name(self):
        fourth = touch(self.local / "Programs" / "antigravity" / "bin" / "gemini.cmd")
        found, searched = self.find()
        self.assertEqual(found, str(fourth))
        self.assertEqual(len(searched), 5, "configured, ~/.gemini/bin, agy/bin, antigravity, Programs/antigravity")

    def test_the_configured_file_comes_first(self):
        mine = touch(self.root / "tools" / "agy.exe")
        touch(self.home / ".gemini" / "bin" / "agy.exe")
        self.assertEqual(self.find(str(mine))[0], str(mine))

    def test_path_is_the_last_resort_and_missing_env_is_fine(self):
        found, searched = self.find(which=lambda n: "/usr/bin/gemini" if n == "gemini" else None, env={})
        self.assertEqual(found, "/usr/bin/gemini")
        self.assertEqual(searched[-1], "PATH (agy, gemini)")
        self.assertFalse(any("Programs" in s for s in searched), "no LOCALAPPDATA, no guessed folder")

    def test_nothing_found(self):
        found, searched = self.find()
        self.assertIsNone(found)
        self.assertIn(str(self.local / "agy" / "bin"), searched)

    def test_claude_probe(self):
        hit = touch(self.home / ".local" / "bin" / "claude.exe")
        self.assertEqual(config.find_claude("claude.exe", home=self.home, which=lambda n: None)[0], str(hit))

    def test_load_keeps_the_configured_value_and_what_it_searched(self):
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps({"gemini": str(self.root / "missing" / "agy.exe")}), encoding="utf-8")
        with mock.patch.object(config, "find_gemini", return_value=(None, ["a", "b"])):
            settings = config.load(cfg)
        self.assertEqual(settings["gemini"], str(self.root / "missing" / "agy.exe"))
        self.assertEqual((settings["gemini_configured"], settings["gemini_searched"]),
                         (str(self.root / "missing" / "agy.exe"), ["a", "b"]))


class ServiceRunnerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.store = Store(self.state / "almanac.db")
        self.addCleanup(self.store.close)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        self.fleet = Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {})
        self.service = Service(self.store, self.projects, 300, fleet=self.fleet, state=self.state,
                               claude="claude.exe", gemini="agy.exe", probe=runner_probe(self.root, gemini=False))
        self.handler = make_handler(self.service, TOKEN)
        self.repo = self.root / "ws"
        self.repo.mkdir()

    def test_an_install_made_while_running_is_found(self):
        self.assertFalse(self.service.runners()["gemini"]["ok"])
        self.assertEqual(self.service.gemini, "agy.exe", "the configured value while nothing is found")
        agy = touch(self.root / "home" / ".gemini" / "bin" / "agy.exe")
        self.assertFalse(self.service.runners()["gemini"]["ok"], "cached for RUNNER_TTL")
        self.assertEqual(self.service.runners(fresh=True)["gemini"], dict(path=str(agy), ok=True, searched=mock.ANY, reason=None))
        self.assertEqual(self.service.gemini, str(agy))
        with mock.patch("almanac.door.RUNNER_TTL", 0):
            agy.unlink()
            self.assertFalse(self.service.runners()["gemini"]["ok"], "and re-probed once the TTL passes")

    def test_health_is_the_operators(self):
        status, _, doc = self.request(self.handler, "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(set(doc), {"runners", "hub", "ingest_error"})
        self.assertEqual(set(doc["runners"]), {"claude", "gemini"})
        self.assertFalse(doc["runners"]["gemini"]["ok"])
        self.assertTrue(doc["runners"]["gemini"]["searched"])
        self.assertFalse(doc["hub"])
        self.assertEqual(self.request(self.handler, "/api/health", token=web.agent_token(TOKEN, "a"))[0], 403)

    def fake_popen(self, launched):
        def popen(argv, **kwargs):
            launched.append(list(argv))
            return mock.MagicMock(pid=7000 + len(launched), **{"poll.return_value": None})
        return popen

    def test_a_gemini_spawn_without_a_runner_is_refused_and_nothing_is_recorded(self):
        launched = []
        with mock.patch.object(spawner.subprocess, "Popen", side_effect=self.fake_popen(launched)):
            status, _, res = self.request(self.handler, "/api/spawn", method="POST",
                                          body={"cwd": str(self.repo), "role_id": "reviewer-gemini", "agent_name": "rev"})
        self.assertEqual(status, 409)
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith("Gemini runner not found; looked in: "), res["error"])
        self.assertIn('set "gemini" in config.json', res["error"])
        self.assertEqual((launched, self.store.spawned()), ([], []))
        self.assertFalse((self.state / "spawns").exists(), "no spawn folder either")

    def test_a_squad_still_launches_its_claude_members(self):
        launched = []
        with mock.patch.object(spawner.subprocess, "Popen", side_effect=self.fake_popen(launched)):
            status, _, res = self.request(self.handler, "/api/squads", method="POST", body={
                "name": "sq", "cwd": str(self.repo), "members": [
                    {"role_id": "architect-claude", "agent_name": "lead", "workspace_mode": "shared"},
                    {"role_id": "reviewer-gemini", "agent_name": "rev", "workspace_mode": "shared"}]})
        self.assertEqual((status, res["launched"], res["failed"]), (200, 1, 1))
        by_name = {a["agent_name"]: a for a in res["squad"]["agents"]}
        self.assertTrue(by_name["lead"]["ok"])
        self.assertIn("Gemini runner not found", by_name["rev"]["error"])
        self.assertEqual([s["agent_name"] for s in self.store.spawned()], ["lead"])
        self.assertEqual(len(launched), 1)


class FlaggedHubTests(MeshRig):
    def test_a_flagged_message_sent_straight_to_the_hub_is_journaled_as_open(self):
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)
        hub = uds.PeerServer(self.root / "claude" / "sessions", name="almanac-hub", pid=601, provider="almanac",
                             on_message=self.service._on_hub_message, in_memory=True).start()
        self.addCleanup(hub.close)
        wire = uds.format_cross_session(uds.needs_marker("blocking", "impl") + "Which database?", from_pipe="p",
                                        from_name="impl")
        frames = [{"type": "auth", "token": hub.token},
                  {"type": "user", "message": {"role": "user", "content": wire}}]
        self.assertEqual(hub.accept_bytes("".join(json.dumps(f) + "\n" for f in frames).encode()), 1)
        [row] = self.store.messages()
        self.assertEqual((row["from_name"], row["to_name"], row["needs"], row["body"], row["squad_id"]),
                         ("impl", "almanac-hub", "blocking", "Which database?", "sq-1"))
        self.assertEqual(self.store.operator_counts()["open"], dict(reply=0, blocking=1))

    def test_api_send_carries_a_flag(self):
        proc = next(p for p in self.service.fleet_now()["processes"] if p["pid"] == 500)
        status, _, res = self.request(self.handler, "/api/send", method="POST",
                                      body={"pid": 500, "created": proc["created"], "text": "Ready?", "needs": "reply"})
        self.assertEqual(status, 200, res)
        self.assertTrue(self.inbox["impl"][0]["body"].startswith("[needs: reply] operator wants an answer"))
        [row] = self.store.messages()
        self.assertEqual((row["body"], row["needs"]), ("Ready?", "reply"))
        self.assertEqual(self.request(self.handler, "/api/send", method="POST",
                                      body={"pid": 500, "created": proc["created"], "text": "x", "needs": "now"})[0], 400)
