"""Backlog tasks (a task of a project, no squad yet) and the operator's one-click dispatch of a squad on one:
`POST /api/mesh/object {backlog}`, `GET /api/mesh/objects?backlog=1`, `POST /api/squads/dispatch {ref}`."""
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from support import TOKEN
from test_elastic import ElasticRig

from almanac import mcp_mesh, spawner, web

ROSTER = [{"role": "architect-claude", "name": "lead"}, {"role": "implementer-claude"}]


class BacklogRig(ElasticRig):
    def setUp(self):
        super().setUp()
        # the rig fakes Popen for every caller, git included: ProjectRootTests checks the real one
        patcher = mock.patch.object(spawner, "project_root", side_effect=lambda cwd: str(Path(cwd).resolve()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = str(self.ws.resolve())
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        self.store.create_squad("sq-3", "gamma", str(self.elsewhere), "Other project.")
        self.store.record_spawn(agent_name="far", session_id="sess-far", squad_id="sq-3", role_id="architect-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.elsewhere), worktree_path=None,
                                pid=1)

    def post(self, path, token=TOKEN, **body):
        return self.request(self.handler, path, token=token, method="POST", body=body)

    def backlog(self, token=TOKEN, **body):
        body.setdefault("title", "Build the thing")
        if token == TOKEN:
            body.setdefault("project", str(self.ws))
        return self.post("/api/mesh/object", token, backlog=True, **body)

    def listed(self, token=TOKEN, **query):
        status, _, res = self.request(self.handler, f"/api/mesh/objects?{urllib.parse.urlencode(dict(query, backlog=1))}",
                                      token=token)
        self.assertEqual(status, 200, res)
        return [o["ref"] for o in res["objects"]]

    def dispatch(self, ref, token=TOKEN):
        return self.post("/api/squads/dispatch", token, ref=ref)


class BacklogTests(BacklogRig):
    def test_the_operator_files_a_backlog_task_with_its_squad(self):
        status, _, res = self.backlog(body="the body", roster=ROSTER, spawn_role="implementer-claude", spawn_limit=3)
        self.assertEqual(status, 200, res)
        o = res["object"]
        self.assertEqual((o["ref"], o["squad_id"], o["status"], o["owner_name"], o["project"]),
                         ("T-1", None, "queued", None, self.project))
        self.assertEqual(o["data"], dict(roster=ROSTER, spawn_role="implementer-claude", spawn_limit=3))
        self.assertEqual(self.listed(), ["T-1"])
        self.assertEqual(self.listed(project=str(self.ws)), ["T-1"])
        self.assertEqual(self.listed(project=str(self.elsewhere)), [])
        self.assertEqual(self.request(self.handler, "/api/mesh/objects?squad=sq-1", token=TOKEN)[2]["objects"], [],
                         "not in any squad's plan")

    def test_bad_backlog_filings_are_refused(self):
        cases = [dict(roster=[{"role": "ghost"}]), dict(roster=[]), dict(roster="architect-claude"),
                 dict(roster=[{"name": "x"}]), dict(spawn_role="ghost"), dict(spawn_limit=21), dict(owner="impl"),
                 dict(parent="T-1"), dict(status="in progress"), dict(project=str(self.root / "nowhere"))]
        for body in cases:
            self.assertEqual(self.backlog(**body)[0], 400, body)
        self.assertEqual(self.post("/api/mesh/object", squad="sq-1", title="x", roster=ROSTER)[0], 400,
                         "a roster is for a backlog task")
        self.assertEqual(self.store.objects_rev(), 0, "nothing was filed")

    def test_an_agent_files_follow_up_work_in_its_projects_backlog(self):
        arch = web.agent_token(TOKEN, "arch", "sess-arch")
        status, _, res = self.backlog(arch, title="Follow-up: tidy the store", files=["almanac/store.py"])
        self.assertEqual(status, 200, res)
        self.assertEqual((res["object"]["project"], res["object"]["squad_id"], res["object"]["created_by_name"]),
                         (self.project, None, "arch"))
        self.assertEqual(self.listed(arch), ["T-1"])
        self.assertEqual(self.listed(web.agent_token(TOKEN, "far", "sess-far")), [], "another project's backlog")
        self.assertEqual(self.request(self.handler, "/api/mesh/object?ref=T-1", token=arch)[0], 200)
        self.assertEqual(self.request(self.handler, "/api/mesh/object?ref=T-1",
                                      token=web.agent_token(TOKEN, "far", "sess-far"))[0], 403)
        self.assertEqual(self.post("/api/mesh/object/log", arch, ref="T-1", owner="impl")[0], 400,
                         "no owner before dispatch")

    def test_tools_and_cli_carry_the_backlog_fields(self):
        seen = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30: seen.append((path, body)) or {})
        mcp_mesh._run_cli(["file", "Later", "--backlog", "--roster", "architect-claude:lead", "reviewer-gemini",
                           "--spawn-role", "*", "--spawn-limit", "0"], client)
        seen[-1][1].pop("squad", None)                       # set from the environment when run inside a squad
        self.assertEqual(seen[-1][1], dict(kind="task", title="Later", backlog=True, spawn_role="*", spawn_limit=0,
                                           roster=[{"role": "architect-claude", "name": "lead"},
                                                   {"role": "reviewer-gemini"}]))
        mcp_mesh._run_cli(["objects", "--backlog"], client)
        self.assertIn("backlog=1", seen[-1][0])


class DispatchTests(BacklogRig):
    def test_an_agent_cannot_dispatch(self):
        self.backlog(roster=ROSTER)
        for name in ("arch", "impl"):
            self.assertEqual(self.dispatch("T-1", web.agent_token(TOKEN, name, f"sess-{name}"))[0], 403)
        self.assertEqual(self.argv, [], "nobody was started")
        self.assertIsNone(self.store.object_row(1)["squad_id"])

    def test_dispatch_launches_the_roster_on_the_task_itself(self):
        self.backlog(title="Build the thing", body="All of it.", roster=ROSTER, spawn_role="implementer-claude",
                     spawn_limit=3, files=["almanac/door.py"])
        status, _, res = self.dispatch("T-1")
        self.assertEqual(status, 200, res)
        squad = res["squad"]
        self.assertEqual((res["dispatched"], squad["name"], squad["cwd"], squad["brief"]),
                         (True, "Build the thing", self.project, "Build the thing\n\nAll of it."))
        self.assertEqual((squad["spawn_role"], squad["spawn_limit"]), ("implementer-claude", 3))
        self.assertEqual([(a["agent_name"], a["role_id"]) for a in squad["agents"]],
                         [("lead", "architect-claude"), ("implementer-claude", "implementer-claude")])
        lead = squad["agents"][0]
        row = self.store.object_row(1)
        self.assertEqual((row["squad_id"], row["owner_session"], row["status"]), (squad["id"], lead["session_id"],
                                                                                  "in progress"))
        self.assertEqual([o["id"] for o in self.store.objects(squad_id=squad["id"])], [1], "no second task")
        self.assertEqual(self.store.objects(squad_id=None), [], "out of the backlog")
        self.assertIn("## Your Initial Assignment (`T-1`)", self.argv[0][-1])
        self.assertIn('mesh_log(ref="T-1"', self.argv[0][-1])
        log = self.store.object_log(1, 10)
        self.assertEqual([(e["kind"], e["status_to"], e["owner_to"]) for e in log],
                         [("dispatch", "in progress", "lead"), ("create", "queued", None)])
        self.assertEqual(res["object"]["owner_name"], "lead")

    def test_the_default_squad_is_an_orchestrator_and_a_gemini_reviewer(self):
        self.backlog()
        status, _, res = self.dispatch("T-1")
        self.assertEqual(status, 200, res)
        self.assertEqual([a["role_id"] for a in res["squad"]["agents"]], ["orchestrator-claude", "reviewer-gemini"])
        self.assertEqual((res["squad"]["spawn_role"], res["squad"]["spawn_limit"]), ("*", 5))

    def test_a_failed_launch_leaves_the_task_in_the_backlog(self):
        self.backlog(roster=ROSTER)
        before = self.store.object_row(1)
        self.dies.add(1)                                           # the lead exits at once
        status, _, res = self.dispatch("T-1")
        self.assertEqual((status, res["dispatched"]), (409, False))
        self.assertEqual(self.store.object_row(1), before)
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["create"])
        self.assertEqual(self.listed(), ["T-1"])

    def test_a_launch_refused_before_anyone_starts_leaves_it_too(self):
        self.backlog(roster=ROSTER)
        before = self.store.object_row(1)
        with self._swap(spawner, "launch_squad", ValueError("folder does not exist: x")):
            self.assertEqual(self.dispatch("T-1")[0], 400)
        self.assertEqual(self.store.object_row(1), before)
        self.assertEqual(self.dispatch("T-1")[0], 200, "and it can be dispatched again")

    def test_a_second_dispatch_is_refused(self):
        self.backlog(roster=ROSTER)
        self.assertEqual(self.dispatch("T-1")[0], 200)
        status, _, res = self.dispatch("T-1")
        self.assertEqual(status, 409, res)
        self.assertIn("already dispatched", res["error"])
        self.assertEqual(len(self.argv), 2, "one squad launched")

    def _swap(self, target, attr, exc):
        return mock.patch.object(target, attr, side_effect=exc)


class ProjectRootTests(unittest.TestCase):
    def test_a_project_is_the_checkout_root_or_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, plain = Path(tmp) / "repo", Path(tmp) / "plain"
            (repo / "sub").mkdir(parents=True)
            plain.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            self.assertEqual(spawner.project_root(str(repo / "sub")), str(repo.resolve()))
            self.assertEqual(spawner.project_root(str(plain)), str(plain.resolve()))
