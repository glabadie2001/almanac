"""Slice X: elastic model and effort. A role lists the models and efforts a spawner may pick besides its own
defaults; an agent's mesh_spawn picks inside that set (or gets a 400 saying what is allowed) and says why; the choice
is in the join notice, the action log and the member's T; the roster shows the sets. The operator's adds keep their
free overrides."""
import json

from support import TOKEN
from test_elastic import TASK, ElasticRig

from almanac import mcp_mesh, spawner, web
from almanac.store import Store

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


class RoleSetTests(ElasticRig):
    def test_seeded_sets_round_trip_and_alternatives_only(self):
        impl = self.store.role("implementer-claude")
        self.assertEqual((impl["model"], impl["models"], impl["efforts"]), ("sonnet", [HAIKU, SONNET, OPUS], ["low", "medium", "high"]))
        self.assertEqual((self.store.role("reviewer-claude")["models"], self.store.role("architect-claude")["efforts"]), ([], []))
        rev = self.store.upsert_role(dict(self.store.role("reviewer-claude"), models="opus-5.5, claude-opus-5-5,\nhaiku-5.5",
                                          efforts=["med", "low"]))
        self.assertEqual((rev["models"], rev["efforts"]), ([OPUS, HAIKU], ["medium", "low"]),
                         "normalized, deduped, and the default need not be listed: the lists only add choices")
        kept = {k: v for k, v in rev.items() if k not in ("models", "efforts")}
        self.assertEqual(self.store.upsert_role(dict(kept, title="Reviewer"))["models"], [OPUS, HAIKU], "absent keeps")
        with self.assertRaises(ValueError):
            self.store.upsert_role(dict(kept, efforts=["turbo"]))
        self.assertEqual(self.store.upsert_role(dict(kept, models=[]))["models"], [], "an empty list clears")

    def test_an_existing_database_keeps_its_roles(self):
        path = self.root / "old.db"
        s = Store(path)
        s.db.execute("update roles set models = '[]', efforts = '[]'")
        s.db.execute("update meta set value = '9' where key = 'schema'")
        s.db.commit()
        s.close()
        s = Store(path)
        self.addCleanup(s.close)
        self.assertEqual(s.role("implementer-claude")["models"], [], "the operator's roles are not reseeded")


class AgentChoiceTests(ElasticRig):
    def spawn_as_arch(self, **body):
        return self.request(self.handler, "/api/mesh/spawn", token=web.agent_token(TOKEN, "arch", "sess-arch"),
                            method="POST", body={"task": TASK, **body})

    def flag(self, name):
        argv = self.argv[-1]
        return argv[argv.index(name) + 1]

    def test_inside_the_sets_it_runs_on_the_pick_and_says_why(self):
        status, _, res = self.spawn_as_arch(model="haiku-5.5", effort="low", why="rote rename\nacross two files " + "x" * 300)
        self.assertEqual(status, 200, res)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), (HAIKU, "low"))
        data = json.loads(self.store.db.execute("select data from objects where id = 1").fetchone()[0])
        self.assertEqual((data["model"], data["effort"]), (HAIKU, "low"))
        self.assertTrue(data["why"].startswith("rote rename across two files") and len(data["why"]) == 200)
        notice = next(m["body"] for m in self.store.messages() if "joined as" in m["body"])
        self.assertIn(f"on {HAIKU} at low: rote rename across two files", notice)
        act = self.store.actions(1)[0]
        self.assertIn(f"on {HAIKU} at low: rote rename", act["detail"])

    def test_the_defaults_are_always_allowed(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), models=[OPUS], efforts=["medium"]))
        self.assertEqual(self.spawn_as_arch(model="sonnet", effort="high")[0], 200)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), ("sonnet", "high"))
        self.assertEqual(self.spawn_as_arch(model="opus-5.5", effort="medium")[0], 200)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), (OPUS, "medium"))

    def test_outside_the_sets_it_is_refused_naming_what_is_allowed(self):
        status, _, res = self.spawn_as_arch(model="claude-mythos-9")
        self.assertEqual(status, 400)
        self.assertIn(f"sonnet, {HAIKU}, {SONNET}, {OPUS}", res["error"])
        status, _, res = self.spawn_as_arch(effort="max")
        self.assertEqual(status, 400)
        self.assertIn("allows high, low, medium", res["error"])
        self.assertEqual((self.argv, self.used(), self.store.objects_rev()), ([], 0, 0), "nothing started, nothing filed")

    def test_an_effort_written_into_the_model_is_a_pick_too(self):                # review of d29c103
        self.assertEqual(self.spawn_as_arch(model="claude-haiku-5-5-low")[0], 200)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), (HAIKU, "low"))
        status, _, res = self.spawn_as_arch(model="sonnet-max")
        self.assertEqual(status, 400)
        self.assertIn("effort 'sonnet-max'", res["error"])
        self.assertEqual(self.spawn_as_arch(model="Claude-Haiku-5.5", effort="MEDIUM")[0], 200, "case does not matter")
        self.assertEqual((self.flag("--model"), self.flag("--effort")), (HAIKU, "medium"))

    def test_the_operators_choice_is_recorded_normalized(self):                    # review of d29c103
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST", body={
            "squad_id": "sq-1", "role_id": "implementer-claude", "task": TASK, "model": "haiku-5.5", "effort": "med"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["choice"], dict(model=HAIKU, effort="medium", why=None))
        data = json.loads(self.store.db.execute("select data from objects order by id desc limit 1").fetchone()[0])
        self.assertEqual(data, dict(model=HAIKU, effort="medium"))

    def test_empty_sets_run_the_role_as_before(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), models=[], efforts=[]))
        self.assertEqual(self.spawn_as_arch(model=HAIKU)[0], 400)
        status, _, res = self.spawn_as_arch()
        self.assertEqual(status, 200, res)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), ("sonnet", "high"))

    def test_the_operator_keeps_free_overrides(self):
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST", body={
            "squad_id": "sq-1", "role_id": "implementer-claude", "task": TASK, "model": "claude-mythos-9", "effort": "max"})
        self.assertEqual(status, 200, res)
        self.assertEqual((self.flag("--model"), self.flag("--effort")), ("claude-mythos-9", "max"))

    def test_the_roster_shows_the_sets(self):
        def api(path, method="GET", body=None, timeout=30.0):
            return self.request(self.handler, path, token=web.agent_token(TOKEN, "arch", "sess-arch"), method=method, body=body)[2]
        roles = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch", api_fn=api).call_tool("mesh_roster")["roles"]
        impl = next(r for r in roles if r["id"] == "implementer-claude")
        self.assertEqual((impl["model"], impl["effort"], impl["models"], impl["efforts"]),
                         ("sonnet", "high", [HAIKU, SONNET, OPUS], ["low", "medium", "high"]))

    def test_the_charter_carries_the_rubric(self):
        charter = spawner.build_mesh_charter(agent_name="lead", provider="claude", role_title="Architect", role_prompt="p",
                                             almanac_url="http://x", state_dir=None, squad_id="sq-1", grow=dict(role_title="Implementer", limit=2))
        self.assertIn("Raise effort before raising the model", charter)
        self.assertIn("Haiku for mechanical work that fails loudly", charter)

    def test_the_orchestrators_handle(self):
        squad = self.store.squad("sq-1")
        self.assertEqual(spawner.free_handle(self.service, squad, self.store.role("orchestrator-claude")), "orch-claude")
