"""T-54: a task can be a plan. A lead files it with mesh_file(plan=true) and puts slices under it; its hand-in is
refused while a slice lacks a sketch (or a screen, for almanac/static/), goes to the operator as a blocking message
and starts no reviewer; only the operator accepts it (data.plan.accepted_ts) or sends it back (the lead is told)."""
import json
import subprocess
from unittest import mock

from support import TOKEN, runner_probe
from test_objects import ObjectRig
from test_respawn import RespawnRig

from almanac import spawner
from almanac.door import PLAN_TEMPLATE, plan_of

REAL_POPEN = subprocess.Popen
SLICE = "## Goal\nx\n## Sketch\n```python\ndef f(): ...\n```"


class PlanTests(ObjectRig):
    PEERS = ObjectRig.PEERS + (("rev", 900, "sq-1", ["#squad"]),)

    def setUp(self):
        super().setUp()
        # arch leads (its role may spawn); rev is the squad's launched reviewer, so an ordinary first hand-in would
        # start a fresh one
        self.store.db.execute("update spawned_agents set role_id = 'orchestrator-claude' where session_id = 'sess-arch'")
        self.store.db.commit()
        self.store.record_spawn(agent_name="rev", session_id="sess-rev", squad_id="sq-1", role_id="reviewer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None,
                                pid=0)
        self.store.update_squad("sq-1", spawn_limit=2)
        self.service.probe = runner_probe(self.root, claude=True, gemini=False)
        self.service._runners = None
        self.argv = []                                    # what the hub started, git aside
        mock.patch.object(spawner.subprocess, "Popen", side_effect=self.popen).start()
        self.addCleanup(mock.patch.stopall)

    def popen(self, argv, **kwargs):
        if argv[0] == "git":                              # the hand-in's head read runs for real
            return REAL_POPEN(argv, **kwargs)
        self.argv.append(list(argv))
        return mock.MagicMock(pid=9000 + len(self.argv), **{"poll.return_value": None})

    def plan(self, token=None, **body):
        status, _, res = self.file(token or self.tok("arch"), title="the plan", plan=True, **body)
        self.assertEqual(status, 200, res)
        return res

    def slice(self, parent="T-1", body=SLICE, **kw):
        status, _, res = self.file(self.tok("arch"), title="a slice", parent=parent, body=body, **kw)
        self.assertEqual(status, 200, res)
        return res["object"]["ref"]

    def hand_in(self, ref="T-1", who="arch"):
        return self.log(self.tok(who), ref=ref, status="in review", body="the plan, for decision")

    def operator_mail(self):
        return [m for m in self.store.operator_inbox(squad_id="sq-1")["messages"] if m["to_name"] == "operator"]

    def clear(self):
        for got in self.inbox.values():
            got.clear()

    def test_files_a_plan_and_returns_the_template(self):
        res = self.plan()
        self.assertEqual(res["template"], PLAN_TEMPLATE)
        self.assertIn("## Sketch      signatures", res["template"])
        self.assertEqual(self.store.object_row(1)["data"], {"plan": {}})
        ref = self.slice()
        self.assertEqual(plan_of(self.service, self.store.object_row(int(ref[2:])))["id"], 1)
        self.assertNotIn("template", self.file(self.tok("arch"))[2], "an ordinary task has none")
        self.assertIsNone(plan_of(self.service, self.store.object_row(3)))
        self.assertEqual(self.file(title="op plan", plan=True)[0], 200, "the operator may")

    def test_a_builder_cannot_file_a_plan(self):
        status, _, res = self.file(self.tok("impl"), title="p", plan=True)
        self.assertEqual(status, 403, res)
        self.assertEqual(self.file(title="p", plan=True, backlog=True)[0], 400, "not in the backlog")
        self.assertEqual(self.store.objects_rev(), 0, "nothing written")

    def test_an_agent_files_a_plan_undecided_only(self):
        for status in ("accepted", "in review", "blocked"):
            code, _, res = self.file(self.tok("arch"), title="p", plan=True, status=status)
            self.assertEqual(code, 403, (status, res))
        self.assertIn("hand it in with mesh_log", self.file(self.tok("arch"), title="p", plan=True,
                                                            status="in review")[2]["error"])
        self.assertEqual(self.store.objects_rev(), 0, "nothing written")
        self.assertEqual(self.file(self.tok("arch"), title="p", plan=True, status="in progress")[0], 200)

    def test_handin_refused_names_slices_without_a_sketch(self):
        self.plan()
        status, _, res = self.hand_in()
        self.assertEqual(status, 400, res)
        self.assertIn("no slices", res["error"])
        self.slice()                                                   # T-2 fine
        self.slice(body="## Goal\nmentions ## Sketch mid-line only")   # T-3
        self.slice(parent="T-2", body="no headings")                   # T-4, a grandchild
        self.file(self.tok("arch"), title="an addition", plan=True, parent="T-1")   # T-5, a plan under the plan
        self.slice(parent="T-5", body="no headings")                   # T-6: T-5's to decide, not T-1's
        rev = self.store.objects_rev()
        status, _, res = self.hand_in()
        self.assertEqual(status, 400, res)
        self.assertIn("T-3: no ## Sketch", res["error"])
        self.assertIn("T-4: no ## Sketch", res["error"])
        self.assertNotIn("T-2", res["error"])
        self.assertNotIn("T-5", res["error"])
        self.assertNotIn("T-6", res["error"])
        self.assertEqual((self.store.objects_rev(), self.store.object_row(1)["status"]), (rev, "queued"),
                         "a refused hand-in writes nothing")
        self.assertEqual(self.operator_mail(), [])

    def test_a_plan_whose_only_child_is_a_plan_has_no_slices(self):
        self.plan()
        self.file(self.tok("arch"), title="nested", plan=True, parent="T-1")
        status, _, res = self.hand_in()
        self.assertEqual(status, 400, res)
        self.assertIn("T-1 has no slices", res["error"])

    def test_cli_files_a_plan(self):
        import contextlib
        import io
        from almanac import mcp_mesh

        def api(path, method="GET", body=None, timeout=30.0):
            status, _, doc = self.request(self.handler, path, token=self.tok("arch"), method=method, body=body)
            if status >= 400:
                raise RuntimeError(doc.get("error"))
            return doc
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch", squad_id="sq-1", api_fn=api)
        with contextlib.redirect_stdout(io.StringIO()):
            mcp_mesh._run_cli(["file", "the plan", "--plan"], client)
        self.assertEqual(self.store.object_row(1)["data"], {"plan": {}})

    def test_a_static_slice_needs_a_screen(self):
        self.plan()
        self.slice(files=["almanac/static/app.js"])
        self.slice(files=["almanac/door.py"])
        status, _, res = self.hand_in()
        self.assertEqual(status, 400, res)
        self.assertIn("T-2: no ## Screen", res["error"])
        self.assertNotIn("T-3", res["error"])
        self.store.db.execute("update objects set body = ? where id = 2", (SLICE + "\n## Screen\n[ PLAN ]",))
        self.store.db.commit()
        self.assertEqual(self.hand_in()[0], 200)

    def test_handin_alerts_the_operator_and_starts_no_reviewer(self):
        self.plan()
        self.slice()
        self.clear()
        status, _, res = self.hand_in()
        self.assertEqual(status, 200, res)
        [mail] = self.operator_mail()
        self.assertEqual((mail["from_name"], mail["needs"]), ("arch", "blocking"))
        self.assertIn("[T-1 queued → in review by arch] the plan", mail["body"])
        self.assertNotIn("Branch:", mail["body"], "a plan ships no code")
        self.assertIsNone(self.store.object_row(1)["sha"])
        self.assertEqual({n: g for n, g in self.inbox.items() if g}, {}, "no reviewer told, standing or fresh")
        self.assertNotIn("reviewer", self.store.object_row(1)["data"])
        self.assertEqual(self.argv, [])
        self.file(self.tok("arch"), owner="impl")         # T-3, an ordinary task: its first hand-in still gets one
        self.assertEqual(self.log(self.tok("impl"), ref="T-3", status="in review", body="done")[0], 200)
        self.assertEqual(len(self.argv), 1)

    def test_an_agent_cannot_accept_or_withdraw_a_plan(self):
        self.plan()
        self.slice()
        self.assertEqual(self.log(self.tok("arch"), ref="T-1", status="accepted")[0], 403, "not even before review")
        self.assertEqual(self.hand_in()[0], 200)
        for status in ("accepted", "in progress", "blocked", "queued"):
            code, _, res = self.log(self.tok("arch"), ref="T-1", status=status)
            self.assertEqual(code, 403, (status, res))
            self.assertIn("only the operator decides a plan", res["error"])
        self.assertEqual(self.log(self.tok("arch"), ref="T-1", body="a note is fine")[0], 200)
        self.assertEqual(self.store.object_row(1)["status"], "in review")
        self.assertEqual(self.log(ref="T-1", status="accepted")[0], 200)
        for who, status in (("arch", "in progress"), ("arch", "blocked"), ("arch", "queued"), ("impl", "blocked")):
            self.assertEqual(self.log(self.tok(who), ref="T-1", status=status)[0], 403, (who, status))
        self.assertEqual(self.store.object_row(1)["status"], "accepted")
        self.assertEqual(self.hand_in()[0], 200, "the lead re-hands an accepted plan in: a re-plan")
        self.assertEqual(self.store.object_row(1)["status"], "in review")

    def test_a_builder_cannot_hand_in_or_move_a_plan_but_may_note_it(self):
        self.plan()
        self.slice()
        self.clear()
        for status in ("in review", "in progress", "blocked"):
            code, _, res = self.hand_in(who="impl") if status == "in review" else self.log(
                self.tok("impl"), ref="T-1", status=status)
            self.assertEqual(code, 403, (status, res))
            self.assertIn("may not change a plan", res["error"])
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", owner="impl")[0], 403)
        self.assertEqual((self.operator_mail(), self.heard()), ([], set()), "pages nobody")
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", body="T-2 needs a migration")[0], 200)
        self.assertEqual(self.store.object_row(1)["status"], "queued")
        self.assertEqual(self.log(self.tok("arch"), ref="T-1", status="blocked")[0], 200, "the lead may, undecided")

    def blocking(self):
        return self.store.operator_counts(squad_id="sq-1")["open"]["blocking"]

    def test_a_second_hand_in_leaves_one_open_flag(self):
        self.plan()
        self.slice()
        self.hand_in()
        first = self.store.object_row(1)["data"]["plan"]["flag"]
        self.hand_in()
        self.assertEqual(self.blocking(), 1)
        self.assertEqual([m["id"] for m in self.store.open_needs(squad_id="sq-1", to_name="operator")],
                         [self.store.object_row(1)["data"]["plan"]["flag"]])
        self.assertNotEqual(self.store.object_row(1)["data"]["plan"]["flag"], first)

    def test_accept_stamps_accepted_ts(self):
        self.plan()
        self.slice()
        handin = self.hand_in()[2]
        self.assertEqual(self.blocking(), 1)
        with mock.patch("almanac.door.context_boundary") as boundary:
            status, _, res = self.log(ref="T-1", status="accepted", body="go")
        self.assertEqual(status, 200, res)
        boundary.assert_not_called()                      # an accepted plan starts the work: its owner stays
        self.assertEqual(self.store.object_row(1)["data"]["plan"],
                         {"accepted_ts": res["entry"]["ts"], "handin_ts": handin["entry"]["ts"]})
        self.assertEqual(self.blocking(), 0, "the decision closes the hand-in's flag")

    def test_send_back_tells_the_lead_with_the_comment(self):
        self.file(title="the plan", plan=True)            # the operator files it: only the lead path tells arch
        self.slice()
        self.hand_in()
        self.clear()
        status, _, res = self.log(ref="T-1", status="in progress", body="split T-2 in two")
        self.assertEqual(status, 200, res)
        self.assertEqual({n: len(g) for n, g in self.inbox.items() if g}, {"arch": 1})
        said = json.dumps(self.inbox["arch"][0], ensure_ascii=False)
        self.assertIn("[T-1 in review → in progress by operator] the plan", said)
        self.assertIn("split T-2 in two", said)
        self.assertEqual(list(self.store.object_row(1)["data"]["plan"]), ["handin_ts"], "the flag is gone")
        self.assertEqual(self.blocking(), 0, "sending it back closes the hand-in's flag")


TASK = "Build the slice to its sketch, with tests, and hand it in."


class GateTests(PlanTests):
    """T-55: in a plan-first squad an agent's mesh_spawn starts a member only on a slice of a plan the operator
    accepted. sq-1 lets arch (the lead) add implementers, up to 2."""

    def setUp(self):
        super().setUp()
        self.store.upsert_role(dict(self.store.role("implementer-claude"), models=[], efforts=[]))
        self.store.update_squad("sq-1", spawn_role="implementer-claude", spawn_limit=2, plan_first=True)
        # the test folder sits inside Almanac's own checkout: a real worktree would branch the live repo
        mock.patch.object(spawner, "ensure_worktree", side_effect=lambda repo, *a, **k: repo).start()

    def spawn(self, agent="arch", **body):
        token = self.tok(agent) if agent else TOKEN
        return self.request(self.handler, "/api/mesh/spawn", token=token, method="POST", body={"task": TASK, **body})

    def refused(self, needle, **body):
        rev, used = self.store.objects_rev(), self.store.squad("sq-1")["spawn_used"]
        status, _, res = self.spawn(**body)
        self.assertEqual(status, 403, res)
        self.assertIn(needle, res["error"])
        self.assertEqual((self.store.objects_rev(), self.store.squad("sq-1")["spawn_used"], self.argv), (rev, used, []),
                         "no task filed, no slot taken, nothing started")
        self.assertEqual(self.store.actions()[0]["detail"], f"mesh_spawn refused: {res['error']}")
        return res["error"]

    def accepted_plan(self):
        """T-1 a plan with slice T-2, handed in and accepted."""
        self.plan()
        self.slice()
        self.assertEqual(self.hand_in()[0], 200)
        self.assertEqual(self.log(ref="T-1", status="accepted")[0], 200)

    def test_a_plan_first_squad_refuses_spawn_without_ref(self):
        self.assertIn("squad sq-1 is plan-first: name a filed task with ref", self.refused("plan-first"))

    def test_refuses_a_task_outside_any_plan(self):
        self.file(self.tok("arch"), title="loose work")                       # T-1, under no plan
        self.assertEqual(self.refused("not under an accepted plan", ref="T-1"),
                         "T-1 is not under an accepted plan: file the slices, hand the plan in and wait for the operator")
        self.plan()                                                           # T-2: a plan itself is no slice
        self.refused("T-2 is a plan, not a slice", ref="T-2")

    def test_refuses_while_the_plan_is_in_review(self):
        self.plan()
        self.slice()
        self.refused("(T-1 is queued)", ref="T-2")
        self.hand_in()
        self.refused("(T-1 is in review)", ref="T-2")

    def test_allows_a_covered_slice_after_accept(self):
        self.accepted_plan()
        self.slice()                                       # T-3
        status, _, res = self.spawn(ref="T-2")
        self.assertEqual(status, 200, res)
        self.assertEqual((self.store.object_row(2)["status"], self.store.squad("sq-1")["spawn_used"]), ("in progress", 1))
        # a re-hand-in keeps accepted_ts but not the status: nothing is covered until the operator accepts again
        self.store.db.execute("update objects set created_ts = 0 where id = 3")     # as if filed before the accept
        self.store.db.commit()
        self.argv.clear()
        self.assertEqual(self.hand_in()[0], 200)
        self.assertIn("accepted_ts", self.store.object_row(1)["data"]["plan"])
        self.refused("(T-1 is in review)", ref="T-3")

    def test_files_at_spawn_cannot_widen_a_covered_slice(self):
        self.accepted_plan()
        self.assertEqual(self.refused("files are the plan's", ref="T-2", files=["almanac/other.py"]),
                         "T-2's files are the plan's: amend the task and hand the plan in again")
        status, _, res = self.spawn(ref="T-2", task="working notes: start from the store, then the route")
        self.assertEqual(status, 200, res)

    def test_refuses_a_slice_filed_after_accept(self):
        self.accepted_plan()
        ref = self.slice()
        self.assertEqual(self.refused("changed after", ref=ref),
                         f"{ref} changed after plan T-1 was handed in: hand the plan in again")

    def test_refuses_a_slice_amended_after_accept(self):
        self.accepted_plan()
        status, _, res = self.post("/api/mesh/object/amend", ref="T-2", body=SLICE + "\nand one thing more")
        self.assertEqual(status, 200, res)
        self.refused("T-2 changed after plan T-1 was handed in", ref="T-2")

    def amend(self, ref, more):
        status, _, res = self.post("/api/mesh/object/amend", ref=ref, body=SLICE + more)
        self.assertEqual(status, 200, res)

    def test_a_slice_filed_between_hand_in_and_accept_is_not_covered(self):
        self.plan()
        self.slice()                                       # T-2, handed in
        self.assertEqual(self.hand_in()[0], 200)
        ref = self.slice()                                 # T-3, filed while the plan is in review: the operator never saw it
        self.assertEqual(self.log(ref="T-1", status="accepted")[0], 200)
        self.assertEqual(self.refused("changed after", ref=ref),
                         f"{ref} changed after plan T-1 was handed in: hand the plan in again")
        self.assertEqual(self.spawn(ref="T-2")[0], 200, "what was handed in is covered")

    def test_a_slice_amended_between_hand_in_and_accept_is_not_covered(self):
        self.plan()
        self.slice()
        self.slice()                                       # T-3, untouched
        self.assertEqual(self.hand_in()[0], 200)
        self.amend("T-2", "\nand one thing more")          # while the plan is in review: allowed, not covered
        self.assertEqual(self.log(ref="T-1", status="accepted")[0], 200)
        self.refused("T-2 changed after plan T-1 was handed in", ref="T-2")
        self.assertEqual(self.spawn(ref="T-3")[0], 200)

    def test_a_re_hand_in_moves_the_cut(self):
        self.plan()
        self.slice()
        self.assertEqual(self.hand_in()[0], 200)
        self.assertEqual(self.log(ref="T-1", status="in progress", body="tighten T-2")[0], 200)     # sent back
        self.amend("T-2", "\ntightened")
        self.assertEqual(self.hand_in()[0], 200)          # the amended slice is handed in
        self.assertEqual(self.log(ref="T-1", status="accepted")[0], 200)
        status, _, res = self.spawn(ref="T-2")
        self.assertEqual(status, 200, res)

    def test_a_second_accepted_plan_covers_its_own_slices(self):
        self.accepted_plan()
        self.plan(parent="T-1")                            # T-3, an addition under the accepted plan
        ref = self.slice(parent="T-3")                     # T-4: T-3 decides it, not T-1
        self.refused("(T-3 is queued)", ref=ref)
        self.assertEqual(self.hand_in("T-3")[0], 200)
        self.assertEqual(self.log(ref="T-3", status="accepted")[0], 200)
        self.assertEqual(self.spawn(ref=ref)[0], 200)
        self.assertEqual(self.spawn(ref="T-2", name="impl-two")[0], 200, "T-1's slice is still covered")

    def test_the_operator_is_never_gated(self):
        status, _, res = self.spawn(agent=None, squad_id="sq-1", role_id="implementer-claude")
        self.assertEqual(status, 200, res)
        loose = self.file(self.tok("arch"), title="loose")[2]["object"]["ref"]       # under no plan
        self.assertEqual(self.spawn(agent=None, squad_id="sq-1", role_id="implementer-claude", ref=loose,
                                    name="impl-two")[0], 200)

    def test_the_hub_reviewer_is_never_gated(self):
        self.file(self.tok("arch"), owner="impl")          # T-1, under no plan: its first hand-in starts a reviewer
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", status="in review", body="done")[0], 200)
        self.assertEqual(len(self.argv), 1)

    def test_a_squad_without_plan_first_is_unchanged(self):
        self.store.update_squad("sq-1", plan_first=False)
        status, _, res = self.spawn()
        self.assertEqual(status, 200, res)
        self.assertEqual(self.store.squad("sq-1")["spawn_used"], 1)

    def test_a_squad_launched_plan_first_has_it_before_any_member_starts(self):
        seen, wrapped = [], self.popen

        def popen(argv, **kwargs):
            if argv[0] != "git":
                seen.append(self.store.squads(limit=1)[0]["plan_first"])
            return wrapped(argv, **kwargs)
        mock.patch.object(spawner.subprocess, "Popen", side_effect=popen).start()
        member = [dict(role_id="implementer-claude", workspace_mode="shared")]
        status, _, res = self.post("/api/squads", name="pf", cwd=str(self.root), brief="b", plan_first=True,
                                   members=member)
        self.assertEqual(status, 200, res)
        self.assertEqual((seen, res["squad"]["plan_first"]), ([True], True))
        status, _, res = self.post("/api/squads", name="off", cwd=str(self.root), brief="b", members=member)
        self.assertEqual(status, 200, res)
        self.assertEqual((seen[-1], res["squad"]["plan_first"]), (False, False), "left out: off")

    def test_a_backlog_task_carries_plan_first_to_its_dispatch(self):
        from almanac.door import dispatch_task
        status, _, res = self.file(title="later", backlog=True, project=str(self.root), plan_first=True)
        self.assertEqual(status, 200, res)
        self.assertIs(self.store.object_row(res["object"]["id"])["data"]["plan_first"], True)
        self.assertEqual(self.file(self.tok("arch"), title="now", plan_first=True)[0], 400, "for a backlog task")
        with mock.patch.object(spawner, "launch_squad", side_effect=RuntimeError("stop")) as launch:
            with self.assertRaises(RuntimeError):
                dispatch_task(self.service, res["object"]["ref"], almanac_url="http://x", token=TOKEN)
        self.assertIs(launch.call_args.kwargs["plan_first"], True)

    def test_mesh_file_and_the_cli_send_plan_first(self):
        import contextlib
        import io
        from almanac import mcp_mesh

        def api(path, method="GET", body=None, timeout=30.0):
            status, _, doc = self.request(self.handler, path, token=self.tok("arch"), method=method, body=body)
            if status >= 400:
                raise RuntimeError(doc.get("error"))
            return doc
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch", squad_id="sq-1", api_fn=api)
        res = client.call_tool("mesh_file", {"title": "later", "backlog": True, "plan_first": True})
        self.assertIs(self.store.object_row(res["object"]["id"])["data"]["plan_first"], True)
        with contextlib.redirect_stdout(io.StringIO()):
            mcp_mesh._run_cli(["file", "later too", "--backlog", "--plan-first"], client)
            mcp_mesh._run_cli(["file", "a squad task"], client)          # left out: not sent, so not refused
        self.assertIs(self.store.object_row(2)["data"]["plan_first"], True)
        self.assertEqual(self.store.object_row(3)["title"], "a squad task")


class RespawnGateTests(RespawnRig):
    def test_respawn_is_never_gated(self):
        """mesh_respawn carries on a task that already has its member: a plan-first squad does not gate it."""
        self.store.update_squad("sq-1", plan_first=True)
        status, _, res = self.respawn("arch")             # T-1 is under no plan
        self.assertEqual(status, 200, res)
        self.assertEqual(self.killed, [500])


class MoveTests(GateTests):
    """T-60: slices that arrived in a plan-first squad from the backlog are filed before any plan; the lead (or the
    operator) moves each queued, unowned one under a plan of the squad with mesh_log(parent=)."""

    def loose(self, body="no headings", **kw):
        """A squad task under no plan, as a slice dispatched from the backlog arrives."""
        status, _, res = self.file(self.tok("arch"), title="from the backlog", body=body, **kw)
        self.assertEqual(status, 200, res)
        return res["object"]["ref"]

    def move(self, ref, parent, who="arch"):
        return self.log(self.tok(who) if who else TOKEN, ref=ref, parent=parent)

    def unmoved(self, ref, parent, code, needle, who="arch"):
        before = self.store.object_row(int(ref[2:]))["parent_id"]
        status, _, res = self.move(ref, parent, who)
        self.assertEqual(status, code, res)
        self.assertIn(needle, res["error"])
        self.assertEqual(self.store.object_row(int(ref[2:]))["parent_id"], before)

    def test_a_lead_moves_a_queued_unowned_slice_under_a_plan(self):
        ref = self.loose()                                 # T-1, no sketch
        self.plan()                                        # T-2
        status, _, res = self.move(ref, "T-2")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.store.object_row(1)["parent_id"], 2)
        self.assertEqual(res["entry"]["kind"], "parent")
        self.assertEqual(res["entry"]["body"], "parent (none) → T-2")
        status, _, res = self.hand_in("T-2")
        self.assertEqual(status, 400, res)
        self.assertIn("T-1: no ## Sketch", res["error"], "plan_gaps reads the moved slice")
        sub = self.loose(body=SLICE)                       # T-3: under a slice of the plan is under the plan
        self.assertEqual(self.move(sub, "T-1", who=None)[0], 200, "the operator may")
        self.assertEqual(plan_of(self.service, self.store.object_row(3))["id"], 2)

    def test_a_builder_cannot_move_a_slice(self):
        ref = self.loose()
        self.plan()
        self.unmoved(ref, "T-2", 403, "only a backlog task", who="impl")

    def test_an_owned_or_started_slice_does_not_move(self):
        self.plan()                                        # T-1
        owned = self.loose(owner="impl")
        started = self.loose(status="in progress")
        for ref in (owned, started):
            self.unmoved(ref, "T-1", 400, "queued, unowned")
        self.unmoved(owned, "T-1", 400, "queued, unowned", who=None)
        cleared = self.slice()                             # under T-1 already: never cleared this way
        self.unmoved(cleared, "", 400, "queued, unowned")

    def test_not_under_a_task_outside_a_plan(self):
        ref, other = self.loose(), self.loose()
        self.unmoved(ref, other, 400, f"{other} is no plan, nor under one")
        self.unmoved(ref, other, 400, "nor under one", who=None)

    def test_not_into_another_squads_plan(self):
        ref = self.loose()                                 # T-1
        self.assertEqual(self.file(squad="sq-2", title="theirs", plan=True)[0], 200)     # T-2, sq-2's
        self.unmoved(ref, "T-2", 400, "no task of squad sq-1")
        self.unmoved(ref, "T-2", 400, "no task of squad sq-1", who=None)
        self.assertEqual(self.log(self.tok("other"), ref="T-2", body="x")[0], 200)
        self.unmoved(ref, "T-2", 403, "another squad", who="other")

    def test_a_plan_does_not_move_under_a_plan(self):
        self.plan()                                        # T-1
        self.plan()                                        # T-2
        self.unmoved("T-2", "T-1", 400, "queued, unowned")

    def test_no_cycle(self):
        self.plan()                                        # T-1
        top = self.slice()                                 # T-2
        ref = self.slice(parent=top)                       # T-3
        self.unmoved(top, ref, 400, "itself")
        self.unmoved(top, top, 400, "itself")
        self.unmoved(ref, top, 400, "already has parent")

    def test_a_slice_moved_after_the_hand_in_is_not_covered(self):
        loose, late = self.loose(body=SLICE), self.loose(body=SLICE)    # T-1, T-2: filed before the plan
        self.plan()                                        # T-3
        self.assertEqual(self.move(loose, "T-3")[0], 200)
        self.assertEqual(self.hand_in("T-3")[0], 200)
        self.assertEqual(self.move(late, "T-3")[0], 200, "moved while the plan is in review")
        self.assertEqual(self.log(ref="T-3", status="accepted")[0], 200)
        self.refused(f"{late} changed after plan T-3 was handed in", ref=late)
        self.assertEqual(self.spawn(ref=loose)[0], 200, "what was handed in is covered")
        self.argv.clear()
        another = self.loose(body=SLICE)                   # moved under the accepted plan
        self.assertEqual(self.move(another, "T-3")[0], 200)
        self.refused(f"{another} changed after plan T-3 was handed in", ref=another)
        self.assertEqual(self.hand_in("T-3")[0], 200)
        self.assertEqual(self.log(ref="T-3", status="accepted")[0], 200)
        self.assertEqual(self.spawn(ref=another, name="impl-two")[0], 200, "handed in again: covered")

    def test_the_dispatch_notice_names_the_plan_step_only_in_a_plan_first_squad(self):
        from almanac.door import PLAN_FIRST_MOVE
        for plan_first in (True, False):
            self.store.update_squad("sq-1", plan_first=plan_first)
            self.clear()
            status, _, res = self.file(title="feature", backlog=True, project=str(self.root))
            self.assertEqual(status, 200, res)
            top = res["object"]["ref"]
            self.assertEqual(self.file(title="slice", backlog=True, project=str(self.root), parent=top)[0], 200)
            status, _, res = self.post("/api/squads/assign", ref=top, squad_id="sq-1")
            self.assertEqual((status, res.get("delivered")), (200, True), res)
            said = json.dumps(self.inbox["arch"], ensure_ascii=False)
            self.assertEqual(PLAN_FIRST_MOVE in said, plan_first, said)
        self.store.update_squad("sq-1", plan_first=True)
        self.clear()
        status, _, res = self.file(title="alone", backlog=True, project=str(self.root))
        self.assertEqual(self.post("/api/squads/assign", ref=res["object"]["ref"], squad_id="sq-1")[0], 200)
        self.assertNotIn("plan-first", json.dumps(self.inbox["arch"]), "no slices moved: nothing to put under a plan")
        from almanac.door import dispatch_task                       # the dispatched lead's brief says it too
        for plan_first in (True, False):
            top = self.file(title="later", backlog=True, project=str(self.root), plan_first=plan_first)[2]["object"]["ref"]
            self.file(title="its slice", backlog=True, project=str(self.root), parent=top)
            with mock.patch.object(spawner, "launch_squad", side_effect=RuntimeError("stop")) as launch:
                with self.assertRaises(RuntimeError):
                    dispatch_task(self.service, top, almanac_url="http://x", token=TOKEN)
            task = launch.call_args.kwargs["members"][0]["task"]
            self.assertEqual("file what is missing under it, then hand the plan in. Once the operator accepts it, put "
                             "members on the slices with mesh_spawn(ref=...). When the work is done, hand the feature "
                             f"in with mesh_log(ref=\"{top}\", status=\"in review\")." in task, plan_first, task)
            self.assertEqual("put members on them with mesh_spawn" in task, not plan_first, task)

    def test_the_parts_of_a_slice_moved_after_the_hand_in_are_not_covered(self):
        a = self.loose(body=SLICE)                         # T-1
        a1 = self.loose(body=SLICE, parent=a)              # T-2, its part
        b = self.loose(body=SLICE)                         # T-3
        b1 = self.loose(body=SLICE, parent=b)              # T-4
        self.plan()                                        # T-5
        self.assertEqual(self.move(b, "T-5")[0], 200)      # handed in with the plan: b and its part are covered
        self.slice(parent="T-5")                           # T-6
        self.assertEqual(self.hand_in("T-5")[0], 200)
        self.assertEqual(self.move(a, "T-5")[0], 200, "moved while the plan is in review")
        self.assertEqual(self.log(ref="T-5", status="accepted")[0], 200)
        self.refused(f"{a1} changed after plan T-5 was handed in", ref=a1)
        self.refused(f"{a} changed after plan T-5 was handed in", ref=a)
        self.assertEqual(self.spawn(ref=b1)[0], 200, "a part of a slice that was handed in is covered")
        self.argv.clear()
        c = self.loose(body=SLICE)
        c1 = self.loose(body=SLICE, parent=c)
        self.assertEqual(self.move(c, "T-5")[0], 200, "moved under the accepted plan")
        self.refused(f"{c1} changed after plan T-5 was handed in", ref=c1)
