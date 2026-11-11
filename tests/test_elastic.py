"""Slice S, elastic squads: an architect adds members of the role the operator chose, within the operator's limit;
the operator adds members freely; refusals say why; the newcomer knows its squad and the squad hears who joined."""
import sqlite3
import threading
from pathlib import Path
from unittest import mock

from support import TOKEN, Fixture, runner_probe
from test_server_fixes import MeshRig

from almanac import spawner, web
from almanac.store import Store

TASK = "Build slice 6: telemetry by provider and model in store.py, with tests."


class ElasticRig(MeshRig):
    """MeshRig's live peers: arch (sq-1, may spawn), impl (sq-1, may not), other (sq-2, may spawn, limit 0)."""

    def setUp(self):
        super().setUp()
        self.service.probe = runner_probe(self.root, claude=True, gemini=False)
        self.service._runners = None
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.store.create_squad("sq-1", "alpha", str(self.ws), "Ship the roadmap.", spawn_role="implementer-claude",
                                spawn_limit=2)
        self.store.create_squad("sq-2", "beta", str(self.ws), "Other work.", spawn_role="implementer-claude", spawn_limit=0)
        for name, squad, role in (("arch", "sq-1", "architect-claude"), ("impl", "sq-1", "implementer-claude"),
                                  ("other", "sq-2", "architect-claude")):
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id=squad, role_id=role,
                                    provider="claude", model=None, effort=None, cwd=str(self.ws), worktree_path=None, pid=1)
        self.argv = []
        self.dies = set()                 # launch numbers (1 = the first Popen) whose process exits at once

        def popen(argv, **kwargs):
            self.argv.append(list(argv))
            return mock.MagicMock(pid=9000 + len(self.argv), **{"poll.return_value": 1 if len(self.argv) in self.dies else None})

        # the test folders sit inside Almanac's own checkout: a real worktree would branch the live repo
        for target, attr, fake in ((spawner.subprocess, "Popen", popen), (spawner, "ensure_worktree", lambda repo, *a, **k: repo)):
            patcher = mock.patch.object(target, attr, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def spawn(self, agent="arch", **body):
        return self.request(self.handler, "/api/mesh/spawn", token=web.agent_token(TOKEN, agent) if agent else TOKEN,
                            method="POST", body={"task": TASK, **body})

    def used(self, squad="sq-1"):
        return self.store.squad(squad)["spawn_used"]


class LimitTests(ElasticRig):
    def test_the_limit_holds_and_only_successful_spawns_count(self):
        status, _, res = self.spawn()
        self.assertEqual((status, res["agent_name"], res["role_id"], res["spawned_by"]), (200, "impl-claude", "implementer-claude", "arch"))
        self.dies.add(2)
        status, _, res = self.spawn()
        self.assertEqual(status, 409, "a member that dies at launch is a failure")
        self.assertEqual(self.used(), 1, "and does not use a slot")
        self.assertEqual(self.spawn(name="impl-three")[0], 200)
        status, _, res = self.spawn()
        self.assertEqual((status, res["error"]), (403, "2/2 used; ask the operator to raise the limit"))
        [refused] = [a for a in self.store.actions() if a["detail"].startswith("mesh_spawn refused")]
        self.assertEqual((refused["who"], refused["ok"]), ("arch", False))

    def test_two_calls_for_the_last_slot(self):
        self.store.update_squad("sq-1", spawn_limit=1)
        results = []
        threads = [threading.Thread(target=lambda n=n: results.append(self.spawn(name=f"racer-{n}")[0])) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 403])
        self.assertEqual(self.used(), 1)

    def test_an_agent_cannot_choose_model_workspace_permissions_or_squad(self):
        # slice X: a model or effort outside what the role allows is refused before anything starts
        status, _, res = self.spawn(model="evil-model")
        self.assertEqual(status, 400)
        self.assertIn("allows", res["error"])
        self.assertEqual((self.argv, self.used()), ([], 0))
        self.assertEqual(self.spawn(effort="max")[0], 400)
        status, _, res = self.spawn(permission_mode="bypass", workspace_mode="shared", squad_id="sq-2")
        self.assertEqual(status, 200)
        row = next(s for s in self.store.spawned() if s["agent_name"] == res["agent_name"])
        self.assertEqual((row["role_id"], row["squad_id"]), ("implementer-claude", "sq-1"))
        argv = " ".join(self.argv[-1])
        for word in ("evil-model", "bypass", "--dangerously-skip-permissions"):
            self.assertNotIn(word, argv)

    def test_another_squads_agent_cannot_spend_this_squads_slots(self):
        status, _, res = self.spawn("other", squad_id="sq-1")
        self.assertEqual(status, 403)
        self.assertIn("sq-2", res["error"])
        self.assertEqual(self.used(), 0)

    def test_refusals_say_why(self):
        reasons = {}
        reasons["role"] = self.spawn("impl")[2]["error"]
        reasons["limit 0"] = self.spawn("other")[2]["error"]
        self.service.probe = runner_probe(self.root / "bare", claude=False, gemini=False)
        self.service._runners = None
        reasons["runner"] = self.spawn()[2]["error"]
        self.store.db.execute("update squads set status = 'ended' where id = 'sq-1'")
        self.store.db.commit()
        reasons["ended"] = self.spawn()[2]["error"]
        self.assertIn("may not add squad members", reasons["role"])
        self.assertIn("limit 0", reasons["limit 0"])
        self.assertIn("runner not found", reasons["runner"])
        self.assertIn("is ended", reasons["ended"])
        self.assertEqual(len(set(reasons.values())), 4)
        self.assertEqual(self.request(self.handler, "/api/mesh/spawn", token=web.agent_token(TOKEN, "arch"),
                                      method="POST", body={"task": "too short"})[0], 400)


class AnyRoleTests(ElasticRig):
    """S2: spawn_role '*' lets the architect add any defined role; a fixed role refuses another one by name."""

    def test_a_fixed_role_refuses_another_by_name(self):
        status, _, res = self.spawn(role="architect-claude")
        self.assertEqual((status, res["error"]), (400, "squad sq-1 may add only implementer-claude, not architect-claude"))
        self.assertEqual(self.spawn(role="implementer-claude")[0], 200, "naming the allowed role is fine")

    def test_any_role_needs_a_defined_role_and_uses_its_definition(self):
        self.store.update_squad("sq-1", spawn_role="*", spawn_limit=3)
        status, _, res = self.spawn()
        self.assertEqual(status, 400)
        self.assertIn("name the role to add", res["error"])
        self.assertIn("reviewer-claude", res["error"], "the refusal lists the role ids")
        status, _, res = self.spawn(role="no-such-role")
        self.assertEqual(status, 400)
        self.assertIn("no role 'no-such-role'", res["error"])
        self.assertEqual(self.spawn(role="reviewer-claude", model="evil-model")[0], 400, "slice X: outside the role")
        status, _, res = self.spawn(role="reviewer-claude", permission_mode="bypass")
        self.assertEqual((status, res["role_id"], res["agent_name"]), (200, "reviewer-claude", "rev-claude"))
        row = next(s for s in self.store.spawned() if s["agent_name"] == "rev-claude")
        self.assertNotEqual(row["model"], "evil-model", "the model comes from the role, not the agent")
        argv = " ".join(self.argv[-1])
        for word in ("evil-model", "bypass", "--dangerously-skip-permissions"):
            self.assertNotIn(word, argv)

    def test_the_limit_counts_every_role(self):
        self.store.update_squad("sq-1", spawn_role="*", spawn_limit=2)
        self.assertEqual(self.spawn(role="reviewer-claude")[0], 200)
        self.assertEqual(self.spawn(role="implementer-claude")[0], 200)
        status, _, res = self.spawn(role="reviewer-claude")
        self.assertEqual((status, res["error"]), (403, "2/2 used; ask the operator to raise the limit"))

    def test_any_role_in_the_charter_and_the_store(self):
        self.assertEqual(self.store.update_squad("sq-2", spawn_role="*", spawn_limit=4)["spawn_role"], "*")
        grow = spawner._grow_line(self.service, self.store.role("architect-claude"), "sq-2")
        self.assertEqual(grow, dict(role_title=None, any=True, limit=4))
        charter = spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="R", role_prompt="p",
                                             almanac_url="u", grow=grow)
        self.assertIn('adds one member of any defined role', charter)
        self.assertIn('role="<role id>"', charter)
        _, _, mesh = self.request(self.handler, "/api/mesh")
        self.assertEqual(next(s for s in mesh["squads"] if s["id"] == "sq-2")["spawn_role"], "*")


class OperatorTests(ElasticRig):
    def test_the_operator_adds_freely_and_overrides(self):
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST", body={
            "squad_id": "sq-1", "role_id": "reviewer-claude", "task": TASK, "model": "opus", "workspace_mode": "shared"})
        self.assertEqual((status, res["agent_name"], res["spawned_by"]), (200, "rev-claude", None))
        self.assertEqual(self.used(), 0, "the operator's adds never use the agents' limit")
        self.assertEqual(self.spawn(agent=None, squad_id="sq-1", role_id="implementer-claude")[0], 200)
        self.assertEqual(self.used(), 0)

    def test_the_operator_sets_the_limit(self):
        status, _, res = self.request(self.handler, "/api/squad", method="POST", body={"id": "sq-2", "spawn_limit": 3})
        self.assertEqual((status, res["squad"]["spawn_limit"], res["squad"]["spawn_role"]), (200, 3, "implementer-claude"))
        self.assertEqual(self.request(self.handler, "/api/squad", method="POST", body={"id": "sq-2", "spawn_limit": 21})[0], 400)
        self.assertEqual(self.request(self.handler, "/api/squad", method="POST", body={"id": "sq-2", "spawn_role": "nope"})[0], 400)
        self.assertEqual(self.request(self.handler, "/api/squad", method="POST", body={"id": "nope", "spawn_limit": 1})[0], 404)
        _, _, mesh = self.request(self.handler, "/api/mesh")
        beta = next(s for s in mesh["squads"] if s["id"] == "sq-2")
        self.assertEqual((beta["spawn_limit"], beta["spawn_used"]), (3, 0))

    def test_agent_tokens_cannot_reach_the_operators_routes(self):
        agent = web.agent_token(TOKEN, "arch")
        self.assertEqual(self.request(self.handler, "/api/squad", token=agent, method="POST", body={"id": "sq-1", "spawn_limit": 20})[0], 403)
        self.assertEqual(self.request(self.handler, "/api/squad/member", token=agent, method="POST",
                                      body={"squad_id": "sq-1", "role_id": "implementer-claude", "task": TASK})[0], 403)
        self.assertEqual(self.store.squad("sq-1")["spawn_limit"], 2)


class JoinTests(ElasticRig):
    def test_the_newcomer_knows_its_squad_and_the_squad_hears_the_join(self):
        status, _, res = self.spawn()
        self.assertEqual(status, 200)
        charter = next((self.state / "spawns").glob(f"{res['agent_name']}-*/charter.md")).read_text(encoding="utf-8")
        self.assertIn("**`arch`**", charter)
        self.assertIn("**`impl`**", charter)
        prompt = self.argv[-1][-1]
        for words in ("## Squad Mission (alpha)", "Ship the roadmap.", TASK, "added to the running squad by arch",
                      f"branch `almanac/sq-1/{res['agent_name']}`"):
            self.assertIn(words, prompt)
        heard = [m["body"] for m in self.inbox["impl"]]
        self.assertTrue(any(b.startswith("[impl-claude joined as Implementer (Claude), added by arch, on sonnet at high]") for b in heard), heard)
        self.assertFalse(self.inbox["other"], "another squad hears nothing")

    def test_only_a_role_that_may_spawn_in_a_squad_with_slots_gets_the_charter_line(self):
        role = lambda rid: self.store.role(rid)   # noqa: E731
        self.assertEqual(spawner._grow_line(self.service, role("architect-claude"), "sq-1"),
                         dict(role_title="Implementer (Claude)", limit=2))
        self.assertIsNone(spawner._grow_line(self.service, role("implementer-claude"), "sq-1"))
        self.assertIsNone(spawner._grow_line(self.service, role("architect-claude"), "sq-2"), "limit 0")
        line = spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="R", role_prompt="p",
                                          almanac_url="u", grow=dict(role_title="Implementer (Claude)", limit=2))
        self.assertIn("8. **Grow the squad**: `mesh_spawn(task=...)` adds one Implementer (Claude) to this squad, up to 2", line)
        self.assertNotIn("Grow the squad", spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="R",
                                                                      role_prompt="p", almanac_url="u"))

    def test_a_name_already_live_is_refused(self):
        self.assertEqual(self.spawn(name="impl")[0], 400)

    def test_the_context_stream_names_role_and_squad(self):
        status, _, doc = self.request(self.handler, "/api/mesh/context?peer=arch")
        self.assertEqual((status, doc["live"]), (200, True))
        self.assertEqual((doc["peer"]["agent_role"], doc["peer"]["squad_id"]), ("architect-claude", "sq-1"))


class MigrationV7Tests(Fixture):
    def test_a_v6_database_gains_the_elastic_columns(self):
        path = self.root / "v6.db"
        Store(path).close()
        db = sqlite3.connect(path)
        for table, column in (("spawned_agents", "spawned_by"), ("squads", "spawn_role"), ("squads", "spawn_limit"),
                              ("roles", "may_spawn")):
            db.execute(f"alter table {table} drop column {column}")
        db.execute("insert into squads (id, name, cwd, created_ts) values ('old', 'old', '.', 1)")
        db.execute("insert into spawned_agents (session_id, agent_name, squad_id, provider, cwd, created_ts) "
                   "values ('s', 'a', 'old', 'claude', '.', 1)")
        db.execute("update meta set value = '6' where key = 'schema'")
        db.commit()
        db.close()
        s = Store(path)
        self.addCleanup(s.close)
        old = s.squad("old")
        self.assertEqual((old["spawn_role"], old["spawn_limit"], old["spawn_used"]), (None, 0, 0))
        self.assertIsNone(s.spawned()[0]["spawned_by"])
        self.assertEqual({r["id"] for r in s.roles() if r["may_spawn"]},
                         {"architect-claude", "architect-gemini", "orchestrator-claude"})   # v10 adds the orchestrator
        base = dict(id="scribe", provider="claude", system_prompt="write")
        self.assertTrue(s.upsert_role(dict(base, may_spawn=True))["may_spawn"])
        self.assertTrue(s.upsert_role(base)["may_spawn"], "left out keeps it")
