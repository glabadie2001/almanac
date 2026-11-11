"""Slice O: the orchestrator role, `may_restart`, and a restart that cannot leave the machine without a hub: only
under a launcher, never with a headless Claude agent alive, only after a preflight of the code on disk passes, and
the exit comes after the answer. The mesh client waits out a door that is down."""
import json
import os
import sqlite3
import urllib.error
from pathlib import Path
from unittest import mock

from support import TOKEN, Fixture
from test_server_fixes import MeshRig

from almanac import door, mcp_mesh, web
from almanac.__main__ import check
from almanac.store import Store

LAUNCHED = {door.LAUNCHER_ENV: "1"}


class RoleTests(Fixture):
    def test_the_flag_round_trips_and_the_orchestrator_is_seeded(self):
        s = Store(self.root / "a.db")
        self.addCleanup(s.close)
        orch = s.role("orchestrator-claude")
        self.assertEqual((orch["may_restart"], orch["may_spawn"], orch["may_page_operator"], orch["workspace_mode"],
                          orch["model"]), (True, True, True, "shared", "opus"))
        self.assertFalse(s.role("architect-claude")["may_restart"])
        self.assertTrue(s.upsert_role(dict(s.role("implementer-claude"), may_restart="on"))["may_restart"])
        kept = {k: v for k, v in s.role("implementer-claude").items() if k != "may_restart"}
        self.assertTrue(s.upsert_role(dict(kept, title="Builder"))["may_restart"], "absent keeps it")
        self.assertFalse(s.upsert_role(dict(kept, may_restart=False))["may_restart"])

    def test_a_v9_database_gains_the_orchestrator_once(self):
        path = self.root / "b.db"
        s = Store(path)
        s.delete_role("orchestrator-claude")
        s.db.execute("update meta set value = '9' where key = 'schema'")
        s.db.commit()
        s.close()
        s = Store(path)
        self.assertIsNotNone(s.role("orchestrator-claude"), "added by the v10 migration")
        s.delete_role("orchestrator-claude")
        s.close()
        s = Store(path)
        self.addCleanup(s.close)
        self.assertIsNone(s.role("orchestrator-claude"), "an operator's delete sticks")


class RestartTests(MeshRig):
    def setUp(self):
        super().setUp()
        for name, role in (("arch", "orchestrator-claude"), ("impl", "implementer-claude")):
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id="sq-1", role_id=role,
                                    provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=0)
        self.order = []
        self.service.on_exit = lambda: self.order.append("exit")
        self.service.preflight = mock.Mock(return_value=(True, "almanac check: ok"))
        order = self.order

        class Spy(self.handler):
            def _send(self, code, doc):
                order.append(code)
                return super()._send(code, doc)
        self.spy = Spy

    def restart(self, who="arch", env=LAUNCHED):
        token = web.agent_token(TOKEN, who, f"sess-{who}") if who else TOKEN
        self.service.launcher = bool(env)
        return self.request(self.spy, "/api/mesh/restart", token=token, method="POST", body={})

    def test_a_role_without_the_flag_is_refused(self):
        self.assertEqual(self.restart("impl")[0], 403)
        self.assertNotIn("exit", self.order)

    def test_outside_a_launcher_nothing_restarts(self):
        status, _, res = self.restart(env={})
        self.assertEqual(status, 409)
        self.assertIn("run.ps1", res["error"])
        self.service.preflight.assert_not_called()

    def test_a_live_headless_claude_agent_refuses_it(self):
        self.service.headless_procs = {"sess-impl": mock.Mock(**{"poll.return_value": None}),
                                       "sess-gone": mock.Mock(**{"poll.return_value": 0})}
        status, _, res = self.restart()
        self.assertEqual(status, 409)
        self.assertIn("headless Claude agents would end with Almanac: impl", res["error"])
        self.service.preflight.assert_not_called()

    def test_a_failing_preflight_returns_its_output_and_does_not_exit(self):
        self.service.preflight.return_value = (False, "ImportError: no module named nope")
        status, _, res = self.restart(who=None)
        self.assertEqual((status, res["output"]), (409, "ImportError: no module named nope"))
        self.assertNotIn("exit", self.order)
        self.assertEqual(self.heard(), set(), "nobody was told")

    def test_success_answers_tells_the_squads_then_exits_once(self):
        status, _, res = self.restart()
        self.assertEqual((status, res["code"]), (200, door.RESTART_CODE))
        self.assertEqual(self.order, [200, "exit"], "the exit comes after the answer, once")
        self.assertEqual(self.service.exit_code, door.RESTART_CODE)
        self.assertEqual(self.heard(), {"arch", "impl", "other"}, "every live squad hears it, from the hub")
        self.assertIn("asked by arch", json.dumps(self.inbox["impl"][0]))
        act = self.store.actions(1)[0]
        self.assertEqual((act["action"], act["who"]), ("restart", "arch"))
        self.assertEqual(self.store.open_needs(), [])

    def test_the_operator_may_restart(self):
        self.assertEqual(self.restart(who=None)[0], 200)

    def test_one_restart_at_a_time_and_never_twice(self):                          # O3
        self.assertEqual(self.restart()[0], 200)
        status, _, res = self.restart()
        self.assertEqual((status, res["error"]), (409, "a restart is already under way"), "latched")
        self.assertEqual(self.order.count("exit"), 1)
        self.service.restarting = False
        with self.service.restart_lock:                     # another restart is mid-preflight
            self.assertEqual(self.restart()[0], 409)

    def test_a_headless_agent_started_during_the_preflight_stops_it(self):         # O3
        def started(*_):
            self.service.headless_procs = {"sess-impl": mock.Mock(**{"poll.return_value": None})}
            return True, "almanac check: ok"
        self.service.preflight.side_effect = started
        status, _, res = self.restart()
        self.assertEqual(status, 409)
        self.assertIn("impl", res["error"])
        self.assertNotIn("exit", self.order)
        self.assertFalse(self.service.restarting)


class CheckTests(Fixture):
    def test_check_passes_on_this_tree_and_never_touches_the_live_database(self):
        state = self.root / "state"
        s = Store(state / "almanac.db")
        s.db.execute("update meta set value = '9' where key = 'schema'")
        s.db.commit()
        s.close()
        self.assertEqual(check(state), 0)
        live = sqlite3.connect(state / "almanac.db")
        self.addCleanup(live.close)
        self.assertEqual(live.execute("select value from meta where key = 'schema'").fetchone()[0], "9")

    def test_check_fails_on_a_module_that_does_not_import(self):
        self.assertEqual(check(self.root, modules=["almanac.no_such_module"]), 1)

    def test_check_fails_on_a_config_the_relaunch_could_not_start_with(self):      # O2
        for text in ("{not json", '{"interval": 5}', '{"port": 0}', '{"nope": 1}'):
            config = self.root / "config.json"
            config.write_text(text, encoding="utf-8")
            self.assertEqual(check(self.root / "state", config), 1, text)
        from almanac import __main__ as entry
        with mock.patch.object(entry, "load", side_effect=RuntimeError("config.load broke in the merge")):
            self.assertEqual(check(self.root / "state", self.root / "absent.json"), 1)
        with mock.patch.object(entry, "build_service", side_effect=RuntimeError("Prices broke")):
            self.assertEqual(check(self.root / "state", self.root / "absent.json"), 1)

    def test_the_preflight_checks_the_config_almanac_started_with(self):            # O2
        store = Store(self.root / "s" / "almanac.db")
        self.addCleanup(store.close)
        service = door.Service(store, self.root, state=self.root / "s")
        service.config_path = self.root / "bad.json"
        service.config_path.write_text('{"interval": 5}', encoding="utf-8")
        ok, out = service.preflight()
        self.assertFalse(ok)
        self.assertIn("interval", out)

    def test_the_launcher_mark_comes_off_the_environment(self):                     # O4
        from almanac.__main__ import take_launcher_mark
        with mock.patch.dict(os.environ, LAUNCHED):
            self.assertTrue(take_launcher_mark())
            self.assertNotIn(door.LAUNCHER_ENV, os.environ, "agents and resumed consoles never inherit it")
            self.assertFalse(take_launcher_mark())

    def test_the_preflight_runs_the_check_in_a_subprocess(self):
        store = Store(self.root / "s" / "almanac.db")
        self.addCleanup(store.close)
        ok, out = door.Service(store, self.root, state=self.root / "s").preflight()
        self.assertTrue(ok, out)
        self.assertIn("almanac check: ok", out)


class RetryTests(Fixture):
    def refused(self):
        return urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    def test_a_refused_connection_is_retried_until_the_door_is_back(self):
        with mock.patch.object(mcp_mesh, "http_api", side_effect=[self.refused(), ConnectionResetError(), {"ok": 1}]) as api, \
                mock.patch.object(mcp_mesh.time, "sleep") as sleep:
            self.assertEqual(mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/wait"), {"ok": 1})
        self.assertEqual((api.call_count, sleep.call_count), (3, 2))

    def test_a_reset_is_retried_only_for_a_get(self):
        reset = urllib.error.URLError(ConnectionResetError(10054, "reset"))
        for method, calls in (("POST", 1), ("GET", 2)):
            with mock.patch.object(mcp_mesh, "http_api", side_effect=[reset, {"ok": 1}]) as api,                     mock.patch.object(mcp_mesh.time, "sleep"):
                if calls == 1:
                    with self.assertRaises(urllib.error.URLError):
                        mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/spawn", method=method, body={})
                else:
                    self.assertEqual(mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/wait", method=method), {"ok": 1})
            self.assertEqual(api.call_count, calls, method)
        with mock.patch.object(mcp_mesh, "http_api", side_effect=[ConnectionResetError(), {"ok": 1}]) as api:
            with self.assertRaises(ConnectionResetError):
                mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/restart", method="POST", body={})
        self.assertEqual(api.call_count, 1, "a raw reset on a POST too")

    def test_a_wait_open_longer_than_the_window_still_waits_out_a_restart(self):  # O1
        clock = [1000.0]
        refused = self.refused()

        def call(*a, **k):
            if api.call_count == 1:
                clock[0] += 120                     # the mesh_wait was open for two minutes when the door went away
                raise ConnectionAbortedError(10053, "aborted")
            if api.call_count == 2:
                raise refused                       # still down
            return {"ok": 1}
        with mock.patch.object(mcp_mesh, "http_api", side_effect=call) as api,                 mock.patch.object(mcp_mesh.time, "monotonic", side_effect=lambda: clock[0]),                 mock.patch.object(mcp_mesh.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            self.assertEqual(mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/wait"), {"ok": 1})
        self.assertEqual(api.call_count, 3)

    def test_an_aborted_post_is_not_retried(self):                                  # O1
        with mock.patch.object(mcp_mesh, "http_api", side_effect=ConnectionAbortedError(10053, "aborted")) as api:
            with self.assertRaises(ConnectionAbortedError):
                mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/spawn", method="POST", body={})
        self.assertEqual(api.call_count, 1)

    def test_a_refused_post_is_retried(self):
        with mock.patch.object(mcp_mesh, "http_api", side_effect=[self.refused(), {"ok": 1}]) as api,                 mock.patch.object(mcp_mesh.time, "sleep"):
            self.assertEqual(mcp_mesh.http_api_retrying("http://x", "t", "/api/mesh/spawn", method="POST", body={}), {"ok": 1})
        self.assertEqual(api.call_count, 2, "nothing was listening, so nothing happened")

    def test_it_gives_up_after_the_limit_and_never_retries_an_answer(self):
        with mock.patch.object(mcp_mesh, "http_api", side_effect=self.refused()), \
                mock.patch.object(mcp_mesh, "RETRY_S", 0), mock.patch.object(mcp_mesh.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                mcp_mesh.http_api_retrying("http://x", "t", "/")
        with mock.patch.object(mcp_mesh, "http_api", side_effect=RuntimeError("403")) as api:
            with self.assertRaises(RuntimeError):
                mcp_mesh.http_api_retrying("http://x", "t", "/")
        self.assertEqual(api.call_count, 1)
        dns = urllib.error.URLError(OSError("getaddrinfo failed"))
        with mock.patch.object(mcp_mesh, "http_api", side_effect=dns) as api:
            with self.assertRaises(urllib.error.URLError):
                mcp_mesh.http_api_retrying("http://x", "t", "/")
        self.assertEqual(api.call_count, 1)

    def test_the_restart_tool_and_verb(self):
        seen = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30: seen.append((path, method)) or {})
        mcp_mesh._run_cli(["restart"], client)
        self.assertEqual(seen, [("/api/mesh/restart", "POST")])


class SpawningSetTests(MeshRig):
    def test_a_spawn_that_raises_leaves_no_session_behind(self):
        from almanac import spawner
        with mock.patch.object(spawner, "_spawn_agent", side_effect=RuntimeError("git refused")):
            with self.assertRaises(RuntimeError):
                spawner.spawn_agent(self.service, almanac_url="http://x", token=TOKEN, cwd=str(self.root))
        self.assertEqual(self.service.spawning, set())
