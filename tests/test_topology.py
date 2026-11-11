"""Slice 4: the MESH topology (who wrote to whom, open flags, ended agents kept, role columns) and the swimlane's
messages and marks on /api/worktime."""
import time

from support import TOKEN, Fixture
from test_server_fixes import MeshRig

from almanac import door, web
from almanac.store import Store


class GraphTests(MeshRig):
    """MeshRig's live peers arch and impl are in sq-1; `gone` was spawned in sq-1 and has exited."""

    def setUp(self):
        super().setUp()
        self.store.create_squad("sq-1", "alpha", ".", "brief")
        for name, role in (("arch", "architect-claude"), ("impl", "implementer-claude"), ("gone", "reviewer-gemini")):
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id="sq-1", role_id=role,
                                    provider="gemini" if name == "gone" else "claude", model=None, effort=None, cwd=".",
                                    worktree_path="/wt/impl" if name == "impl" else None, pid=1)
        say = lambda f, t, n=None, squad="sq-1": self.store.record_message(from_name=f, to_name=t, body=f"{f}>{t}",  # noqa: E731
                                                                           needs=n, squad_id=squad)
        for _ in range(3):
            say("arch", "impl")
        say("impl", "arch", "reply")
        say("arch", "impl")                                 # answers impl's reply flag
        say("arch", "@squad")
        say("impl", "operator", "blocking")
        say("impl", "almanac-hub")                          # the hub's other name is the operator too
        say("gone", "#review")
        say("arch", "impl", squad="sq-2")                   # another squad's traffic stays out

    def test_edges_group_by_pair_and_carry_open_flags(self):
        edges = {(e["from"], e["to"]): e for e in self.store.message_graph("sq-1")}
        self.assertEqual(set(edges), {("arch", "impl"), ("impl", "arch"), ("arch", "@squad"), ("impl", "operator"),
                                      ("gone", "#review")})
        self.assertEqual(edges[("arch", "impl")]["count"], 4)
        self.assertEqual(edges[("impl", "arch")]["open"], {"reply": 0, "blocking": 0}, "answered by arch")
        self.assertEqual((edges[("impl", "operator")]["count"], edges[("impl", "operator")]["open"]),
                         (2, {"reply": 0, "blocking": 1}))
        self.assertGreater(edges[("arch", "impl")]["last_ts"], 0)

    def test_nodes_keep_ended_agents_and_name_their_column(self):
        status, _, doc = self.request(self.handler, "/api/mesh/graph?squad=sq-1")
        self.assertEqual(status, 200)
        nodes = {n["id"]: n for n in doc["nodes"]}
        self.assertEqual([n["id"] for n in doc["nodes"] if n["kind"] == "agent"][:3], ["arch", "impl", "gone"],
                         "spawn order")
        self.assertEqual((nodes["arch"]["role_class"], nodes["arch"]["live"]), ("architect", True))
        self.assertEqual((nodes["impl"]["role_class"], nodes["impl"]["worktree"]), ("implementer", "/wt/impl"))
        self.assertEqual((nodes["gone"]["role_class"], nodes["gone"]["live"], nodes["gone"]["status"], nodes["gone"]["provider"]),
                         ("reviewer", False, "ended", "gemini"))
        self.assertEqual(nodes["operator"]["kind"], "operator")
        self.assertEqual({n["id"] for n in doc["nodes"] if n["kind"] == "channel"}, {"@squad", "#review"})
        self.assertNotIn("other", nodes, "a peer of another squad is not drawn")
        self.assertEqual(self.request(self.handler, "/api/mesh/graph?squad=nope")[0], 404)
        agent = web.agent_token(TOKEN, "arch")
        self.assertEqual(self.request(self.handler, "/api/mesh/graph?squad=sq-1", token=agent)[0], 200, "agents may read it")

    def test_no_squad_draws_the_live_peers_without_edges(self):
        _, _, doc = self.request(self.handler, "/api/mesh/graph")
        self.assertEqual(doc["edges"], [])
        self.assertTrue({"arch", "impl", "other", "outsider", "operator"} <= {n["id"] for n in doc["nodes"]})

    def test_role_class(self):
        self.assertEqual([door.role_class(r) for r in ("architect-gemini", "implementer-claude", "reviewer-claude", "scribe")],
                         ["architect", "implementer", "reviewer", "other"])
        self.assertEqual(door.role_class("lead", "Lead Architect"), "architect", "the title counts too")


class SwimlaneTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        self.t0 = time.time() - 600
        for sid in ("s1", "s2"):
            self.store.db.execute("insert into turns values (?, ?, ?, 'prompt')", (sid, self.t0, self.t0 + 500))
        calls = [("c1", "s1", 10, "Edit", None, 0), ("c2", "s1", 20, "Bash", "edit", 0), ("c3", "s1", 30, "Bash", "tests", 1),
                 ("c4", "s2", 40, "Read", None, 0), ("c5", "s2", 50, "write_to_file", None, 0), ("c6", "s2", 900, "Edit", None, 0)]
        for cid, sid, dt, tool, family, err in calls:
            self.store.db.execute("insert into calls (natural, tool_use_id, session, ts, tool, family, is_error) values (?, ?, ?, ?, ?, ?, ?)",
                                  (cid, cid, sid, self.t0 + dt, tool, family, err))
        self.store.db.commit()
        for i, (f, t, n) in enumerate((("a", "b", "blocking"), ("b", "a", None), ("a", "@squad", None), ("b", "almanac-hub", "reply"))):
            self.store.record_message(from_name=f, to_name=t, body=f"message {i} " + "x" * 300, needs=n, squad_id="sq",
                                      ts=self.t0 + 100 + i, from_session="s1" if f == "a" else "s2")

    def test_marks_are_errors_and_edits_in_range(self):
        marks = self.store.timeline_marks(["s1", "s2"], start=self.t0, end=self.t0 + 600)
        self.assertEqual([(round(m["ts"] - self.t0), m["session"], m["kind"]) for m in marks],
                         [(10, "s1", "edit"), (20, "s1", "edit"), (30, "s1", "error"), (50, "s2", "edit")])
        newest = self.store.timeline_marks(["s1", "s2"], start=self.t0, end=self.t0 + 600, limit=2)
        self.assertEqual([round(m["ts"] - self.t0) for m in newest], [30, 50], "the cap keeps the newest, oldest first")

    def test_messages_carry_flags_and_a_short_preview(self):
        msgs = self.store.timeline_messages(squad_id="sq", start=self.t0, end=self.t0 + 600)
        self.assertEqual([(m["from"], m["to"], m["needs"]) for m in msgs],
                         [("a", "b", "blocking"), ("b", "a", None), ("a", "@squad", None), ("b", "operator", "reply")])
        self.assertIsNotNone(msgs[0]["answered_ts"], "b's reply answered a's blocking question")
        self.assertEqual(len(msgs[0]["preview"]), 120)
        self.assertEqual(len(self.store.timeline_messages(squad_id="sq", start=self.t0, end=self.t0 + 600, limit=2)), 2)
        by_session = self.store.timeline_messages(sessions=["s2"], start=self.t0, end=self.t0 + 600)
        self.assertEqual([m["from"] for m in by_session], ["b", "b"])

    def test_worktime_route_carries_both(self):
        from almanac.door import Service, make_handler
        from almanac.fleet import Fleet
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        service = Service(self.store, self.projects, 300, fleet=Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {}))
        _, _, doc = self.request(make_handler(service, TOKEN), "/api/worktime?sessions=s1,s2")
        self.assertEqual({m["kind"] for m in doc["marks"]}, {"edit", "error"})
        self.assertEqual(len(doc["messages"]), 4)
        self.assertEqual(set(doc["messages"][0]), {"id", "ts", "from", "to", "needs", "answered_ts", "channel", "preview"})
