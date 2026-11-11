"""T-24: the operator assigns a queued backlog task to a running squad (`POST /api/squads/assign`), and `mesh_spawn(ref)`
puts a fresh member on an existing queued, unowned task of the squad instead of filing a new T."""
import threading

from support import TOKEN
from test_dispatch import BacklogRig
from test_elastic import TASK

from almanac import door, mcp_mesh, spawner, web


class AssignRig(BacklogRig):
    """BacklogRig: a backlog task T-1 in the project, squad sq-1 (lead arch, live) in that project."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.backlog(title="Tidy the store", body="Move the helpers.", files=["almanac/store.py"])[0], 200)

    def assign(self, ref="T-1", squad="sq-1", token=TOKEN):
        return self.post("/api/squads/assign", token, ref=ref, squad_id=squad)

    def row(self, n=1):
        return self.store.object_row(n)


class AssignTests(AssignRig):
    def test_the_task_joins_the_squad_queued_and_unowned_and_the_lead_is_told_once(self):
        status, _, res = self.assign()
        self.assertEqual(status, 200, res)
        self.assertEqual((res["lead"], res["lead_live"], res["delivered"], res["squad_id"]), ("arch", True, True, "sq-1"))
        row = self.row()
        self.assertEqual((row["squad_id"], row["status"], row["owner_session"]), ("sq-1", "queued", None))
        self.assertEqual((res["object"]["squad_id"], res["object"]["owner_name"]), ("sq-1", None))
        self.assertEqual(self.listed(), [], "out of the backlog")
        self.assertEqual(self.request(self.handler, "/api/mesh/objects?squad=sq-1", token=TOKEN)[2]["objects"][0]["ref"], "T-1")
        entry = self.store.object_log(1, 1)[0]
        self.assertEqual((entry["kind"], entry["agent_name"], entry["owner_to"]), ("assign", "operator", None))
        self.assertIn("alpha", entry["body"])
        self.assertIn("sq-1", entry["body"])
        [told] = self.inbox["arch"]
        for text in ("T-1", "Tidy the store", 'mesh_spawn(ref="T-1"', 'mesh_log(ref="T-1", owner=...)'):
            self.assertIn(text, told["body"])
        self.assertEqual(self.inbox["impl"], [], "only the lead")
        self.assertTrue(any(a["action"] == "squad" and a["ok"] and "assigned T-1" in a["detail"] for a in self.store.actions(5)))

    def test_the_lead_is_the_member_the_squad_was_launched_with_first(self):
        self.store.db.execute("update spawned_agents set created_ts = created_ts + 100 where agent_name = 'arch'")
        self.store.db.commit()
        status, _, res = self.assign()
        self.assertEqual((status, res["lead"]), (200, "impl"), "oldest spawn that no agent added")

    def test_no_live_lead_still_joins_and_says_so(self):
        del self.machine.procs[100]                      # arch's process is gone: it is no longer a live peer
        self.fleet.last["at"] = 0
        status, _, res = self.assign()
        self.assertEqual(status, 200, res)
        self.assertEqual((res["lead"], res["lead_live"], res["delivered"]), ("arch", False, False))
        self.assertIn("not live", res["note"])
        self.assertEqual(self.row()["squad_id"], "sq-1", "the task joined anyway")
        self.assertEqual(self.inbox["arch"], [])

    def test_refusals_say_why_and_change_nothing(self):
        before = self.row()
        self.store.db.execute("update squads set status = 'ended' where id = 'sq-2'")
        self.store.db.commit()
        cases = {
            "another project": (self.assign(squad="sq-3"), 409, "not in"),
            "squad not active": (self.assign(squad="sq-2"), 409, "ended"),
            "no such squad": (self.assign(squad="nowhere"), 404, "no squad"),
            "no such task": (self.assign(ref="T-99"), 404, "no object"),
            "an agent": (self.assign(token=web.agent_token(TOKEN, "arch", "sess-arch")), 403, ""),
        }
        for label, ((status, _, res), code, text) in cases.items():
            self.assertEqual(status, code, (label, res))
            self.assertIn(text, res.get("error", ""), label)
        self.assertEqual(self.row(), before)
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["create"])
        self.assertEqual(self.inbox["arch"], [])

    def test_only_a_queued_backlog_task_is_assigned(self):
        self.assertEqual(self.post("/api/mesh/object/log", ref="T-1", status="accepted", body="not needed")[0], 200)
        status, _, res = self.assign()
        self.assertEqual(status, 409)
        self.assertIn("only a queued task", res["error"])
        self.backlog(title="Second")
        self.assertEqual(self.assign("T-2")[0], 200)
        status, _, res = self.assign("T-2")                    # a squad task now, not a backlog one
        self.assertEqual(status, 409)
        self.assertIn("not in the backlog", res["error"])
        self.assertEqual(len(self.inbox["arch"]), 1, "told once")

    def test_a_double_click_assigns_once(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.assign()[0])) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.inbox["arch"]), 1)
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["assign", "create"])

    def test_the_squad_list_carries_the_project_the_backlog_matches_on(self):
        _, _, res = self.request(self.handler, "/api/squads", token=TOKEN)
        self.assertEqual({s["id"]: s["project"] for s in res["squads"]}["sq-1"], self.project)


class SpawnOnATaskTests(AssignRig):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.assign()[0], 200)
        self.store.log_object(1, who=dict(agent_name="arch"), kind="learned", body="the helpers live in store.py:900")
        self.store.log_object(1, who=dict(agent_name="arch"), kind="next", body="start with _json_list")
        self.inbox["arch"].clear()

    def spawn_on(self, ref="T-1", agent="arch", **body):
        body.setdefault("task", "")
        return self.spawn(agent, ref=ref, **body)

    def test_a_fresh_member_takes_the_task_with_its_log_and_no_second_task_is_filed(self):
        used = self.used()
        status, _, res = self.spawn_on()
        self.assertEqual(status, 200, res)
        row = self.row()
        self.assertEqual((row["owner_session"], row["status"]), (res["session_id"], "in progress"))
        self.assertEqual(self.used(), used + 1, "it counts against spawn_limit")
        self.assertEqual([o["id"] for o in self.store.objects(squad_id="sq-1")], [1], "no second T")
        entry = self.store.object_log(1, 1)[0]
        self.assertEqual((entry["kind"], entry["agent_name"], entry["owner_to"], entry["status_to"]),
                         ("assign", "arch", res["agent_name"], "in progress"))
        kickoff = self.argv[-1][-1]
        for text in ("## Your task (`T-1`): Tidy the store", "Move the helpers.", "Files: almanac/store.py",
                     "the helpers live in store.py:900", "start with _json_list", "moved from the backlog into squad alpha",
                     "Report to `arch`", "Ship the roadmap."):
            self.assertIn(text, kickoff)
        self.assertLess(kickoff.index("store.py:900"), kickoff.index("_json_list"), "the log, oldest first")
        self.assertNotIn("More instructions", kickoff)
        self.assertEqual([m for m in self.inbox["impl"] if "assigned to you" in m["body"]], [], "the kickoff is the assignment")
        heard = [m["body"] for m in self.inbox["impl"]]
        self.assertTrue(any("T-1 Tidy the store" in h for h in heard), "the squad hears who joined, and on what")

    def test_task_is_extra_instructions_and_files_replace_the_tasks(self):
        status, _, res = self.spawn_on(task="Mind the Windows paths.", files=["almanac/spawner.py"])
        self.assertEqual(status, 200, res)
        kickoff = self.argv[-1][-1]
        self.assertIn("## More instructions\nMind the Windows paths.", kickoff)
        self.assertIn("Files: almanac/spawner.py", kickoff)
        self.assertNotIn("Files: almanac/store.py", kickoff)
        self.assertEqual(self.row()["files"], ["almanac/store.py"], "the task row is untouched")

    def test_a_bad_file_is_refused_before_anything_starts(self):
        status, _, res = self.spawn_on(files=["../outside"])
        self.assertEqual(status, 400, res)
        self.assertEqual((self.argv, self.row()["owner_session"], self.used()), ([], None, 0))

    def test_without_a_ref_the_task_is_still_required(self):
        status, _, res = self.spawn("arch", task="too short")
        self.assertEqual(status, 400)
        self.assertIn("at least 20 characters", res["error"])

    def test_a_failed_spawn_leaves_the_task_queued_and_unowned(self):
        self.dies.add(1)
        status, _, res = self.spawn_on()
        self.assertEqual(status, 409, res)
        row = self.row()
        self.assertEqual((row["owner_session"], row["status"], row["squad_id"]), (None, "queued", "sq-1"))
        self.assertEqual(self.used(), 0)
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["next", "learned", "assign", "create"])
        self.assertEqual(self.spawn_on()[0], 200, "and it can be tried again")

    def test_refusals(self):
        self.backlog(title="Still in the backlog")                                # T-2
        sq2 = self.store.create_object(kind="task", project=self.project, squad_id="sq-2", title="theirs", body="b",
                                       status="queued", owner_session=None, parent_id=None, files=[], created_by=None,
                                       created_by_name="other", who=dict(agent_name="other"))   # T-3
        owned = self.store.create_object(kind="task", project=self.project, squad_id="sq-1", title="owned", body="b",
                                         status="queued", owner_session="sess-impl", parent_id=None, files=[],
                                         created_by=None, created_by_name="arch", who=dict(agent_name="arch"))  # T-4
        review = self.store.create_object(kind="task", project=self.project, squad_id="sq-1", title="review", body="b",
                                          status="in review", owner_session=None, parent_id=None, files=[], created_by=None,
                                          created_by_name="arch", who=dict(agent_name="arch"))  # T-5
        cases = [("the backlog", "T-2", 403, "backlog"), ("another squad's", f"T-{sq2['id']}", 403, "squad sq-2"),
                 ("owned", f"T-{owned['id']}", 409, "already has an owner"), ("not queued", f"T-{review['id']}", 409, "in review"),
                 ("no such", "T-99", 404, "no object")]
        for label, ref, code, text in cases:
            status, _, res = self.spawn_on(ref)
            self.assertEqual(status, code, (label, res))
            self.assertIn(text, res["error"], label)
        self.assertEqual((self.argv, self.used()), ([], 0), "nothing started, no slot used")
        status, _, res = self.spawn_on(agent="other", ref="T-1")
        self.assertEqual(status, 403, "another squad's agent cannot spend this task")
        self.assertEqual(self.argv, [])

    def test_the_limit_holds_for_a_ref_too(self):
        self.store.update_squad("sq-1", spawn_limit=1)
        self.assertEqual(self.spawn(name="first")[0], 200)
        status, _, res = self.spawn_on()
        self.assertEqual((status, res["error"]), (403, "1/1 used; ask the operator to raise the limit"))
        self.assertEqual(self.row()["owner_session"], None)

    def test_two_calls_for_one_task_take_it_once(self):
        results = []
        threads = [threading.Thread(target=lambda n=n: results.append(self.spawn_on(name=f"racer-{n}")[0])) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.used(), 1)

    def test_the_operator_may_put_a_member_on_it_too(self):
        status, _, res = self.spawn_on(agent=None, squad_id="sq-1", role_id="implementer-claude")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.row()["owner_session"], res["session_id"])
        self.assertEqual(self.store.object_log(1, 1)[0]["agent_name"], "operator")

    def test_the_kickoff_helper_is_the_respawns(self):
        row, squad = self.row(), self.store.squad("sq-1")
        log = list(reversed(self.store.object_log(1, 100)))
        tail = "Do the thing."
        text = door.task_kickoff(row, log, squad, 50_000, tail, extra="More.")
        self.assertTrue(text.endswith(tail))
        self.assertIn("## More instructions\nMore.", text)
        self.assertEqual(door.respawn_kickoff(row, log, squad, "impl", 50_000),
                         door.task_kickoff(row, log, squad, 50_000, text_tail(row, "impl")))


def text_tail(row, name):
    ref = door.ref_of(row)
    return (f"You are a fresh session of `{name}`, started on {ref} in place of the previous one (its context had grown "
            "too large, or it stalled). Carry on from the log: `git status` and `git log` in your worktree show what is "
            f"already done. Hand in as usual: mesh_log(ref=\"{ref}\", status=\"in review\", body=<files touched, the "
            "test command and its result, what is left>).")


class ToolAndCharterTests(AssignRig):
    def client(self, seen):
        return mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                   api_fn=lambda path, method="GET", body=None, timeout=30: seen.append((path, body)) or {})

    def test_the_tool_and_the_cli_carry_ref(self):
        tool = next(t for t in mcp_mesh.MCP_TOOLS if t["name"] == "mesh_spawn")
        self.assertIn("ref", tool["inputSchema"]["properties"])
        self.assertNotIn("required", tool["inputSchema"], "task is optional with a ref")
        seen = []
        client = self.client(seen)
        client.call_tool("mesh_spawn", {"ref": "T-3", "model": "m"})
        self.assertEqual(seen[-1], ("/api/mesh/spawn", dict(task="", ref="T-3", model="m")))
        mcp_mesh._run_cli(["spawn", "--ref", "T-3", "Mind", "the", "paths", "--role", "implementer-claude"], client)
        self.assertEqual(seen[-1][1], dict(task="Mind the paths", ref="T-3", role="implementer-claude"))
        with self.assertRaises(ValueError):
            client.call_tool("mesh_spawn", {})

    def test_the_charter_mentions_it(self):
        charter = spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="R", role_prompt="p",
                                             almanac_url="u", grow=dict(role_title="Implementer", limit=2))
        self.assertEqual(charter.count('mesh_spawn(ref=\\"T-n\\")'), 0)
        self.assertEqual(charter.count('mesh_spawn(ref="T-n")'), 2, "items 5a and 8")
