"""T-22: respawn a task's owner on the same task with its log. Who may; the same handle, worktree and branch; the
task's owner moves; no spawn slot is used; a failed start leaves the old owner; the kickoff carries the log; the
stall escalation names the task, and closes once the fresh session works."""
import subprocess
from unittest import mock

from support import TOKEN, Fixture
from test_elastic import ElasticRig
from test_recover import T0, FakeService, iso

from almanac import door, mcp_mesh, recover, spawner, web
from almanac.store import Store

REAL_WORKTREE = spawner.ensure_worktree          # before ElasticRig patches it
REAL_POPEN = subprocess.Popen                    # ElasticRig fakes the module's Popen, which git needs too


class RespawnRig(ElasticRig):
    """ElasticRig's sq-1: arch (architect, may spawn) added impl (implementer, live as pid 500); impl owns T-1."""

    def setUp(self):
        super().setUp()
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model="claude-sonnet-5-5", effort="high", cwd=str(self.ws),
                                worktree_path=None, pid=500, spawned_by="arch")
        self.task = self.object(owner="sess-impl", title="Build the widget")
        self.store.log_object(self.task["id"], who=dict(agent_name="impl"), kind="learned",
                              body="the widget needs the flux capacitor")
        self.store.log_object(self.task["id"], who=dict(agent_name="impl"), kind="next", body="wire it into door.py")
        self.killed = []

        def kill(pid, created):
            self.killed.append(pid)
            self.machine.procs.pop(pid, None)    # gone from the next scan
            return True, "SUCCESS", {}

        patcher = mock.patch.object(self.fleet, "kill", side_effect=kill)
        patcher.start()
        self.addCleanup(patcher.stop)

    def object(self, owner=None, squad="sq-1", title="A task"):
        return self.store.create_object(kind="task", project=str(self.ws), squad_id=squad, title=title,
                                        body="Make it work.", status="in progress" if owner else "queued",
                                        owner_session=owner, parent_id=None, files=["almanac/door.py"],
                                        created_by="sess-arch", created_by_name="arch", who=dict(agent_name="arch"))

    def respawn(self, agent="arch", ref=None, **body):
        token = web.agent_token(TOKEN, agent) if agent else TOKEN
        return self.request(self.handler, "/api/mesh/object/respawn", token=token, method="POST",
                            body={"ref": ref or f"T-{self.task['id']}", **body})

    def owner(self, task=None):
        return self.store.object_row((task or self.task)["id"])["owner_session"]


class WhoMayTests(RespawnRig):
    def test_only_the_operator_or_a_squadmate_whose_role_may_spawn(self):
        status, _, res = self.respawn("impl")
        self.assertEqual(status, 403)
        self.assertIn("may not respawn", res["error"])
        status, _, res = self.respawn("other")
        self.assertEqual(status, 403, "another squad's agent")
        mine = self.object(owner="sess-arch")
        status, _, res = self.respawn("arch", ref=f"T-{mine['id']}")
        self.assertEqual(status, 403)
        self.assertIn("itself", res["error"])
        self.assertEqual((self.killed, self.argv), ([], []), "nothing ended, nothing started")
        self.assertEqual(self.respawn(None, ref=f"T-{mine['id']}")[0], 200, "the operator may respawn the lead")

    def test_a_task_with_no_owner_or_in_the_backlog_is_refused(self):
        self.assertEqual(self.respawn(ref=f"T-{self.object()['id']}")[0], 400)
        backlog = self.store.create_object(kind="task", project=str(self.ws), squad_id=None, title="later", body="",
                                           status="queued", owner_session=None, parent_id=None, files=[],
                                           created_by=None, created_by_name="operator", who=dict(agent_name="operator"))
        status, _, res = self.respawn(None, ref=f"T-{backlog['id']}")
        self.assertEqual(status, 400)
        self.assertIn("backlog", res["error"])
        self.assertEqual(self.killed, [])


class RespawnTests(RespawnRig):
    def test_the_owner_is_replaced_on_its_task_with_its_log(self):
        self.store.db.execute("insert into sessions (id, first_ts, last_ts, cwd, context) values (?, 1, 2, ?, ?)",
                              ("sess-impl", str(self.ws), 412345))
        self.store.db.commit()
        used = self.used()
        status, _, res = self.respawn()
        self.assertEqual(status, 200, res)
        self.assertEqual(self.killed, [500], "the live owner was ended")
        new = res["session_id"]
        self.assertNotEqual(new, "sess-impl")
        self.assertEqual((res["agent_name"], self.owner()), ("impl", new))
        row = self.store.spawn_by_session(new)
        self.assertEqual((row["squad_id"], row["role_id"], row["model"], row["effort"], row["spawned_by"]),
                         ("sq-1", "implementer-claude", "claude-sonnet-5-5", "high", "arch"))
        self.assertEqual(self.used(), used, "a respawn replaces a member: it takes no slot")
        argv = self.argv[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        kickoff = argv[-1]
        for text in ("Build the widget", "Make it work.", "almanac/door.py", "flux capacitor", "wire it into door.py",
                     "git status", 'mesh_log(ref="T-1", status="in review"'):
            self.assertIn(text, kickoff)
        self.assertLess(kickoff.index("flux capacitor"), kickoff.index("wire it into door.py"), "oldest first")
        entry = self.store.object_log(self.task["id"], 1)[0]
        self.assertEqual((entry["kind"], entry["agent_name"], entry["owner_to"]), ("respawn", "arch", "impl"))
        for text in ("sess-impl", new, "412,345 tokens"):
            self.assertIn(text, entry["body"])
        assigns = [m for m in self.store.messages(limit=50) if "assigned to you" in m["body"]]
        self.assertEqual(assigns, [], "the kickoff is the assignment")
        self.assertTrue(any(a["action"] == "respawn" and a["ok"] for a in self.store.actions(5)))

    def test_a_model_and_effort_can_be_changed_within_the_role(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), models=["claude-haiku-5-5"], efforts=["low"]))
        status, _, res = self.respawn(model="evil-model")
        self.assertEqual(status, 400)
        self.assertEqual((self.killed, self.argv), ([], []), "a bad pick is refused before anything ends")
        status, _, res = self.respawn(model="claude-haiku-5-5", effort="low")
        self.assertEqual(status, 200, res)
        row = self.store.spawn_by_session(res["session_id"])
        self.assertEqual((row["model"], row["effort"]), ("claude-haiku-5-5", "low"))

    def test_a_fresh_session_that_dies_leaves_the_old_owner_and_says_so(self):
        self.dies.add(1)
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertEqual(self.owner(), "sess-impl", "the task keeps its old owner")
        entry = self.store.object_log(self.task["id"], 1)[0]
        self.assertEqual(entry["kind"], "respawn")
        self.assertIn("respawn failed", entry["body"])
        self.assertTrue(res["ended"])

    def test_what_can_be_checked_is_checked_before_the_kill(self):
        self.service.probe = __import__("support").runner_probe(self.root / "bare", claude=False, gemini=False)
        self.service._runners = None
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertIn("runner", res["error"])
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.ws),
                                worktree_path=str(self.root / "gone"), pid=500, spawned_by="arch")
        self.service._runners = None
        self.service.probe = __import__("support").runner_probe(self.root, claude=True, gemini=False)
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertIn("never makes a new one", res["error"])
        self.assertEqual((self.killed, self.argv, self.owner()), ([], [], "sess-impl"))

    def test_an_owner_that_already_ended_is_just_replaced(self):
        self.machine.procs.pop(500)
        status, _, res = self.respawn()
        self.assertEqual(status, 200, res)
        self.assertEqual(self.killed, [])
        self.assertIn("was not running", self.store.object_log(self.task["id"], 1)[0]["body"])

    def test_the_kickoff_keeps_the_newest_entries_when_the_log_is_long(self):
        log = [dict(ts=T0 + i, agent_name="impl", kind="note", body=f"entry {i} " + "x" * 500) for i in range(100)]
        text = door.respawn_kickoff(self.task, log, self.store.squad("sq-1"), "impl", 20_000)
        self.assertLessEqual(len(text), 20_000)
        self.assertIn("entry 99 ", text)
        self.assertNotIn("entry 0 ", text)
        self.assertIn("older entries left out", text)

    def test_an_accepted_task_is_not_respawned(self):
        self.store.log_object(self.task["id"], who=dict(agent_name="arch"), kind="status", status="accepted")
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertIn("accepted", res["error"])
        self.assertEqual((self.killed, self.argv), ([], []))

    def test_an_owner_that_exited_inside_the_scan_cache_is_replaced(self):
        def gone(pid, created):                   # what fleet.kill says when its fresh scan no longer finds it
            self.killed.append(pid)
            return False, f"pid {pid} is gone or is now a different process; refresh", None

        self.fleet.kill.side_effect = gone
        status, _, res = self.respawn()
        self.assertEqual(status, 200, res)
        self.assertFalse(res["ended"])
        self.assertEqual(self.owner(), res["session_id"])
        self.assertIn("was not running", self.store.object_log(self.task["id"], 1)[0]["body"])

    def test_a_kill_that_fails_on_a_live_owner_changes_nothing(self):
        self.fleet.kill.side_effect = lambda pid, created: (False, "Access is denied.", {"pid": pid})
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertIn("nothing was changed", res["error"])
        self.assertEqual((self.argv, self.owner()), ([], "sess-impl"))
        self.assertNotEqual(self.store.object_log(self.task["id"], 1)[0]["kind"], "respawn")

    def test_its_own_handle_is_not_in_its_roster(self):
        status, _, res = self.respawn()
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        charter = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("**`arch`**", charter)
        self.assertNotIn("**`impl`**", charter)


class CommandLineTests(RespawnRig):
    """R1: the kickoff is cut to what the charter and arguments leave of a Windows command line, before the kill."""

    def long_charter(self, n):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), system_prompt="Implement. " + "y" * n))
        for i in range(60):
            self.store.log_object(self.task["id"], who=dict(agent_name="impl"), kind="note",
                                  body=f"entry {i} " + 'say "quoted" ' * 40)

    def test_a_long_charter_leaves_less_room_and_the_oldest_entries_go(self):
        self.long_charter(20_000)
        status, _, res = self.respawn()
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        self.assertLessEqual(len(subprocess.list2cmdline(argv)), spawner.CMDLINE_MAX)
        kickoff = argv[-1]
        self.assertIn("entry 59 ", kickoff)
        self.assertNotIn("entry 0 ", kickoff)
        self.assertIn("older entries left out", kickoff)
        self.assertIn("Make it work.", kickoff, "the task itself is never cut")

    def test_no_room_even_for_the_task_is_refused_before_the_kill(self):
        self.long_charter(31_900)
        status, _, res = self.respawn()
        self.assertEqual(status, 409)
        self.assertIn("does not fit on the command line", res["error"])
        self.assertFalse(res["ended"])
        self.assertEqual((self.killed, self.argv, self.owner()), ([], [], "sess-impl"))
        self.assertNotEqual(self.store.object_log(self.task["id"], 1)[0]["kind"], "respawn")


class WorktreeTests(RespawnRig):
    """A real git repo under .test-tmp: the fresh session opens in the old worktree as it stands."""

    def git(self, *args, cwd=None):
        with mock.patch.object(subprocess, "Popen", REAL_POPEN):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd or self.repo), *args],
                                  capture_output=True, text=True, check=True).stdout.strip()

    def test_same_worktree_and_branch_uncommitted_files_included(self):
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "first")
        self.store.create_squad("sq-3", "gamma", str(self.repo), "Build.", spawn_role="implementer-claude", spawn_limit=2)
        self.store.record_spawn(agent_name="arch", session_id="sess-arch", squad_id="sq-3", role_id="architect-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.repo), worktree_path=None, pid=100)
        with mock.patch.object(subprocess, "Popen", REAL_POPEN):
            wt = REAL_WORKTREE(self.repo, self.state, "impl", "sq-3")
        (wt / "half-done.py").write_text("wip", encoding="utf-8")
        self.git("commit", "-q", "--allow-empty", "-m", "slice part 1", cwd=wt)
        head = self.git("rev-parse", "HEAD", cwd=wt)
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-3", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(wt), worktree_path=str(wt), pid=500,
                                spawned_by="arch")
        task = self.object(owner="sess-impl", squad="sq-3")
        worktrees = self.git("worktree", "list")
        with mock.patch.object(spawner, "ensure_worktree", side_effect=REAL_WORKTREE):
            status, _, res = self.respawn(None, ref=f"T-{task['id']}")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.owner(task), res["session_id"])
        self.assertEqual(spawner.subprocess.Popen.call_args.kwargs["cwd"], str(wt), "it opens in the old worktree")
        self.assertEqual(self.store.spawn_by_session(res["session_id"])["worktree_path"], str(wt))
        self.assertEqual((wt / "half-done.py").read_text(encoding="utf-8"), "wip", "uncommitted work is kept")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), head, "the branch was not reset")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt), "almanac/sq-3/impl")
        self.assertEqual(self.git("worktree", "list"), worktrees, "no new worktree")


class RecoverTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        self.store.create_squad("sq-1", "alpha", str(self.root), "Ship it.")
        self.svc = FakeService(self.store)
        self.now = T0
        self.rec = recover.Recover(clock=lambda: self.now)
        self.store.record_spawn(agent_name="impl", session_id="s-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=200)
        self.svc.fleet.rows.append(dict(pid=200, created="1", role="session", session_id="s-impl", status="idle"))
        self.svc.live = {"impl": None}
        self.task = self.store.create_object(kind="task", project=str(self.root), squad_id="sq-1", title="t", body="",
                                             status="in progress", owner_session="s-impl", parent_id=None, files=[],
                                             created_by=None, created_by_name="operator", who=dict(agent_name="operator"))

    def test_the_escalation_names_the_task_and_closes_once_the_fresh_session_works(self):
        self.svc.fleet.stalls["s-impl"] = dict(error="authentication_failed", text="Not logged in", ts=iso(T0))
        self.rec.tick(self.svc)
        [alert] = self.store.open_needs(to_name="operator")
        self.assertIn(f'mesh_respawn("T-{self.task["id"]}")', alert["body"])
        self.assertIn("[RESPAWN]", alert["body"])
        # a respawn: the same handle in the same squad, a new session that is not stalled
        self.store.record_spawn(agent_name="impl", session_id="s-impl-2", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=201)
        self.svc.fleet.rows[:] = [dict(pid=201, created="2", role="session", session_id="s-impl-2", status="busy")]
        self.now = T0 + 30
        done = self.rec.tick(self.svc)
        self.assertIn(dict(action="resolved", agent="impl"), done)
        self.assertEqual(self.store.open_needs(to_name="operator"), [])

    def test_no_task_no_hint(self):
        self.store.log_object(self.task["id"], who=dict(agent_name="operator"), kind="status", status="accepted")
        self.svc.fleet.stalls["s-impl"] = dict(error="authentication_failed", text="x", ts=iso(T0))
        self.rec.tick(self.svc)
        self.assertNotIn("mesh_respawn", self.store.open_needs(to_name="operator")[0]["body"])


class SurfaceTests(Fixture):
    def test_the_mcp_tool_and_cli_post_the_respawn(self):
        calls = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, **kw: calls.append((path, kw.get("body"))) or {"ok": True})
        self.assertIn("mesh_respawn", {t["name"] for t in mcp_mesh.MCP_TOOLS})
        with mock.patch("builtins.print"):
            mcp_mesh._run_cli(["respawn", "T-4", "--model", "claude-haiku-5-5"], client)
        self.assertEqual(calls, [("/api/mesh/object/respawn", {"ref": "T-4", "model": "claude-haiku-5-5"})])

    def test_the_charter_names_respawn_for_a_role_that_may_spawn(self):
        kw = dict(agent_name="a", provider="claude", role_title="Lead", role_prompt="Lead.", almanac_url="http://x")
        self.assertIn("mesh_respawn(ref)", spawner.build_mesh_charter(**kw, may_respawn=True))
        text = spawner.build_mesh_charter(**kw)
        self.assertNotIn("mesh_respawn", text)
        self.assertIn("spawn|respawn|objects", text)
