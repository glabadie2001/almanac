"""T-32: the backlog as a feature map. A backlog task takes a backlog parent (`mesh_file(backlog, parent)`,
`mesh_log(ref, parent)`), any task is amended with its old text kept (`POST /api/mesh/object/amend`), and a parent's
queued backlog children move with it on dispatch and assign."""
from unittest import mock

from support import TOKEN
from test_dispatch import ROSTER, BacklogRig

from almanac import door, mcp_mesh, web

ARCH = web.agent_token(TOKEN, "arch", "sess-arch")        # sq-1's lead: its role may spawn
IMPL = web.agent_token(TOKEN, "impl", "sess-impl")        # sq-1, may not
FAR = web.agent_token(TOKEN, "far", "sess-far")           # sq-3: another project


class TreeRig(BacklogRig):
    def log(self, ref, token=TOKEN, **body):
        return self.post("/api/mesh/object/log", token, ref=ref, **body)

    def amend(self, ref, token=TOKEN, **body):
        return self.post("/api/mesh/object/amend", token, ref=ref, **body)

    def parent_of(self, n):
        return self.store.object_row(n)["parent_id"]

    def squad_task(self, title="in the squad", **kw):
        return self.store.create_object(
            kind="task", project=self.project, squad_id="sq-1", title=title, body="b", status="queued",
            owner_session=None, parent_id=kw.get("parent_id"), files=[], created_by=None, created_by_name="arch",
            who=dict(agent_name="arch"))


class FilingTests(TreeRig):
    def test_a_backlog_task_takes_a_backlog_parent_of_its_project(self):
        self.assertEqual(self.backlog(title="Feature")[0], 200)                       # T-1
        status, _, res = self.backlog(title="Slice", parent="T-1")
        self.assertEqual(status, 200, res)
        self.assertEqual((res["object"]["parent"], res["object"]["squad_id"]), ("T-1", None))
        self.assertEqual(self.backlog(ARCH, title="By an agent", parent="T-1")[0], 200)
        self.assertEqual(self.store.object_row(1)["parent_id"], None)
        self.assertEqual([self.parent_of(n) for n in (2, 3)], [1, 1])
        self.assertEqual(self.listed(), ["T-1", "T-2", "T-3"], "parents first")

    def test_each_refusal(self):
        self.backlog(title="Feature")                                                 # T-1
        self.backlog(title="Theirs", project=str(self.elsewhere))                     # T-2: another project's backlog
        sq = self.squad_task()                                                        # T-3: a squad's
        cases = {"in a squad": dict(parent=f"T-{sq['id']}"), "another project": dict(parent="T-2"),
                 "none such": dict(parent="T-99"), "not a ref": dict(parent="soon")}
        for label, body in cases.items():
            status, _, res = self.backlog(title="Slice", **body)
            self.assertEqual(status, 400, (label, res))
            self.assertIn("parent" if label != "not a ref" else "not a ref", res["error"], label)
        status, _, res = self.backlog(ARCH, title="Slice", parent="T-2")
        self.assertEqual(status, 400, "an agent files in its own project's backlog only")
        self.assertEqual(len(self.store.objects(squad_id=None)), 2, "nothing was filed")
        status, _, res = self.post("/api/mesh/object", ARCH, title="Squad task", parent="T-1")
        self.assertEqual(status, 400, "a squad task still takes only a parent of its squad")


class ReparentTests(TreeRig):
    def setUp(self):
        super().setUp()
        for t in ("Feature", "Other feature", "Slice"):                               # T-1, T-2, T-3
            self.backlog(title=t)

    def test_set_move_and_clear_say_from_what_to_what(self):
        self.assertEqual(self.log("T-3", parent="T-1")[0], 200)
        self.assertEqual(self.parent_of(3), 1)
        self.assertEqual(self.log("T-3", ARCH, parent="T-2")[0], 200, "any agent of the project")
        self.assertEqual(self.parent_of(3), 2)
        status, _, res = self.log("T-3", IMPL, parent="")
        self.assertEqual(status, 200, res)
        self.assertEqual((self.parent_of(3), res["object"]["parent"]), (None, None))
        entries = self.store.object_log(3, 3)
        self.assertEqual([(e["kind"], e["body"], e["agent_name"]) for e in entries],
                         [("parent", "parent T-2 → (none)", "impl"), ("parent", "parent T-1 → T-2", "arch"),
                          ("parent", "parent (none) → T-1", "operator")])
        self.assertEqual(self.store.object_row(3)["status"], "queued", "the status is untouched")

    def test_with_a_note_or_the_operators_status_in_the_same_call(self):
        status, _, res = self.log("T-3", parent="T-1", body="part of the feature", status="blocked")
        self.assertEqual(status, 200, res)
        self.assertEqual([e["kind"] for e in self.store.object_log(3, 3)], ["status", "parent", "create"])
        self.assertEqual(self.log("T-3", ARCH, parent="T-2", status="accepted")[0], 403, "an agent never sets it")
        self.assertEqual(self.parent_of(3), 1, "and the refusal changed nothing")

    def test_refusals(self):
        self.log("T-3", parent="T-1")
        self.backlog(title="Theirs", project=str(self.elsewhere))                     # T-4
        sq = self.squad_task()                                                        # T-5
        before = self.store.object_log(3, 10)
        cases = {"itself": ("T-3", TOKEN, dict(parent="T-3"), 400, "itself"),
                 "its own child": ("T-1", TOKEN, dict(parent="T-3"), 400, "itself"),
                 "already": ("T-3", TOKEN, dict(parent="T-1"), 400, "already has parent T-1"),
                 "already none": ("T-2", TOKEN, dict(parent=""), 400, "no parent"),
                 "another project's": ("T-3", TOKEN, dict(parent="T-4"), 400, "no task of the backlog"),
                 "a squad's": ("T-3", TOKEN, dict(parent=f"T-{sq['id']}"), 400, "no task of the backlog"),
                 "none such": ("T-3", TOKEN, dict(parent="T-99"), 400, "no task"),
                 "not text": ("T-3", TOKEN, dict(parent=7), 400, ""),
                 "an agent of another project": ("T-3", FAR, dict(parent="T-2"), 403, "another squad"),
                 "a task in a squad": (f"T-{sq['id']}", ARCH, dict(parent="T-1"), 400, "only a backlog task")}
        for label, (ref, token, body, code, text) in cases.items():
            status, _, res = self.log(ref, token, **body)
            self.assertEqual(status, code, (label, res))
            self.assertIn(text, res["error"], label)
        self.assertEqual(self.store.object_log(3, 10), before)
        self.assertEqual((self.parent_of(1), self.parent_of(3), self.parent_of(sq["id"])), (None, 1, None))

    def test_a_longer_loop_is_a_cycle_too(self):
        self.log("T-2", parent="T-1")
        self.log("T-3", parent="T-2")
        status, _, res = self.log("T-1", parent="T-3")
        self.assertEqual(status, 400, res)
        self.assertIn("itself", res["error"])

    def test_a_task_that_joined_a_squad_since_it_was_read_is_refused(self):
        real = door.load_object
        self.assertEqual(self.post("/api/squads/assign", ref="T-3", squad_id="sq-1")[0], 200)
        # the call read T-3 while it was still in the backlog; by the time it writes, it is a squad's
        with mock.patch.object(door, "load_object", side_effect=lambda *a, **k: dict(real(*a, **k), squad_id=None)):
            status, _, res = self.log("T-3", parent="T-1")
        self.assertEqual(status, 400, res)
        self.assertIn("only a backlog task", res["error"])
        self.assertIsNone(self.parent_of(3))
        self.assertEqual([e["kind"] for e in self.store.object_log(3, 5)], ["assign", "create"])

    def test_two_crossing_re_parents_leave_no_loop(self):
        self.assertEqual(self.log("T-1", parent="T-2")[0], 200)
        status, _, res = self.log("T-2", parent="T-1")
        self.assertEqual(status, 400, res)
        self.assertEqual((self.parent_of(1), self.parent_of(2)), (2, None))
        self.assertEqual(self.listed(), ["T-2", "T-1", "T-3"], "nothing vanished from the list")

    def test_the_tool_and_the_cli_send_an_empty_parent(self):
        seen = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30: seen.append((path, body)) or {})
        client.call_tool("mesh_log", {"ref": "T-3", "parent": ""})
        self.assertEqual(seen[-1], ("/api/mesh/object/log", dict(ref="T-3", parent="")))
        mcp_mesh._run_cli(["log", "T-3", "--parent", "T-1"], client)
        self.assertEqual(seen[-1][1], dict(ref="T-3", parent="T-1"))
        mcp_mesh._run_cli(["log", "T-3", "note", "--parent", ""], client)
        self.assertEqual(seen[-1][1], dict(ref="T-3", body="note", parent=""))


class AmendTests(TreeRig):
    def setUp(self):
        super().setUp()
        self.backlog(title="Old title", body="Old body", files=["almanac/door.py", "almanac/store.py"])   # T-1
        self.squad_task("Squad slice")                                                                     # T-2

    def test_the_operator_amends_and_the_log_keeps_the_old_text(self):
        status, _, res = self.amend("T-1", title="New title", body="New body", files=["almanac/app.py"])
        self.assertEqual(status, 200, res)
        o = res["object"]
        self.assertEqual((o["title"], o["body"], o["files"]), ("New title", "New body", ["almanac/app.py"]))
        row = self.store.object_row(1)
        self.assertEqual((row["title"], row["body"], row["files"]), ("New title", "New body", ["almanac/app.py"]))
        entry = self.store.object_log(1, 1)[0]
        self.assertEqual((entry["kind"], entry["agent_name"], res["entry"]["kind"]), ("amend", "operator", "amend"))
        for old in ("title, body, files", "title: Old title", "files: almanac/door.py, almanac/store.py", "body:\nOld body"):
            self.assertIn(old, entry["body"])

    def test_only_what_is_sent_changes_and_files_may_be_cleared(self):
        self.assertEqual(self.amend("T-1", body="Only the body")[0], 200)
        row = self.store.object_row(1)
        self.assertEqual((row["title"], row["body"], len(row["files"])), ("Old title", "Only the body", 2))
        self.assertIn("amended body.", self.store.object_log(1, 1)[0]["body"])
        self.assertEqual(self.amend("T-1", files=[])[0], 200)
        self.assertEqual(self.store.object_row(1)["files"], [])
        self.assertEqual(self.amend("T-1", body="")[0], 200)
        self.assertEqual(self.store.object_row(1)["body"], "")
        self.assertIn("body:\nOnly the body", self.store.object_log(1, 1)[0]["body"])

    def test_who_may(self):
        self.assertEqual(self.amend("T-1", IMPL, title="By impl")[0], 200, "any agent of the project, for a backlog task")
        self.assertEqual(self.amend("T-1", ARCH, title="By arch")[0], 200)
        self.assertEqual(self.store.object_log(1, 1)[0]["agent_name"], "arch")
        self.assertEqual(self.amend("T-2", ARCH, title="By the lead")[0], 200, "a squad's task: its lead")
        status, _, res = self.amend("T-2", IMPL, title="By impl")
        self.assertEqual(status, 403, res)
        self.assertIn("may not amend", res["error"])
        self.assertEqual(self.amend("T-2", web.agent_token(TOKEN, "other", "sess-other"), title="Not mine")[0], 403,
                         "another squad's lead")
        self.assertEqual(self.amend("T-1", FAR, title="Not mine")[0], 403, "another project's agent")
        self.assertEqual(self.amend("T-2", title="By the operator")[0], 200)
        self.assertEqual(self.store.object_row(2)["title"], "By the operator")
        self.assertEqual(self.store.object_row(1)["title"], "By arch")

    def test_the_checks_are_filings(self):
        before = self.store.object_row(1)
        cases = {"nothing": dict(), "same text": dict(title="Old title", body="Old body"),
                 "empty title": dict(title="  "), "long title": dict(title="x" * (door.TITLE_MAX + 1)),
                 "long body": dict(body="x" * (door.BODY_MAX + 1)), "body not text": dict(body=3),
                 "outside the repo": dict(files=["../outside"]), "files not a list": dict(files=5),
                 "too many": dict(files=[f"f{n}.py" for n in range(door.FILES_MAX + 1)])}
        for label, body in cases.items():
            status, _, res = self.amend("T-1", **body)
            self.assertEqual(status, 400, (label, res))
        self.assertEqual(self.amend("T-99", title="x")[0], 404)
        self.assertEqual(self.store.object_row(1), before)
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["create"])

    def test_the_tool_and_the_cli(self):
        seen = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30: seen.append((path, body)) or {})
        self.assertIn("mesh_amend", [t["name"] for t in mcp_mesh.MCP_TOOLS])
        client.call_tool("mesh_amend", {"ref": "T-1", "title": "t", "files": []})
        self.assertEqual(seen[-1], ("/api/mesh/object/amend", dict(ref="T-1", title="t", files=[])))
        mcp_mesh._run_cli(["amend", "T-1", "--body", "b", "--files", "a.py", "b.py"], client)
        self.assertEqual(seen[-1][1], dict(ref="T-1", body="b", files=["a.py", "b.py"]))
        with self.assertRaises(ValueError):
            client.call_tool("mesh_amend", {})


class MoveTests(TreeRig):
    def setUp(self):
        super().setUp()
        self.backlog(title="Feature", roster=ROSTER)                                  # T-1
        self.backlog(title="Slice A", parent="T-1")                                   # T-2 queued
        self.backlog(title="Slice B", parent="T-1")                                   # T-3 accepted
        self.backlog(title="Sub-slice of A", parent="T-2")                            # T-4 queued
        self.backlog(title="Sub-slice of B", parent="T-3")                            # T-5 queued, under a child that stays
        self.assertEqual(self.log("T-3", status="accepted", body="not needed")[0], 200)

    def check_moved(self, children, squad):
        self.assertEqual(children["moved"], ["T-2", "T-4"])
        self.assertEqual(children["left"], [dict(ref="T-3", why="accepted")])
        for n in (2, 4):
            row = self.store.object_row(n)
            self.assertEqual((row["squad_id"], row["status"], row["owner_session"]), (squad, "queued", None))
            entry = self.store.object_log(n, 1)[0]
            self.assertEqual((entry["kind"], entry["agent_name"]), ("assign", "operator"))
            self.assertIn(f"with its parent T-{row['parent_id']}", entry["body"])
        for n in (3, 5):
            self.assertIsNone(self.store.object_row(n)["squad_id"], "stays where it is")
        self.assertEqual(self.listed(), ["T-3", "T-5"])
        note = self.store.object_log(1, 1)[0]["body"]
        self.assertIn("moved with it: T-2, T-4", note)
        self.assertIn("T-3 (accepted)", note)

    def test_dispatch_brings_the_queued_children_along_and_says_which_stay(self):
        status, _, res = self.dispatch("T-1")
        self.assertEqual(status, 200, res)
        self.check_moved(res["children"], res["squad"]["id"])
        self.assertEqual(res["object"]["last_log"]["body"][:25], "its slices moved with it:", "read after the move")
        self.assertTrue(any("Its slices T-2, T-4 are already filed" in a for argv in self.argv for a in argv),
                        "the lead is told, not asked to file them again")
        _, _, plan = self.request(self.handler, f"/api/mesh/objects?squad={res['squad']['id']}", token=TOKEN)
        self.assertEqual([(o["ref"], o["parent"]) for o in plan["objects"]],
                         [("T-1", None), ("T-2", "T-1"), ("T-4", "T-2")], "the plan shows the feature with its slices")

    def test_assign_does_too_and_the_lead_hears_of_them(self):
        status, _, res = self.post("/api/squads/assign", ref="T-1", squad_id="sq-1")
        self.assertEqual(status, 200, res)
        self.check_moved(res["children"], "sq-1")
        [told] = self.inbox["arch"]
        self.assertIn("T-2, T-4", told["body"])

    def test_a_parent_without_children_is_as_before(self):
        self.backlog(title="Alone")                                                   # T-6
        status, _, res = self.post("/api/squads/assign", ref="T-6", squad_id="sq-1")
        self.assertEqual((status, res["children"]), (200, dict(moved=[], left=[])), res)
        self.assertEqual([e["kind"] for e in self.store.object_log(6, 10)], ["assign", "create"])

    def test_a_child_goes_alone_keeps_its_parent_and_the_counts_follow_it(self):
        self.assertEqual(self.post("/api/squads/assign", ref="T-2", squad_id="sq-1")[0], 200)
        row = self.store.object_row(2)
        self.assertEqual((row["squad_id"], row["parent_id"]), ("sq-1", 1))
        self.assertEqual(self.store.object_row(4)["squad_id"], "sq-1", "it is a parent itself: its slice moved with it")
        _, _, plan = self.request(self.handler, "/api/mesh/objects?squad=sq-1", token=TOKEN)
        self.assertEqual([(o["ref"], o["parent"]) for o in plan["objects"]], [("T-2", "T-1"), ("T-4", "T-2")],
                         "a task whose parent is not in the squad is a root of its plan")
        # the feature's progress counts its slices wherever they are: T-2 in the squad, T-3 accepted in the backlog
        _, _, res = self.request(self.handler, "/api/mesh/object?ref=T-1", token=TOKEN)
        self.assertEqual(res["object"]["children"], dict(total=2, accepted=1))
        self.assertEqual(self.log("T-2", ARCH, status="accepted", body="done")[0], 200)
        _, _, res = self.request(self.handler, "/api/mesh/object?ref=T-1", token=TOKEN)
        self.assertEqual(res["object"]["children"], dict(total=2, accepted=2))
        listed = self.request(self.handler, "/api/mesh/objects?backlog=1", token=TOKEN)[2]["objects"]
        self.assertEqual({o["ref"]: o["children"] for o in listed if o["ref"] in ("T-1", "T-3")},
                         {"T-1": dict(total=2, accepted=2), "T-3": dict(total=1, accepted=0)})

    def test_a_child_being_dispatched_or_in_another_squad_stays(self):
        self.store.log_object(4, who=dict(agent_name="operator"), kind="assign", squad_id="sq-2", body="elsewhere")
        door._DISPATCHING.add(2)
        try:
            status, _, res = self.post("/api/squads/assign", ref="T-1", squad_id="sq-1")
        finally:
            door._DISPATCHING.discard(2)
        self.assertEqual(status, 200, res)
        self.assertEqual(res["children"]["moved"], [])
        self.assertEqual(res["children"]["left"], [dict(ref="T-2", why="being dispatched"), dict(ref="T-3", why="accepted")])
        self.assertEqual(self.store.object_row(4)["squad_id"], "sq-2")
