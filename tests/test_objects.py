"""Stage 1, tasks as objects: the `objects` / `object_log` store, the four `/api/mesh/` object routes, `re=` on
mesh_send, `objects_rev`, the squad rules, caps, and the caller resolved by session (two squads sharing a handle)."""
import json
import os
import subprocess
import urllib.parse
from unittest import mock

from support import TOKEN, runner_probe
from test_fleet import CLI
from test_server_fixes import MeshRig
from test_spawn_fixes import git

from almanac import spawner, uds, web
from almanac.door import Service, make_handler
from almanac.store import Store


class ObjectRig(MeshRig):
    def setUp(self):
        super().setUp()
        # the temp folder sits inside Almanac's own checkout: a hand-in must not read that repo's HEAD
        ceiling = mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.root.resolve()))
        ceiling.start()
        self.addCleanup(ceiling.stop)
        for name, squad in (("arch", "sq-1"), ("impl", "sq-1"), ("other", "sq-2")):
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id=squad, role_id=None,
                                    provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None,
                                    pid=0)
        for squad in ("sq-1", "sq-2"):
            self.store.create_squad(squad, squad, str(self.root))

    def tok(self, name):
        return web.agent_token(TOKEN, name, f"sess-{name}")

    def post(self, path, token=TOKEN, **body):
        return self.request(self.handler, path, token=token, method="POST", body=body)

    def file(self, token=TOKEN, **body):
        if token == TOKEN:
            body.setdefault("squad", "sq-1")
        body.setdefault("title", "a task")
        return self.post("/api/mesh/object", token, **body)

    def log(self, token=TOKEN, **body):
        return self.post("/api/mesh/object/log", token, **body)

    def get(self, path, token=TOKEN, **query):
        return self.request(self.handler, f"{path}?{urllib.parse.urlencode(query)}", token=token)

    def texts(self, name):
        return [m["body"] if isinstance(m, dict) and "body" in m else str(m) for m in self.inbox[name]]


class FileTests(ObjectRig):
    def test_filing_with_an_owner_assigns_it(self):
        status, _, res = self.file(title="Build the store", body="v10 tables", owner="impl", files=["almanac\\store.py"])
        self.assertEqual(status, 200, res)
        obj = res["object"]
        self.assertEqual((obj["ref"], obj["kind"], obj["status"], obj["owner_name"], obj["owner_session"]),
                         ("T-1", "task", "in progress", "impl", "sess-impl"))
        self.assertEqual((obj["files"], obj["squad_id"], obj["created_by_name"], obj["parent"]),
                         (["almanac/store.py"], "sq-1", "operator", None))
        self.assertIn(obj["owner_state"], ("busy", "idle"))
        self.assertIs(res["delivered"], True)
        self.assertEqual(len(self.inbox["impl"]), 1)
        self.assertIn("[T-1 assigned to you] Build the store", json.dumps(self.inbox["impl"][0]))
        _, _, doc = self.get("/api/mesh/object", ref="T-1")
        self.assertEqual([e["kind"] for e in doc["log"]], ["assign", "create"], "newest first")
        self.assertEqual(doc["object"]["body"], "v10 tables")
        msg = self.store.message(doc["log"][0]["message_id"])
        self.assertEqual((msg["from_name"], msg["to_name"], msg["squad_id"]), ("operator", "impl", "sq-1"))
        self.assertEqual(set(doc["log"][0]), {"id", "ts", "agent_name", "role_id", "provider", "kind", "body",
                                              "status_from", "status_to", "owner_to", "message_id", "sha"})

    def test_unowned_is_queued_and_sends_nothing(self):
        status, _, res = self.file()
        self.assertEqual((status, res["object"]["status"], res["object"]["owner_state"], res["delivered"]),
                         (200, "queued", None, None))
        self.assertEqual(self.heard(), set())

    def test_an_agent_files_in_its_own_squad_only(self):
        status, _, res = self.file(self.tok("arch"), owner="impl")
        self.assertEqual((status, res["object"]["squad_id"], res["object"]["created_by_name"]), (200, "sq-1", "arch"))
        self.assertEqual(self.store.object_row(1)["created_by"], "sess-arch")
        self.assertEqual(self.file(self.tok("arch"), squad="sq-2")[0], 403)
        self.assertEqual(self.file(self.tok("arch"), owner="other")[0], 400, "an owner is a member of the squad")
        self.assertEqual(self.file(web.agent_token(TOKEN, "outsider", "sess-outsider"))[0], 403, "in no squad")

    def test_the_operator_names_a_squad_unless_one_is_live(self):
        self.assertEqual(self.post("/api/mesh/object", title="x")[0], 400, "two live squads")
        self.assertEqual(self.file(squad="sq-9")[0], 404)
        self.assertEqual(self.get("/api/mesh/objects")[0], 400)

    def test_caps_and_bad_input(self):
        cases = [dict(title=""), dict(title="x" * 121), dict(body="x" * 8001), dict(kind="finding"),
                 dict(files=[f"f{i}" for i in range(51)]), dict(owner="ghost"), dict(status="done"),
                 dict(parent="T-99"), dict(files=["C:/elsewhere/x.py"]), dict(files=7)]
        for body in cases:
            status, _, res = self.file(**body)
            self.assertEqual(status, 400, (body, res))
        self.assertIn("trim", self.file(body="x" * 8001)[2]["error"])
        self.assertEqual(self.file(kind="finding")[2]["error"], "kind finding: not yet")
        self.assertEqual(self.file(title="x" * 120, files="a.py\n./b.py\n\n")[2]["object"]["files"], ["a.py", "b.py"])
        root = str(self.root).replace("\\", "/")
        self.assertEqual(self.file(files=[f"{root}/almanac/x.py"])[2]["object"]["files"], ["almanac/x.py"])
        self.assertEqual(self.store.objects_rev(), 2, "refused filings wrote nothing")

    def test_a_parent_is_a_task_of_the_same_squad(self):
        self.file(self.tok("other"))                        # T-1 in sq-2
        self.assertEqual(self.file(parent="T-1")[0], 400)
        self.assertEqual(self.file(squad="sq-2", parent="1")[2]["object"]["parent"], "T-1")


class ListTests(ObjectRig):
    def test_tree_order_children_and_filters(self):
        a = self.file(title="A")[2]["object"]["ref"]                         # T-1
        b = self.file(title="B", parent=a, owner="impl")[2]["object"]["ref"]  # T-2
        self.file(title="C needle")                                           # T-3
        self.file(title="D", parent=b, status="accepted")                     # T-4
        self.file(title="E", parent=a, status="accepted")                     # T-5
        status, _, res = self.get("/api/mesh/objects", squad="sq-1")
        self.assertEqual(status, 200, res)
        self.assertEqual([o["ref"] for o in res["objects"]], ["T-1", "T-2", "T-4", "T-5", "T-3"])
        top = res["objects"][0]
        self.assertEqual((top["children"], top["log_count"], top["last_log"]["kind"]),
                         ({"total": 2, "accepted": 1}, 1, "create"))
        self.assertNotIn("body", top)
        self.assertEqual(res["rev"], self.store.objects_rev())
        refs = lambda **q: [o["ref"] for o in self.get("/api/mesh/objects", squad="sq-1", **q)[2]["objects"]]
        self.assertEqual(refs(status="accepted"), ["T-4", "T-5"])
        self.assertEqual(refs(status="queued,accepted"), ["T-1", "T-5", "T-3", "T-4"], "T-4's parent is filtered out")
        self.assertEqual(refs(status="queued,accepted", limit=2), ["T-1", "T-5"])
        self.assertEqual(refs(owner="impl"), ["T-2"])
        self.assertEqual(refs(owner="none"), ["T-1", "T-5", "T-3", "T-4"])
        self.assertEqual(refs(q="needle"), ["T-3"])
        self.assertEqual(refs(q="%"), [], "LIKE wildcards are literal")
        self.assertEqual(self.get("/api/mesh/objects", squad="sq-1", status="done")[0], 400)

    def test_an_agent_sees_only_its_squad(self):
        self.file()
        self.file(squad="sq-2")
        self.assertEqual([o["ref"] for o in self.get("/api/mesh/objects", self.tok("other"))[2]["objects"]], ["T-2"])
        self.assertEqual(self.get("/api/mesh/objects", self.tok("other"), squad="sq-1")[0], 403)
        for ref in ("T-1", "1", "t-1"):
            self.assertEqual(self.get("/api/mesh/object", self.tok("other"), ref=ref)[0], 403, ref)
        self.assertEqual(self.get("/api/mesh/object", self.tok("arch"), ref="T-1")[0], 200)
        self.assertEqual(self.get("/api/mesh/object", ref="T-99")[0], 404)
        self.assertEqual(self.get("/api/mesh/object", ref="F-1")[0], 400, "no findings in stage 1")
        self.assertEqual(self.get("/api/mesh/object", ref="nope")[0], 400)

    def test_objects_rev_on_the_mesh_poll(self):
        self.assertEqual(self.get("/api/mesh")[2]["objects_rev"], 0)
        self.file()
        self.assertEqual(self.get("/api/mesh")[2]["objects_rev"], self.store.objects_rev())
        self.assertGreater(self.store.objects_rev(), 0)


class LogTests(ObjectRig):
    def setUp(self):
        super().setUp()
        self.file(self.tok("arch"), title="Build", owner="impl")           # T-1, created by arch
        for got in self.inbox.values():
            got.clear()

    def test_a_status_change_notices_the_creator_once(self):
        status, _, res = self.log(self.tok("impl"), ref="T-1", status="in review", body="store.py; 12 tests pass")
        self.assertEqual(status, 200, res)
        e = res["entry"]
        self.assertEqual((e["kind"], e["status_from"], e["status_to"], e["agent_name"]),
                         ("status", "in progress", "in review", "impl"))
        self.assertEqual(res["object"]["status"], "in review")
        self.assertEqual(self.heard(), {"arch"})
        self.assertIn("[T-1 in progress → in review by impl] Build", json.dumps(self.inbox["arch"][0], ensure_ascii=False))
        self.assertEqual(self.store.object_row(1)["status"], "in review")
        self.assertEqual(len(self.store.object_log(1, 50)), 3, "the notice is not a log row")

    def test_a_plain_entry_or_the_creators_change_notifies_nobody(self):
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", kind="learned", body="sqlite is fine")[2]["entry"]["kind"],
                         "learned")
        self.log(self.tok("arch"), ref="T-1", status="blocked")
        self.assertEqual(self.heard(), set())
        self.log(ref="T-1", status="accepted")                    # the operator did it: arch created it
        self.assertEqual(self.heard(), {"arch"})

    def test_body_needed_unless_something_changes(self):
        self.assertEqual(self.log(self.tok("impl"), ref="T-1")[0], 400)
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", status="in progress")[0], 400, "no change")
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", kind="status", body="x")[0], 400)
        self.assertEqual(self.log(self.tok("impl"), ref="T-1", body="x" * 8001)[0], 400)
        self.assertEqual(self.log(self.tok("other"), ref="T-1", body="x")[0], 403)
        self.assertEqual(self.log(self.tok("impl"), ref="T-9", body="x")[0], 404)

    def test_changing_the_owner_assigns_it(self):
        status, _, res = self.log(self.tok("impl"), ref="T-1", owner="arch", body="yours")
        self.assertEqual((status, res["entry"]["kind"], res["entry"]["owner_to"], res["delivered"]), (200, "assign", "arch", True))
        self.assertIsNotNone(res["entry"]["message_id"])
        self.assertEqual((res["object"]["owner_name"], res["object"]["owner_session"]), ("arch", "sess-arch"))
        self.assertIn("[T-1 assigned to you]", json.dumps(self.inbox["arch"][0]))

    def test_taking_a_task_yourself_sends_no_message(self):
        status, _, res = self.file(self.tok("arch"), title="mine", owner="arch")
        self.assertEqual((status, res["delivered"], res["object"]["owner_name"]), (200, None, "arch"))
        status, _, res = self.log(self.tok("arch"), ref="T-1", owner="arch", body="taking it over")   # T-1 was impl's
        self.assertEqual((status, res["entry"]["kind"], res["entry"]["owner_to"], res["entry"]["message_id"], res["delivered"]),
                         (200, "assign", "arch", None, None))
        self.assertEqual(self.heard(), set(), "nobody is told they assigned a task to themselves")
        self.assertEqual([e["kind"] for e in self.store.object_log(2, 5)], ["assign", "create"], "the row is still written")
        self.assertEqual(self.store.object_log(2, 1)[0]["message_id"], None)

    def test_an_ended_owner_gets_the_row_and_an_undelivered_message(self):
        self.store.record_spawn(agent_name="gone", session_id="sess-gone", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=0)
        status, _, res = self.file(title="later", owner="gone")
        self.assertEqual((status, res["delivered"], res["object"]["owner_state"]), (200, False, "ended"))
        msg = self.store.message(self.store.object_log(res["object"]["id"], 1)[0]["message_id"])
        self.assertEqual((msg["to_name"], msg["transport"]), ("gone", "undelivered"))


class FlagTests(ObjectRig):
    def test_hub_messages_about_tasks_close_no_open_flag(self):
        self.send(self.tok("impl"), to="arch", text="which table?", needs="reply")
        self.file(self.tok("arch"), owner="impl")                                  # arch -> impl, assignment
        self.assertEqual(len(self.store.open_needs(squad_id="sq-1")), 1, "the assignment is no answer")
        self.send(self.tok("arch"), to="impl", text="status?", needs="blocking")   # a real message: closes impl's
        self.assertEqual([m["body"] for m in self.store.open_needs(squad_id="sq-1")], ["status?"])
        self.log(self.tok("impl"), ref="T-1", status="in review")                  # impl -> arch, notice
        self.assertEqual(self.heard(), {"arch", "impl"})
        self.assertEqual(len(self.store.open_needs(squad_id="sq-1")), 1, "the status notice is no answer")
        self.send(self.tok("impl"), to="arch", text="in review now")               # a real answer still closes
        self.assertEqual(self.store.open_needs(squad_id="sq-1"), [])


class ReTests(ObjectRig):
    def test_re_logs_the_message_on_the_task(self):
        self.file(owner="impl")
        status, _, res = self.send(self.tok("arch"), to="impl", text="use v10", re="T-1")
        self.assertEqual(status, 200, res)
        entry = self.store.object_log(1, 1)[0]
        self.assertEqual((entry["kind"], entry["body"], entry["message_id"], entry["agent_name"]),
                         ("message", "use v10", res["message"]["id"], "arch"))

    def test_an_unknown_or_foreign_re_is_refused_before_sending(self):
        self.file(squad="sq-2")                                      # T-1 is sq-2's
        for re in ("T-9", "T-1", "bogus"):
            status, _, res = self.send(self.tok("arch"), to="impl", text="x", re=re)
            self.assertEqual(status, 400, (re, res))
        self.assertEqual(self.heard(), set())
        self.assertEqual(self.store.messages(), [])


class TwinSquadTests(ObjectRig):
    """Two squads that both have an `arch`: the session in the token decides which one is calling."""

    def setUp(self):
        super().setUp()
        self.inbox["arch2"] = []
        sessions = self.root / "claude" / "sessions"
        peer = uds.PeerServer(sessions, name="arch", pid=900, session_id="sess-arch2", cwd=str(self.root),
                              provider="claude", squad_id="sq-2", channels=["#squad"],
                              on_message=self.inbox["arch2"].append, in_memory=True).start()
        self.addCleanup(peer.close)
        self.machine.procs[900] = dict(ppid=1, exe="claude.exe", path=CLI, cmd=f'"{CLI}"',
                                       created=int(peer.proc_start), cpu=0, mem=1 << 20)
        self.store.record_spawn(agent_name="arch", session_id="sess-arch2", squad_id="sq-2", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=900)

    def test_each_arch_sends_and_files_in_its_own_squad(self):
        one, two = web.agent_token(TOKEN, "arch", "sess-arch"), web.agent_token(TOKEN, "arch", "sess-arch2")
        self.assertEqual(self.file(one)[2]["object"]["squad_id"], "sq-1")
        self.assertEqual(self.file(two)[2]["object"]["squad_id"], "sq-2")
        self.assertEqual(self.send(one, to="@squad", text="a")[2]["message"]["squad_id"], "sq-1")
        self.assertEqual(self.heard(), {"impl"})
        self.inbox["impl"].clear()
        self.assertEqual(self.send(two, to="@squad", text="b")[2]["message"]["squad_id"], "sq-2")
        self.assertEqual(self.heard(), {"other"})

    def test_a_handle_reaches_the_senders_squad_first(self):
        status, _, res = self.send(self.tok("other"), to="arch", text="hi")
        self.assertEqual((status, res["message"]["to_session"]), (200, "sess-arch2"))
        self.assertEqual(self.heard(), {"arch2"})
        self.inbox["arch2"].clear()
        self.send(self.tok("impl"), to="arch", text="hi")
        self.assertEqual(self.heard(), {"arch"})

    def test_an_old_two_part_token_resolves_by_handle(self):
        old = web.agent_token(TOKEN, "arch")
        self.assertEqual(self.file(old)[2]["object"]["squad_id"], "sq-2", "the newest spawn of that handle")

    def test_an_agents_reads_default_to_its_own_squad(self):
        self.send(web.agent_token(TOKEN, "arch", "sess-arch"), to="@squad", text="one")
        self.send(web.agent_token(TOKEN, "arch", "sess-arch2"), to="@squad", text="two")
        self.send(TOKEN, to="@all", text="everyone")
        got = self.get("/api/mesh/messages", web.agent_token(TOKEN, "arch", "sess-arch"), peer="arch")[2]["messages"]
        self.assertEqual([m["body"] for m in got], ["one", "everyone"])
        self.assertEqual(len(self.get("/api/mesh/messages", peer="arch")[2]["messages"]), 3, "the operator sees all")


class McpTests(ObjectRig):
    """The four tools, `re` and the CLI verbs, through the real door."""

    def client(self, name):
        from almanac import mcp_mesh

        def api(path, method="GET", body=None, timeout=30.0):
            status, _, doc = self.request(self.handler, path, token=self.tok(name), method=method, body=body)
            if status >= 400:
                raise RuntimeError(doc.get("error"))
            return doc
        return mcp_mesh.MeshClient(url="http://x", token="t", self_name=name, squad_id="sq-1", api_fn=api)

    def test_file_list_read_log_and_re(self):
        arch, impl = self.client("arch"), self.client("impl")
        res = arch.call_tool("mesh_file", {"kind": "task", "title": "Store", "body": "v10", "owner": "impl",
                                           "files": ["almanac/store.py"]})
        self.assertEqual((res["object"]["ref"], res["delivered"]), ("T-1", True))
        self.assertEqual([o["ref"] for o in impl.call_tool("mesh_objects", {"owner": "impl"})["objects"]], ["T-1"])
        impl.call_tool("mesh_send", {"to": "arch", "message": "on it", "re": "T-1"})
        impl.call_tool("mesh_log", {"ref": "T-1", "status": "in review", "body": "done; 263 OK"})
        doc = arch.call_tool("mesh_object", {"ref": "T-1", "log_limit": 10})
        self.assertEqual([e["kind"] for e in doc["log"]], ["status", "message", "assign", "create"])
        with self.assertRaises(RuntimeError):
            arch.call_tool("mesh_object", {"ref": "T-9"})

    def test_cli_verbs(self):
        import contextlib
        import io
        from almanac import mcp_mesh
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mcp_mesh._run_cli(["file", "From the shell", "--body", "b", "--files", "a.py", "b.py", "--owner", "impl"],
                              self.client("arch"))
            mcp_mesh._run_cli(["log", "T-1", "learned", "a", "thing", "--kind", "learned"], self.client("impl"))
            mcp_mesh._run_cli(["send", "arch", "hi", "--re", "T-1"], self.client("impl"))
            mcp_mesh._run_cli(["objects", "--status", "in progress"], self.client("arch"))
            mcp_mesh._run_cli(["object", "T-1", "--log-limit", "2"], self.client("arch"))
        row = self.store.object_row(1)
        self.assertEqual((row["title"], row["files"]), ("From the shell", ["a.py", "b.py"]))
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["message", "learned", "assign", "create"])
        self.assertEqual(self.store.object_log(1, 10)[1]["body"], "learned a thing")

    def test_gemini_is_granted_the_new_tools(self):
        from almanac import gemini_host
        for tool in ("mesh_objects", "mesh_object", "mesh_file", "mesh_log"):
            self.assertIn(f"mcp(almanac-mesh_almanac-mesh/{tool})", gemini_host.MESH_GRANTS)


from test_elastic import TASK, ElasticRig   # noqa: E402  (after the rigs above, which do not need it)


class SpawnTaskTests(ElasticRig):
    """Both spawn paths file the member's task as a T: unowned and queued first, the new session's and in progress
    after a successful spawn, and no assignment message (the kickoff carries the task)."""

    def test_mesh_spawn_files_a_t_owned_by_the_newcomer(self):
        status, _, res = self.request(self.handler, "/api/mesh/spawn", token=web.agent_token(TOKEN, "arch", "sess-arch"),
                                      method="POST", body={"task": TASK, "files": ["almanac/store.py"]})
        self.assertEqual(status, 200, res)
        row = self.store.object_row(1)
        self.assertEqual((row["title"], row["body"], row["status"], row["owner_session"], row["squad_id"]),
                         (TASK, TASK, "in progress", res["session_id"], "sq-1"))
        self.assertEqual((row["created_by"], row["created_by_name"], row["files"]), ("sess-arch", "arch", ["almanac/store.py"]))
        self.assertIn("## Your Initial Assignment (`T-1`)", self.argv[-1][-1])
        self.assertEqual([e["kind"] for e in self.store.object_log(1, 10)], ["assign", "create"])
        self.assertIsNone(self.store.object_log(1, 1)[0]["message_id"])
        self.assertEqual(self.store.object_log(1, 1)[0]["role_id"], "architect-claude", "the spawning agent's role")
        self.assertFalse([m for m in self.store.messages() if "assigned to you" in m["body"]], "no assignment message")

    def test_a_failed_spawn_leaves_the_t_queued_and_unowned(self):
        self.dies.add(1)
        self.assertEqual(self.request(self.handler, "/api/mesh/spawn", token=web.agent_token(TOKEN, "arch", "sess-arch"),
                                      method="POST", body={"task": TASK})[0], 409)
        row = self.store.object_row(1)
        self.assertEqual((row["status"], row["owner_session"]), ("queued", None))

    def test_a_squad_launch_files_one_t_per_member_with_a_task(self):
        status, _, res = self.request(self.handler, "/api/squads", method="POST", body={
            "name": "gamma", "cwd": str(self.ws), "brief": "b", "members": [
                {"role_id": "architect-claude", "agent_name": "lead", "workspace_mode": "shared",
                 "task": "Plan it.\nSecond line."},
                {"role_id": "implementer-claude", "agent_name": "doer", "workspace_mode": "shared"}]})
        self.assertEqual(status, 200, res)
        squad = res["squad"]["id"]
        rows = self.store.objects(squad_id=squad)
        self.assertEqual(len(rows), 1, "a member with only the mission gets no T")
        lead = next(a for a in res["squad"]["agents"] if a["agent_name"] == "lead")
        self.assertEqual((rows[0]["title"], rows[0]["status"], rows[0]["owner_session"], rows[0]["created_by_name"]),
                         ("Plan it.", "in progress", lead["session_id"], "operator"))
        self.assertIn(f"## Your Initial Assignment (`T-{rows[0]['id']}`)", self.argv[0][-1])
        self.assertIn("mesh_file(kind=", self.argv[0][self.argv[0].index("--append-system-prompt") + 1])


class ReviewFixTests(ObjectRig):
    """rev-gemini's review of b01fb6a..1c3cf90: B1 to B7."""

    def test_a_late_question_stays_open_after_a_notice(self):                       # B1
        self.file(self.tok("arch"), owner="impl")                                  # arch -> impl notice, now
        question = self.store.record_message(from_name="impl", to_name="arch", body="which table?", needs="blocking",
                                             ts=self.store.message(1)["ts"] - 10, squad_id="sq-1", transport="uds-transcript")
        self.assertIsNone(self.store.message(question["id"])["answered_ts"], "an assignment is no answer, even later")

    def test_a_status_notice_does_not_spend_the_reply_pass(self):                   # B4
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=0)
        self.file(owner="impl")                                                    # the operator created T-1
        self.send(TOKEN, to="impl", text="how is it going?")
        self.assertTrue(self.service.may_page_operator("impl", "sess-impl")[0])
        self.log(self.tok("impl"), ref="T-1", status="in review")                  # impl -> operator, a notice
        self.assertTrue(self.service.may_page_operator("impl", "sess-impl")[0], "the pass is still there")
        self.assertEqual(self.send(self.tok("impl"), to="operator", text="in review")[0], 200)
        self.assertFalse(self.service.may_page_operator("impl", "sess-impl")[0], "a real reply spends it")

    def test_mesh_spawn_carries_files(self):                                        # B2
        from almanac import mcp_mesh
        seen = []
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30: seen.append(body) or {})
        mcp_mesh._run_cli(["spawn", "build", "it", "--files", "a.py", "b/*.py"], client)
        self.assertEqual(seen[-1]["files"], ["a.py", "b/*.py"])
        spawn = next(t for t in mcp_mesh.MCP_TOOLS if t["name"] == "mesh_spawn")
        self.assertIn("files", spawn["inputSchema"]["properties"])

    def test_a_token_with_a_session_never_falls_back_to_the_handle(self):           # B5
        stranger = web.agent_token(TOKEN, "arch", "sess-unrecorded")
        self.assertEqual(self.file(stranger)[0], 403)
        self.assertIsNone(self.service.caller("arch", "sess-impl"), "a session of another handle")
        self.assertEqual(self.service.caller("arch", None)["session_id"], "sess-arch", "an old token: by handle")

    def test_a_session_being_spawned_is_waited_for(self):                           # B5
        import threading
        self.service.spawning = {"sess-new"}

        def record():
            self.store.record_spawn(agent_name="new", session_id="sess-new", squad_id="sq-1", role_id=None,
                                    provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=0)
        timer = threading.Timer(0.2, record)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self.service.caller("new", "sess-new")["squad_id"], "sq-1")

    def test_files_are_strings_inside_the_repo(self):                               # B6
        for files in (["../x.py"], ["a/../../b.py"], [None], [3], ["/abs.py"], [f"{self.root}/../x.py"]):
            self.assertEqual(self.file(files=files)[0], 400, files)

    def test_the_operator_inbox_does_not_list_the_hubs_notices_as_its_own(self):
        self.store.record_message(from_name="almanac-hub", to_name="impl", body="continue", answers=False, squad_id="sq-1")
        self.store.record_message(from_name="operator", to_name="impl", body="hello", squad_id="sq-1")
        self.assertEqual([m["body"] for m in self.store.operator_inbox()["messages"]], ["hello"])

    def test_mesh_file_needs_only_a_title(self):                                    # B7
        from almanac import mcp_mesh
        tool = next(t for t in mcp_mesh.MCP_TOOLS if t["name"] == "mesh_file")
        self.assertEqual(tool["inputSchema"]["required"], ["title"])


class LaunchValidationTests(ElasticRig):
    def test_a_bad_member_refuses_the_launch_before_anyone_starts(self):            # B3
        for bad in (dict(files=["../outside.py"]), dict(task="x" * 8001)):
            status, _, res = self.request(self.handler, "/api/squads", method="POST", body={
                "name": "delta", "cwd": str(self.ws), "brief": "b", "members": [
                    {"role_id": "architect-claude", "agent_name": "lead", "workspace_mode": "shared", "task": "Plan it."},
                    dict({"role_id": "implementer-claude", "agent_name": "doer", "workspace_mode": "shared",
                          "task": "Build it."}, **bad)]})
            self.assertEqual(status, 400, res)
        self.assertEqual(self.argv, [], "nobody was started")
        self.assertFalse([s for s in self.store.squads() if s["name"] == "delta"], "no squad row")


class HandinTests(ObjectRig):
    """T-46: a hand-in records the owner's head, branch and base; merge state is read from the squad folder."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        (self.repo / "a.py").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.store.db.execute("update squads set cwd = ? where id = 'sq-1'", (str(self.repo),))
        self.wt = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")
        branch, base = spawner.branch_and_base(self.wt)
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.wt),
                                worktree_path=str(self.wt), pid=0, branch=branch, base=base)
        self.store.record_spawn(agent_name="arch", session_id="sess-arch", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.repo), worktree_path=None,
                                pid=0)

    def commit(self, where, name):
        (where / name).write_text(name, encoding="utf-8")
        git(where, "add", "-A")
        git(where, "commit", "-q", "-m", name)
        return git(where, "rev-parse", "HEAD")

    def hand_in(self, ref, who="impl", body="done"):
        status, _, res = self.log(self.tok(who), ref=ref, status="in review", body=body)
        self.assertEqual(status, 200, res)
        return res

    def read(self, ref):
        return self.get("/api/mesh/object", ref=ref)[2]

    def branches(self):
        return self.get("/api/mesh/branches", squad="sq-1")[2]["branches"]

    def test_the_spawn_row_holds_the_branch_and_the_commit_it_was_cut_from(self):
        row = self.store.spawn_by_session("sess-impl")
        self.assertEqual((row["branch"], row["base"]), ("almanac/sq-1/impl", self.base))
        self.commit(self.wt, "w.py")
        self.assertEqual(spawner.branch_and_base(self.wt), ("almanac/sq-1/impl", self.base),
                         "a respawn into the same worktree reads the same base after commits")
        self.assertEqual(spawner.branch_and_base(self.root / "nowhere"), (None, None))
        self.assertIsNone(self.store.spawn_by_session("sess-arch")["branch"], "a shared-folder member has none")

    def test_a_hand_in_records_its_head_a_fix_round_moves_it_and_a_merge_turns_it_merged(self):
        self.file(owner="impl")
        first = self.commit(self.wt, "one.py")
        res = self.hand_in("T-1")
        obj = res["object"]
        self.assertEqual((obj["sha"], obj["branch"], obj["base"], obj["merged"]),
                         (first, "almanac/sq-1/impl", self.base, False))
        self.assertEqual((res["entry"]["sha"], res["entry"]["body"]), (first, "done"), "a clean worktree adds no note")
        self.assertEqual(self.branches(), [dict(agent_name="impl", branch="almanac/sq-1/impl", worktree=str(self.wt),
                                                ahead=1)])

        second = self.commit(self.wt, "two.py")                       # the fix round: still in review
        res = self.hand_in("T-1", body="fixed F1")
        self.assertEqual((res["object"]["sha"], res["entry"]["sha"], res["object"]["merged"]), (second, second, False))
        # T-30: a squad with a launched reviewer and no slot left has a hub row saying why no fresh reviewer started
        self.assertEqual([e["sha"] for e in self.read("T-1")["log"] if e["agent_name"] != "almanac-hub"][:2],
                         [second, first])
        self.assertEqual(self.branches()[0]["ahead"], 2)

        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "almanac/sq-1/impl")
        self.assertIs(self.read("T-1")["object"]["merged"], True, "nobody logged the merge")
        listed = self.get("/api/mesh/objects", squad="sq-1")[2]["objects"]
        self.assertEqual([(o["ref"], o["sha"], o["merged"]) for o in listed], [("T-1", second, True)])
        self.assertEqual(self.branches(), [], "no branch holds commits the checkout lacks")

    def test_an_owner_without_a_worktree_records_the_checkouts_head_and_no_branch(self):
        self.file(owner="arch")
        head = self.commit(self.repo, "lead.py")
        obj = self.hand_in("T-1", who="arch")["object"]
        self.assertEqual((obj["sha"], obj["branch"], obj["base"], obj["merged"]), (head, None, None, True))

    def test_a_squad_folder_outside_git_records_nothing_and_says_nothing(self):          # L1
        plain = self.root / "plain"
        plain.mkdir()
        self.store.db.execute("update squads set cwd = ? where id = 'sq-1'", (str(plain),))
        self.file(owner="arch")
        res = self.hand_in("T-1", who="arch")
        self.assertEqual((res["object"]["sha"], res["entry"]["sha"], res["entry"]["body"]), (None, None, "done"))

    def test_uncommitted_changes_are_said_on_the_log_row(self):
        self.file(owner="impl")
        head = self.commit(self.wt, "one.py")
        (self.wt / "loose.py").write_text("x", encoding="utf-8")
        res = self.hand_in("T-1")
        self.assertEqual(res["object"]["sha"], head)
        self.assertIn(f"1 uncommitted path(s) in {self.wt} are not in {head[:7]}", res["entry"]["body"])
        self.assertTrue(res["entry"]["body"].startswith("done"))

    def test_an_owner_set_in_the_same_call_is_the_one_whose_head_is_recorded(self):          # L3
        self.file()
        head = self.commit(self.wt, "one.py")
        status, _, res = self.log(ref="T-1", owner="impl", status="in review")
        self.assertEqual(status, 200, res)
        self.assertEqual((res["object"]["sha"], res["object"]["branch"]), (head, "almanac/sq-1/impl"))

    def test_a_branch_named_like_a_path_still_counts(self):                                     # L4
        self.file(owner="impl")
        self.commit(self.wt, "one.py")
        (self.repo / "almanac" / "sq-1").mkdir(parents=True)
        (self.repo / "almanac" / "sq-1" / "impl").write_text("x", encoding="utf-8")   # a path spelled as the branch
        git(self.repo, "tag", "almanac/sq-1/impl", self.base)                         # and a tag of the same name
        self.assertEqual([b["ahead"] for b in self.branches()], [1])
        self.assertIs(self.hand_in("T-1")["object"]["merged"], False)

    def test_branches_are_the_callers_squads_once_per_worktree_with_errors_kept(self):
        self.file(owner="impl")
        first = self.commit(self.wt, "one.py")
        self.hand_in("T-1")
        # a respawn: a second row on the same worktree, written before T-46 (no branch), now owns the task
        self.store.record_spawn(agent_name="impl", session_id="sess-impl2", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.wt),
                                worktree_path=str(self.wt), pid=0)
        self.store.log_object(1, who=dict(agent_name="operator"), kind="assign", owner_session="sess-impl2")
        second = self.commit(self.wt, "two.py")
        obj = self.hand_in("T-1", body="fixed")["object"]
        self.assertEqual((obj["sha"], obj["branch"], obj["base"]), (second, "almanac/sq-1/impl", self.base))
        self.assertNotEqual(first, second)
        ghost = self.root / "ghost"
        ghost.mkdir()
        self.store.record_spawn(agent_name="ghost", session_id="sess-ghost", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(ghost), worktree_path=str(ghost),
                                pid=0, branch="almanac/sq-1/ghost", base=self.base)
        status, _, res = self.get("/api/mesh/branches", token=self.tok("arch"))
        self.assertEqual(status, 200, res)
        self.assertEqual(res["squad"], "sq-1", "an agent reads its own squad by default")
        listed = {b["agent_name"]: b for b in res["branches"]}
        self.assertEqual(set(listed), {"impl", "ghost"}, "one row per worktree")
        self.assertEqual(listed["impl"]["ahead"], 2)
        self.assertIsNone(listed["ghost"]["ahead"])
        self.assertIn("failed", listed["ghost"]["error"], "a branch git cannot count is listed, not left out")
        self.assertEqual(self.get("/api/mesh/branches", token=self.tok("other"), squad="sq-1")[0], 403)

    def test_the_two_second_poll_runs_no_git(self):
        self.file(owner="impl")
        self.commit(self.wt, "one.py")
        self.hand_in("T-1")
        with mock.patch.object(spawner, "git_try", side_effect=AssertionError("git on the poll")) as called:
            status, _, res = self.get("/api/mesh")
        self.assertEqual(status, 200, res)
        called.assert_not_called()

    def test_a_pre_v12_database_gains_the_columns_and_reads_its_old_rows(self):
        path = self.root / "old" / "almanac.db"
        st = Store(path)
        for table, column in (("spawned_agents", "branch"), ("spawned_agents", "base"), ("objects", "base"),
                              ("object_log", "sha")):
            st.db.execute(f"alter table {table} drop column {column}")
        st.db.execute("insert into spawned_agents (session_id, agent_name, squad_id, provider, cwd, created_ts) "
                      "values ('s1', 'old', 'sq', 'claude', '.', 1)")
        st.db.execute("insert into objects (kind, project, squad_id, title, status, created_by_name, created_ts, "
                      "updated_ts) values ('task', 'p', 'sq', 'old task', 'queued', 'operator', 1, 1)")
        st.db.execute("insert into object_log (object_id, ts, agent_name, kind, body) values (1, 1, 'operator', 'create', '')")
        st.db.commit()
        st.close()
        for _ in range(2):                                      # the second open finds nothing to add
            st = Store(path)
            self.assertEqual((st.spawn_by_session("s1")["branch"], st.spawn_by_session("s1")["base"]), (None, None))
            row = st.object_row(1)
            self.assertEqual((row["title"], row["sha"], row["branch"], row["base"]), ("old task", None, None, None))
            self.assertIsNone(st.object_log(1, 5)[0]["sha"])
            st.close()

    def test_a_git_failure_or_timeout_never_refuses_the_hand_in(self):
        self.file(owner="impl")
        with mock.patch.object(spawner.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)):
            res = self.hand_in("T-1")
        self.assertEqual((res["object"]["status"], res["object"]["sha"], res["entry"]["sha"]), ("in review", None, None))
        self.assertIn("no commit recorded: git rev-parse timed out after 10s", res["entry"]["body"])
        self.store.db.execute("update spawned_agents set worktree_path = ? where session_id = 'sess-impl'",
                              (str(self.root / "gone"),))
        res = self.hand_in("T-1", body="again")
        self.assertIsNone(res["object"]["sha"])
        self.assertIn("no commit recorded: git rev-parse failed", res["entry"]["body"])
        self.assertIsNone(res["object"]["merged"])


class HandinNoticeTests(HandinTests):
    """T-37: one hand-in, one notice each to the lead and the squad's live reviewer; none to the sender."""

    PEERS = HandinTests.PEERS + (("rev", 900, "sq-1", ["#squad"]),)

    def setUp(self):
        super().setUp()
        self.store.record_spawn(agent_name="rev", session_id="sess-rev", squad_id="sq-1", role_id="reviewer-gemini",
                                provider="claude", model=None, effort=None, cwd=str(self.repo), worktree_path=None,
                                pid=0)

    def counts(self):
        return {name: len(got) for name, got in self.inbox.items() if got}

    def clear(self):
        for got in self.inbox.values():
            got.clear()

    def test_a_hand_in_and_a_fix_round_each_tell_the_lead_and_the_reviewer_once(self):
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        first = self.commit(self.wt, "one.py")
        self.hand_in("T-1", body="store.py; 12 OK")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})
        rev = json.dumps(self.inbox["rev"][0], ensure_ascii=False)
        for part in ("[T-1 in progress → in review by impl] a task", "Branch: almanac/sq-1/impl, commit: " + first[:7],
                     "store.py; 12 OK"):
            self.assertIn(part, rev)
        self.clear()
        second = self.commit(self.wt, "two.py")
        self.hand_in("T-1", body="fixed F1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1}, "a fix round is one event too")
        self.assertIn("in review again", json.dumps(self.inbox["arch"][0]))
        self.assertIn("commit: " + second[:7], json.dumps(self.inbox["rev"][0]))

    def test_a_reviewer_who_created_the_task_hears_once_and_no_commit_is_said(self):
        self.file(self.tok("rev"), owner="impl")
        self.clear()
        with mock.patch.object(spawner.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)):
            self.hand_in("T-1")
        self.assertEqual(self.counts(), {"rev": 1})
        self.assertIn("commit: no commit recorded", json.dumps(self.inbox["rev"][0]))

    def test_the_reviewer_handing_in_is_not_told(self):
        self.file(owner="rev")                                   # the operator created it
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")
        self.clear()
        self.hand_in("T-1", who="rev")
        self.assertEqual(self.counts(), {"arch": 1}, "rev logged it, so only the lead, standing in for the operator")

    def test_accepting_an_unmerged_sha_is_noted_and_never_refused(self):
        self.file(owner="impl")
        sha = self.commit(self.wt, "one.py")
        self.hand_in("T-1")
        status, _, res = self.log(ref="T-1", status="accepted")
        self.assertEqual((status, res["object"]["status"]), (200, "accepted"))
        self.assertEqual(res["entry"]["body"], f"almanac-hub: accepted with {sha[:7]} not in {self.repo}'s HEAD")
        self.file(owner="impl")                                  # T-2, merged before it is accepted
        self.hand_in("T-2")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "almanac/sq-1/impl")
        self.assertEqual(self.log(ref="T-2", status="accepted", body="ok")[2]["entry"]["body"], "ok")

    def test_a_creator_in_another_squad_has_the_lead_stand_in(self):                  # F1
        self.file(owner="impl")
        self.store.db.execute("update objects set created_by = 'sess-other', created_by_name = 'other' where id = 1")
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")  # arch leads
        self.clear()
        before = self.store.db.execute("select max(id) from messages").fetchone()[0] or 0
        self.hand_in("T-1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1}, "the lead stands in; other is not told")
        to = [r[0] for r in self.store.db.execute("select to_name from messages where id > ?", (before,))]
        self.assertEqual(sorted(to), ["arch", "operator", "rev"])
        self.clear()
        self.log(self.tok("arch"), ref="T-1", status="accepted")
        self.assertEqual(self.counts(), {}, "the lead accepting it is not told")


    def test_a_task_the_operator_filed_wakes_the_lead_too(self):                       # F1b
        self.file(owner="impl")                                  # created_by None, created_by_name operator
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")
        self.clear()
        before = self.store.db.execute("select max(id) from messages").fetchone()[0] or 0
        self.hand_in("T-1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})
        to = [r[0] for r in self.store.db.execute("select to_name from messages where id > ?", (before,))]
        self.assertEqual(sorted(to), ["arch", "operator", "rev"])
        self.clear()
        self.log(self.tok("arch"), ref="T-1", status="accepted")
        self.assertEqual(self.counts(), {}, "the lead accepting it is not told")

    def test_a_lead_named_as_creator_without_a_session_hears_once(self):
        self.file(owner="impl")
        self.store.db.execute("update objects set created_by_name = 'arch' where id = 1")
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")
        self.clear()
        self.hand_in("T-1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})

    def test_a_creator_whose_session_ended_has_the_live_lead_stand_in(self):             # R2
        self.file(self.tok("arch"), owner="impl")
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")
        self.store.record_spawn(agent_name="arch", session_id="sess-arch-old", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.repo), worktree_path=None,
                                pid=0)
        self.store.record_spawn(agent_name="arch", session_id="sess-arch", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.repo), worktree_path=None,
                                pid=0)                           # the live session is the newest
        self.store.db.execute("update objects set created_by = 'sess-arch-old' where id = 1")
        self.clear()
        self.hand_in("T-1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})

    def test_a_lead_who_reviews_hears_once_and_no_live_lead_leaves_the_operator(self):
        self.store.db.execute("update spawned_agents set role_id = 'reviewer-claude' where agent_name = 'arch'")
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name in ('impl', 'rev')")
        self.file(owner="impl")
        self.clear()
        self.hand_in("T-1")
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})
        self.store.db.execute("update spawned_agents set spawned_by = 'x' where agent_name = 'arch'")   # no lead
        self.clear()
        before = self.store.db.execute("select max(id) from messages").fetchone()[0]
        self.hand_in("T-1", body="again")
        to = sorted(r[0] for r in self.store.db.execute("select to_name from messages where id > ?", (before,)))
        self.assertEqual(to, ["arch", "operator", "rev"], "arch hears as a reviewer only")


class FreshReviewerTests(ObjectRig):
    """T-30: a task's first hand-in gets a fresh reviewer of the launched reviewer's role, started by the hub after
    the response; a fix round reaches it; accepting the task retires it; no slot or a failed start falls back to the
    standing reviewers. arch leads, rev is the standing reviewer, impl builds; the squad folder is outside git."""

    PEERS = ObjectRig.PEERS + (("rev", 900, "sq-1", ["#squad"]),)

    def setUp(self):
        super().setUp()
        self.service.probe = runner_probe(self.root, claude=True, gemini=False)
        self.service._runners = None
        self.store.record_spawn(agent_name="rev", session_id="sess-rev", squad_id="sq-1", role_id="reviewer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None,
                                pid=0)
        # the lead's mesh_spawn may add implementers only: the hub's reviewer is not bound by that
        self.store.update_squad("sq-1", spawn_role="implementer-claude", spawn_limit=2)
        self.argv = []
        real = subprocess.Popen

        def popen(argv, **kwargs):
            if argv[0] == "git":                           # the hand-in's head read runs for real
                return real(argv, **kwargs)
            self.argv.append(list(argv))
            return mock.MagicMock(pid=9000 + len(self.argv), **{"poll.return_value": None})
        patcher = mock.patch.object(spawner.subprocess, "Popen", side_effect=popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def counts(self):
        return {name: len(got) for name, got in self.inbox.items() if got}

    def clear(self):
        for got in self.inbox.values():
            got.clear()

    def hand_in(self, ref="T-1", body="done"):
        status, _, res = self.log(self.tok("impl"), ref=ref, status="in review", body=body)
        self.assertEqual(status, 200, res)
        return res

    def task(self, ref="T-1"):
        return self.get("/api/mesh/object", ref=ref, log_limit=50)[2]

    def hub_rows(self, ref="T-1"):
        return [e["body"] for e in self.task(ref)["log"] if e["agent_name"] == "almanac-hub"]

    def reviewer(self, ref="T-1"):
        return self.store.object_row(int(ref[2:]))["data"].get("reviewer")

    def go_live(self, name, sid, pid):
        """The started reviewer's process registers on the mesh, as a real one would."""
        self.inbox[name] = []
        peer = uds.PeerServer(self.root / "claude" / "sessions", name=name, pid=pid, session_id=sid,
                              cwd=str(self.root), provider="claude", squad_id="sq-1", channels=["#squad"],
                              on_message=self.inbox[name].append, in_memory=True).start()
        self.addCleanup(peer.close)
        self.machine.procs[pid] = dict(ppid=1, exe="claude.exe", path=CLI, cmd=f'"{CLI}"',
                                       created=int(peer.proc_start), cpu=0, mem=1 << 20)
        self.fleet.last["at"] = 0
        return peer

    def test_a_first_hand_in_starts_one_reviewer_of_the_launched_role_and_records_it(self):
        self.file(self.tok("arch"), owner="impl", title="store slice", body="Build the store slice.")
        self.clear()
        self.hand_in(body="store.py; 12 OK")
        self.assertEqual(len(self.argv), 1, "exactly one reviewer started")
        mine = self.reviewer()
        row = self.store.spawn_by_session(mine["session_id"])
        self.assertEqual((mine["agent_name"], row["role_id"], row["spawned_by"], row["worktree_path"]),
                         ("rev-claude", "reviewer-claude", "almanac-hub", None))
        self.assertEqual(self.store.squad("sq-1")["spawn_used"], 1, "it counts toward the limit")
        self.assertEqual(self.counts(), {"arch": 1}, "the standing reviewer is not also told: no double review")
        kickoff = self.argv[0][-1]
        for part in ("## Your task (`T-1`): store slice", "Build the store slice.", "store.py; 12 OK",
                     'mesh_diff(ref="T-1")', "lows to the owner `impl` only", "mediums and highs to the owner and the "
                     "lead `arch`", "edit nothing"):
            self.assertIn(part, kickoff)
        [note] = self.hub_rows()
        self.assertIn(f"rev-claude (reviewer-claude) started to review T-1, session {mine['session_id']}", note)

    def test_a_fix_round_starts_none_and_reaches_that_reviewer_while_it_lives(self):
        self.file(self.tok("arch"), owner="impl")
        self.hand_in()
        mine = self.reviewer()
        peer = self.go_live(mine["agent_name"], mine["session_id"], 9001)
        self.clear()
        self.hand_in(body="fixed F1")
        self.assertEqual(len(self.argv), 1, "a fix round starts nobody")
        self.assertEqual(self.counts(), {"arch": 1, "rev-claude": 1})
        self.assertIn("in review again", json.dumps(self.inbox["rev-claude"][0]))
        self.log(self.tok("impl"), ref="T-1", status="in progress", body="one more")
        self.clear()
        self.hand_in(body="fixed F2")                      # in review from another status, a reviewer on record
        self.assertEqual((len(self.argv), self.counts()), (1, {"arch": 1, "rev-claude": 1}))
        peer.close()                                       # it ended: the standing reviewer takes the fix round
        self.machine.procs.pop(9001)
        self.fleet.last["at"] = 0
        self.clear()
        self.hand_in(body="fixed F3")
        self.assertEqual((len(self.argv), self.counts()), (1, {"arch": 1, "rev": 1}))

    def test_no_slot_left_falls_back_to_the_standing_reviewer_with_a_log_row(self):
        self.store.update_squad("sq-1", spawn_limit=0)
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        res = self.hand_in()
        self.assertEqual((res["object"]["status"], self.argv), ("in review", []))
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})
        self.assertIn("[T-1 in progress → in review by impl]", json.dumps(self.inbox["rev"][0], ensure_ascii=False))
        [why] = self.hub_rows()
        self.assertIn("no fresh reviewer for T-1 (reviewer-claude): 0/0 of the squad's spawn limit used", why)
        self.assertIsNone(self.reviewer())

    def test_a_start_that_raises_or_fails_falls_back_and_the_hand_in_stands(self):
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        with mock.patch.object(spawner, "spawn_agent", side_effect=RuntimeError("provider rejected the model")), \
                self.assertLogs("almanac", "WARNING"):
            self.hand_in()
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1})
        self.assertIn("provider rejected the model", self.hub_rows()[0])
        # a reviewer whose runner is missing: spawn_agent answers ok=False, nothing raised
        self.store.db.execute("update spawned_agents set role_id = 'reviewer-gemini' where agent_name = 'rev'")
        self.store.db.commit()
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        self.hand_in("T-2")
        self.assertEqual((self.counts(), self.argv), ({"arch": 1, "rev": 1}, []))
        self.assertIn("Gemini runner not found", self.hub_rows("T-2")[0])
        self.assertEqual(self.store.squad("sq-1")["spawn_used"], 0)

    def test_a_squad_launched_without_a_reviewer_is_as_before(self):
        self.store.db.execute("update spawned_agents set spawned_by = 'arch' where agent_name = 'rev'")  # added later
        self.store.db.commit()
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        self.hand_in()
        self.assertEqual((self.argv, self.hub_rows(), self.reviewer()), ([], [], None))
        self.assertEqual(self.counts(), {"arch": 1, "rev": 1}, "T-37's notice, as it was")

    def test_accepting_the_task_retires_its_reviewer_and_not_the_standing_one(self):
        self.file(self.tok("arch"), owner="impl")
        self.hand_in()
        mine = self.reviewer()
        self.go_live(mine["agent_name"], mine["session_id"], 9001)        # running: it is ended, not just marked
        killed = []

        def kill(pid, created):
            killed.append(pid)
            self.machine.procs.pop(pid, None)
            return True, "SUCCESS", {}
        with mock.patch.object(self.fleet, "kill", side_effect=kill):
            status, _, res = self.log(self.tok("arch"), ref="T-1", status="accepted")
        self.assertEqual(status, 200, res)
        self.assertEqual(killed, [9001], "the fresh reviewer's process, not the standing reviewer's")
        self.assertIsNotNone(self.store.spawn_by_session(mine["session_id"])["retired_ts"])
        self.assertIsNone(self.store.spawn_by_session("sess-rev")["retired_ts"])
        self.assertEqual(self.store.squad("sq-1")["spawn_used"], 0, "its slot comes back")
        retired = [e["body"] for e in self.task()["log"] if e["kind"] == "retired"]
        self.assertEqual(len(retired), 1)
        self.assertIn("rev-claude retired on T-1", retired[0])
        self.assertIn("was ended", retired[0])

    def test_two_tasks_handed_in_back_to_back_get_two_reviewers(self):
        self.file(self.tok("arch"), owner="impl")
        self.file(self.tok("arch"), owner="impl")
        self.hand_in("T-1")
        self.hand_in("T-2")
        self.assertEqual(len(self.argv), 2)
        names = (self.reviewer("T-1")["agent_name"], self.reviewer("T-2")["agent_name"])
        self.assertEqual(names, ("rev-claude", "rev-claude-2"))
        self.assertIn("## Your task (`T-2`)", self.argv[1][-1])

    def queue_starts(self):
        """_start_reviewer held back: each queued start is (real function, its arguments), run when the test says."""
        from almanac import door
        queued = []
        patcher = mock.patch.object(door, "_start_reviewer", side_effect=lambda *a: queued.append(a))
        return door._start_reviewer, queued, patcher

    def test_hand_ins_while_a_start_is_in_flight_tell_no_reviewer_and_start_none(self):       # F1a, F1c
        real, queued, patcher = self.queue_starts()
        heads = iter([dict(sha="a" * 40, branch="almanac/sq-1/impl", base="b" * 40),
                      dict(sha="c" * 40, branch="almanac/sq-1/impl", base="b" * 40),
                      dict(sha="d" * 40, branch="almanac/sq-1/impl", base="b" * 40)])
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        with patcher, mock.patch("almanac.door.handin_head", side_effect=lambda *a: (next(heads), "")):
            self.hand_in(body="v1")
            self.hand_in(body="v2")                        # a retry or quick fix round, in review again
            self.log(self.tok("impl"), ref="T-1", status="in progress", body="one more")
            self.hand_in(body="v3")                        # in review from another status, start still in flight
        self.assertEqual(len(queued), 1, "one start queued for the task")
        self.assertNotIn("rev", self.counts(), "the standing reviewer is not told while a start is in flight")
        real(*queued[0])
        self.assertEqual((len(self.argv), self.counts().get("rev")), (1, None), "one reviewer, no double review")
        kickoff = self.argv[0][-1]
        self.assertIn("commit ddddddd", kickoff, "the kickoff names the latest hand-in's commit")
        self.assertNotIn("commit aaaaaaa", kickoff)
        self.assertEqual(self.service.review_starts, set(), "done: out of the in-flight set")

    def test_a_start_that_runs_late_checks_the_task_again(self):                          # L1
        real, queued, patcher = self.queue_starts()
        self.file(self.tok("arch"), owner="impl")
        with patcher:
            self.hand_in()
        self.log(self.tok("arch"), ref="T-1", status="accepted")    # accepted before its start ran
        real(*queued[0])
        self.assertEqual((len(self.argv), self.reviewer(), self.hub_rows()), (0, None, []))
        self.file(self.tok("arch"), owner="impl")                   # T-2: a reviewer already on record
        self.hand_in("T-2")
        real(*queued[0][:1], self.store.object_row(2), *queued[0][2:])
        self.assertEqual(len(self.argv), 1, "one task, one reviewer")

    def test_crossing_hand_ins_whose_start_falls_back_write_one_row_and_one_notice(self):     # F1b
        self.store.update_squad("sq-1", spawn_limit=0)
        real, queued, patcher = self.queue_starts()
        self.file(self.tok("arch"), owner="impl")
        self.clear()
        with patcher:
            self.hand_in(body="v1")
            self.hand_in(body="v2")
        self.assertEqual(len(queued), 1)
        real(*queued[0])
        self.assertEqual((len(self.hub_rows()), self.counts().get("rev")), (1, 1))

    def test_a_restart_between_the_hand_in_and_the_fix_round_still_finds_the_reviewer(self):
        self.file(self.tok("arch"), owner="impl")
        self.hand_in()
        mine = self.reviewer()
        self.service = Service(self.store, self.projects, 300, fleet=self.fleet, state=self.state)
        self.service.probe = runner_probe(self.root, claude=True, gemini=False)
        self.handler = make_handler(self.service, TOKEN)
        self.go_live(mine["agent_name"], mine["session_id"], 9001)
        self.clear()
        self.hand_in(body="fixed F1")
        self.assertEqual((len(self.argv), self.counts()), (1, {"arch": 1, "rev-claude": 1}))


class ImplementerBriefTests(ObjectRig):
    def test_an_unedited_brief_is_upgraded_and_an_edited_one_kept(self):            # F2
        from almanac import store as st
        self.store.db.execute("update roles set system_prompt = ? where id = 'implementer-claude'", (st.PREV_IMPLEMENTER,))
        self.store.db.execute("update roles set system_prompt = 'mine' where id = 'implementer-gemini'")
        self.store.db.commit()
        again = Store(self.state / "almanac.db")
        self.addCleanup(again.close)
        self.assertEqual(again.role("implementer-claude")["system_prompt"], st.IMPLEMENTER)
        self.assertEqual(again.role("implementer-gemini")["system_prompt"], "mine")
        self.assertNotIn("mesh_send", st.IMPLEMENTER)
