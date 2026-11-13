"""T-54: a task can be a plan. A lead files it with mesh_file(plan=true) and puts slices under it; its hand-in is
refused while a slice lacks a sketch (or a screen, for almanac/static/), goes to the operator as a blocking message
and starts no reviewer; only the operator accepts it (data.plan.accepted_ts) or sends it back (the lead is told)."""
import json
import subprocess
from unittest import mock

from support import runner_probe
from test_objects import ObjectRig

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

    def test_accept_stamps_accepted_ts(self):
        self.plan()
        self.slice()
        self.hand_in()
        with mock.patch("almanac.door.context_boundary") as boundary:
            status, _, res = self.log(ref="T-1", status="accepted", body="go")
        self.assertEqual(status, 200, res)
        boundary.assert_not_called()                      # an accepted plan starts the work: its owner stays
        self.assertEqual(self.store.object_row(1)["data"]["plan"], {"accepted_ts": res["entry"]["ts"]})

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
        self.assertNotIn("accepted_ts", self.store.object_row(1)["data"]["plan"])
