"""Roadmap item 3: the operator's inbox, which roles may page the operator, and `needs` flags that stay open until
the recipient answers the sender directly."""
import sqlite3
import time

from support import TOKEN, Fixture, write_jsonl
from test_server_fixes import MeshRig, peer_line

from almanac import mcp_mesh, spawner, uds, web
from almanac.store import Store


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class MigrationV5Tests(Fixture):
    def test_a_v4_database_keeps_its_rows_and_gains_flags_and_the_paging_role_bit(self):
        path = self.root / "v4.db"
        Store(path).close()
        db = sqlite3.connect(path)
        db.execute("drop index messages_open")
        for column in ("needs", "answered_ts", "answered_by", "read_ts"):
            db.execute(f"alter table messages drop column {column}")
        db.execute("alter table roles drop column may_page_operator")
        db.execute("insert into messages (msg_id, ts, from_name, to_name, body) values ('old', 1, 'impl', 'operator', 'hi')")
        db.execute("update meta set value = '4' where key = 'schema'")
        db.commit()
        db.close()

        s = Store(path)
        self.addCleanup(s.close)
        cols = {r[1] for r in s.db.execute("pragma table_info(messages)")}
        self.assertTrue({"needs", "answered_ts", "answered_by", "read_ts"} <= cols)
        [old] = s.messages()
        self.assertEqual((old["msg_id"], old["body"], old["needs"], old["read_ts"]), ("old", "hi", None, None))
        paging = {r["id"] for r in s.roles() if r["may_page_operator"]}
        self.assertEqual(paging, {"architect-claude", "architect-gemini", "orchestrator-claude"})   # v10 adds the orchestrator
        self.assertGreaterEqual(int(s.db.execute("select value from meta where key = 'schema'").fetchone()[0]), 5)

    def test_upsert_role_keeps_the_paging_bit_unless_told(self):
        s = Store(self.root / "r.db")
        self.addCleanup(s.close)
        base = dict(id="scribe", provider="claude", system_prompt="write")
        self.assertFalse(s.upsert_role(base)["may_page_operator"], "a new role may not page by default")
        self.assertTrue(s.upsert_role(dict(base, may_page_operator=True))["may_page_operator"])
        self.assertTrue(s.upsert_role(dict(base, title="Scribe"))["may_page_operator"], "absent keeps it")
        self.assertFalse(s.upsert_role(dict(base, may_page_operator=False))["may_page_operator"])

    def test_needs_is_validated(self):
        s = Store(self.root / "n.db")
        self.addCleanup(s.close)
        with self.assertRaises(ValueError):
            s.record_message(from_name="a", to_name="b", body="x", needs="urgent")
        self.assertEqual(s.record_message(from_name="a", to_name="b", body="x", needs="")["needs"], None)


class AnsweredRuleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.s = Store(self.root / "a.db")
        self.addCleanup(self.s.close)

    def say(self, frm, to, needs=None, squad="sq-1", **kw):
        return self.s.record_message(from_name=frm, to_name=to, body=f"{frm}->{to}", needs=needs, squad_id=squad, **kw)

    def state(self, msg):
        row = self.s.message(msg["id"])
        return row["answered_by"]

    def test_a_direct_reply_closes_the_question(self):
        q = self.say("impl", "arch", "reply")
        a = self.say("arch", "impl")
        self.assertEqual(self.state(q), a["id"])
        self.assertIsNotNone(self.s.message(q["id"])["answered_ts"])

    def test_a_broadcast_closes_nothing(self):
        q = self.say("impl", "arch", "blocking")
        self.say("arch", "@squad", transport="uds-broadcast")
        self.say("arch", "#impl", transport="uds-broadcast")
        self.assertIsNone(self.state(q))

    def test_a_reply_from_another_squad_or_a_third_party_does_not_close_it(self):
        q = self.say("impl", "arch", "reply")
        self.say("arch", "impl", squad="sq-2")
        self.say("rev", "impl")
        self.say("impl", "arch")             # the asker talking again is not an answer
        self.assertIsNone(self.state(q))
        self.assertEqual([m["id"] for m in self.s.open_needs()], [q["id"]])

    def test_operator_and_agent_answer_each_other_both_ways(self):
        page = self.say("impl", "operator", "blocking")
        answer = self.say("operator", "impl")
        self.assertEqual(self.state(page), answer["id"])
        ask = self.say("operator", "impl", "reply")
        back = self.say("impl", "almanac-hub")
        self.assertEqual(self.state(ask), back["id"])

    def test_a_flagged_broadcast_is_closed_by_any_direct_reply_in_its_squad(self):
        q = self.say("arch", "@squad", "reply", transport="uds-broadcast")
        self.say("other", "arch", squad="sq-2")
        self.assertIsNone(self.state(q))
        a = self.say("impl", "arch")
        self.assertEqual(self.state(q), a["id"])

    def test_an_older_message_ingested_late_answers_nothing(self):
        q = self.say("impl", "arch", "reply")
        self.say("arch", "impl", ts=q["ts"] - 60)
        self.assertIsNone(self.state(q))

    def test_resolve_by_hand(self):
        q = self.say("impl", "operator", "reply")
        self.assertTrue(self.s.resolve_message(q["id"]))
        self.assertEqual(self.state(q), 0)
        self.assertFalse(self.s.resolve_message(q["id"]), "already closed")
        self.assertFalse(self.s.resolve_message(self.say("a", "b")["id"]), "never flagged")


class MarkerTests(MeshRig):
    def setUp(self):
        super().setUp()
        for name, _, squad, _ in self.PEERS:
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id=squad, role_id=None,
                                    provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def test_the_marker_round_trips_through_the_wrapper(self):
        for channel in (None, "#impl"):
            wire = uds.format_cross_session(uds.needs_marker("blocking", "arch") + "ship it?", from_pipe="p",
                                            from_name="arch", channel=channel)
            parsed = uds.parse_cross_session(wire)
            self.assertEqual((parsed["needs"], parsed["body"], parsed["channel"]), ("blocking", "ship it?", channel))
        self.assertEqual(uds.needs_marker(None, "arch"), "")
        self.assertIsNone(uds.parse_cross_session(uds.format_cross_session("plain", from_pipe="p", from_name="a"))["needs"])

    def test_a_flagged_message_arrives_marked_and_is_journaled_once(self):
        status, _, res = self.send(to="impl", text="Which port?", needs="reply")
        self.assertEqual(status, 200)
        self.assertEqual(res["message"]["body"], "Which port?", "the journal keeps the sender's words")
        self.assertEqual(res["message"]["needs"], "reply")
        got = self.inbox["impl"][0]
        self.assertTrue(got["body"].startswith("[needs: reply] operator wants an answer"), got["body"])
        self.assertEqual(got["needs"], "reply")
        # a Gemini host re-wraps the received body into the model's prompt: the flag line survives that
        rewrapped = uds.format_cross_session(got["body"], from_pipe="p", from_name=got["from_name"])
        self.assertIn("[needs: reply] operator wants an answer", rewrapped)
        self.assertEqual(uds.parse_cross_session(rewrapped)["needs"], "reply")
        # the recipient's transcript holds the delivered text; reading it back must not add a second row
        wire = uds.format_cross_session(uds.needs_marker("reply", "operator") + "Which port?", from_pipe="p",
                                        from_name="operator")
        write_jsonl(self.projects / "p" / "sess-impl.jsonl",
                    [{"type": "user", "sessionId": "sess-impl", "timestamp": iso(time.time()), "cwd": "c",
                      "message": {"content": wire}}])
        self.store.ingest(self.projects)
        rows = [m for m in self.store.messages() if m["body"] == "Which port?"]
        self.assertEqual([(m["transport"], m["needs"]) for m in rows], [("uds", "reply")])

    def test_a_native_flagged_message_is_journaled_with_its_flag(self):
        body = uds.needs_marker("blocking", "arch") + "Need the schema now"
        write_jsonl(self.projects / "p" / "sess-impl.jsonl", [peer_line("sess-impl", iso(time.time()), "arch", body)])
        self.store.ingest(self.projects)
        [row] = [m for m in self.store.messages() if m["transport"] == "uds-transcript"]
        self.assertEqual((row["from_name"], row["to_name"], row["needs"], row["body"]),
                         ("arch", "impl", "blocking", "Need the schema now"))
        # and impl's later direct reply, read from arch's transcript, closes it
        write_jsonl(self.projects / "p" / "sess-arch.jsonl", [peer_line("sess-arch", iso(time.time() + 5), "impl", "done")])
        self.store.ingest(self.projects)
        self.assertIsNotNone(self.store.message(row["id"])["answered_ts"])

    def test_bad_needs_is_a_400(self):
        self.assertEqual(self.send(to="impl", text="x", needs="asap")[0], 400)

    def test_the_feed_shows_the_flag(self):
        from almanac.fleet import _peer_in_event
        ev = _peer_in_event("t", dict(from_name="arch", needs="blocking", body="go?"))
        self.assertEqual((ev["needs"], ev["text"]), ("blocking", "arch: [blocking] go?"))


class PagingTests(MeshRig):
    def setUp(self):
        super().setUp()
        roles = dict(arch="architect-claude", impl="implementer-claude", other="implementer-claude")
        for name, _, squad, _ in self.PEERS:
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id=squad, role_id=roles.get(name),
                                    provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def page(self, who, **body):
        return self.send(web.agent_token(TOKEN, who), to=body.pop("to", "operator"), text=body.pop("text", "help"), **body)

    def test_an_architect_pages_and_an_implementer_is_pointed_at_a_relay(self):
        status, _, res = self.page("arch", needs="blocking")
        self.assertEqual((status, res["message"]["to_name"], res["message"]["needs"]), (200, "operator", "blocking"))
        status, _, res = self.page("impl")
        self.assertEqual(status, 403)
        self.assertFalse(res["ok"])
        self.assertIn("ask arch to relay it", res["error"])
        status, _, res = self.page("other", to="almanac-hub")
        self.assertEqual(status, 403)
        self.assertIn("no live squadmate", res["error"], "sq-2 has no one who may page")

    def test_answering_the_operator_is_not_paging_one_reply_per_message(self):
        self.assertEqual(self.send(to="impl", text="status?")[0], 200)
        self.assertEqual(self.page("impl", text="green")[0], 200)
        self.assertEqual(self.page("impl", text="and another thing")[0], 403)
        self.assertEqual(self.send(to="impl", text="thanks")[0], 200)
        self.assertEqual(self.page("impl", text="np")[0], 200)

    def test_role_less_and_role_gone_agents_may_page(self):
        self.assertEqual(self.page("outsider")[0], 200)
        self.store.record_spawn(agent_name="ghost", session_id="sess-ghost", squad_id="sq-1", role_id="retired",
                                provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)
        self.assertEqual(self.page("ghost")[0], 200)

    def test_the_role_bit_decides(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), may_page_operator=True))
        self.assertEqual(self.page("impl")[0], 200)

    def test_the_operator_itself_is_never_refused(self):
        self.assertEqual(self.send(to="operator", text="note to self")[0], 200)


class InboxTests(MeshRig):
    def setUp(self):
        super().setUp()
        self.ids = [self.store.record_message(from_name=f, to_name=t, body=b, needs=n, squad_id=sq)["id"]
                    for f, t, b, n, sq in (("arch", "operator", "fyi", None, "sq-1"),
                                           ("arch", "operator", "pick a db", "blocking", "sq-1"),
                                           ("operator", "impl", "go", None, "sq-1"),
                                           ("impl", "almanac-hub", "ok?", "reply", "sq-1"),
                                           ("arch", "impl", "agent chatter", None, "sq-1"),
                                           ("other", "operator", "hello", None, "sq-2"))]

    def operator_inbox(self, query=""):
        return self.request(self.handler, f"/api/operator/inbox{query}")

    def test_counts_messages_and_read_marks(self):
        status, _, doc = self.operator_inbox()
        self.assertEqual(status, 200)
        self.assertEqual([m["body"] for m in doc["messages"]], ["fyi", "pick a db", "go", "ok?", "hello"])
        self.assertEqual((doc["unread"], doc["open"]), (4, dict(reply=1, blocking=1)))
        self.assertIsNotNone(doc["oldest_blocking_ts"])
        _, _, counts = self.operator_inbox("?limit=0")
        self.assertEqual((counts["messages"], counts["unread"]), ([], 4))
        _, _, sq2 = self.operator_inbox("?squad=sq-2")
        self.assertEqual(([m["body"] for m in sq2["messages"]], sq2["unread"]), (["hello"], 1))
        status, _, res = self.request(self.handler, "/api/operator/read", method="POST", body={"ids": [self.ids[0]]})
        self.assertEqual((status, res["marked"], res["unread"]), (200, 1, 3))
        _, _, res = self.request(self.handler, "/api/operator/read", method="POST", body={"upto": self.ids[3]})
        self.assertEqual((res["marked"], res["unread"]), (2, 1))
        self.assertEqual(self.request(self.handler, "/api/operator/read", method="POST", body={})[0], 400)

    def test_resolve_by_hand(self):
        status, _, res = self.request(self.handler, "/api/operator/resolve", method="POST", body={"id": self.ids[1]})
        self.assertEqual((status, res["open"]["blocking"], res["message"]["answered_by"]), (200, 0, 0))
        self.assertEqual(self.request(self.handler, "/api/operator/resolve", method="POST", body={"id": self.ids[1]})[0], 404)
        self.assertEqual(self.request(self.handler, "/api/operator/resolve", method="POST", body={"id": "x"})[0], 400)

    def test_an_agent_token_cannot_open_the_inbox(self):
        agent = web.agent_token(TOKEN, "arch")
        self.assertEqual(self.request(self.handler, "/api/operator/inbox", token=agent)[0], 403)
        self.assertEqual(self.request(self.handler, "/api/operator/read", token=agent, method="POST", body={"upto": 99})[0], 403)
        self.assertEqual(self.request(self.handler, "/api/operator/resolve", token=agent, method="POST",
                                      body={"id": self.ids[1]})[0], 403)

    def test_the_mesh_lists_open_flags(self):
        _, _, doc = self.request(self.handler, "/api/mesh?squad=sq-1")
        self.assertEqual([m["body"] for m in doc["open"]], ["pick a db", "ok?"])
        calls = []
        client = mcp_mesh.MeshClient(url="u", token="t", self_name="arch",
                                     api_fn=lambda path, method="GET", body=None, timeout=30.0: calls.append(body) or doc)
        self.assertEqual(len(client.call_tool("mesh_roster")["open"]), 2)
        client.call_tool("mesh_send", {"to": "impl", "message": "hi", "needs": "blocking"})
        self.assertEqual(calls[-1]["needs"], "blocking")


class CharterTests(Fixture):
    def charter(self, flag):
        return spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="R", role_prompt="p",
                                          almanac_url="http://x", may_page_operator=flag)

    def test_the_charter_tells_each_role_how_it_reaches_the_operator(self):
        yes, no = self.charter(True), self.charter(False)
        for text in (yes, no):
            self.assertIn('needs="blocking"', text)
            self.assertIn("replies to the sender directly", text)
        self.assertIn('mesh_send(to="operator"', yes)
        self.assertNotIn("does not page", yes)
        self.assertIn("your role does not page the operator", no)
        self.assertNotIn('mesh_send(to="operator"', no)
