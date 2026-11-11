"""Stage 1, tasks as objects: the `objects` / `object_log` store, the four `/api/mesh/` object routes, `re=` on
mesh_send, `objects_rev`, the squad rules, caps, and the caller resolved by session (two squads sharing a handle)."""
import json
import urllib.parse

from support import TOKEN
from test_fleet import CLI
from test_server_fixes import MeshRig

from almanac import uds, web


class ObjectRig(MeshRig):
    def setUp(self):
        super().setUp()
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
                                              "status_from", "status_to", "owner_to", "message_id"})

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
