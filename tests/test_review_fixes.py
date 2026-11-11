"""FIX-7: the reviewer's findings, each from its repro script: a squad-less broadcast is not a second, direct row;
a typed needs marker is the flag; reserved handles; a squad's used handles; whom a newcomer reports to; a Cookie
header with someone else's odd cookie."""
import time

from support import TOKEN, Fixture, write_jsonl
from test_elastic import TASK, ElasticRig
from test_server_fixes import MeshRig, peer_line

from almanac import uds, web
from almanac.store import Store


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SquadlessBroadcastTests(Fixture):
    """r2_close.py: an operator @all (or a #channel with no squad) read back from a squad member's transcript."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq1", role_id=None, provider="claude",
                                model=None, effort=None, cwd=".", worktree_path=None, pid=1)
        self.now = time.time()

    def read_back(self, body, ts):
        write_jsonl(self.projects / "p" / "sess-impl.jsonl", [peer_line("sess-impl", iso(ts), "operator", body)], mode="a")
        self.store.ingest(self.projects)

    def test_it_closes_no_flag_and_grants_no_reply_pass(self):
        flag = self.store.record_message(from_name="impl", to_name="operator", squad_id="sq1", body="may I delete prod?",
                                         transport="hub", needs="blocking", ts=self.now - 5)
        self.store.record_message(from_name="operator", to_name="@all", squad_id=None, body="lunch at noon",
                                  transport="uds-broadcast", ts=self.now)
        self.read_back("lunch at noon", self.now + 1)
        self.assertEqual(len(self.store.messages()), 2, "the copy is the broadcast, not a new direct row")
        self.assertIsNone(self.store.message(flag["id"])["answered_ts"], "the blocking flag stays open")
        self.assertEqual(self.store.last_operator_exchange("impl", "sq1")["id"], flag["id"], "no reply pass")

    def test_a_squadless_channel_post_too(self):
        self.store.record_message(from_name="operator", to_name="#review", channel="#review", squad_id=None,
                                  body="look at X", transport="uds-broadcast", ts=self.now)
        self.read_back("look at X", self.now + 1)
        self.assertEqual([m["transport"] for m in self.store.messages()], ["uds-broadcast"])


class TypedMarkerTests(MeshRig):
    """r1_dedup.py case 1: a sender types the marker line into the body and leaves needs unset."""

    def setUp(self):
        super().setUp()
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None, provider="claude",
                                model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def test_the_typed_marker_is_the_flag_and_the_transcript_adds_nothing(self):
        typed = "[needs: blocking] arch cannot continue until you answer: reply to them directly with mesh_send.\nhello"
        status, _, res = self.send(to="impl", text=typed)
        self.assertEqual((status, res["message"]["needs"], res["message"]["body"]), (200, "blocking", "hello"))
        delivered = self.inbox["impl"][0]["body"]
        self.assertEqual(delivered.count("[needs:"), 1, "one marker line, not two")
        write_jsonl(self.projects / "p" / "sess-impl.jsonl", [{"type": "user", "sessionId": "sess-impl", "timestamp": iso(time.time()),
                    "cwd": "c", "message": {"content": uds.format_cross_session(delivered, from_pipe="p", from_name="operator")}}])
        self.store.ingest(self.projects)
        self.assertEqual([(m["needs"], m["body"]) for m in self.store.messages()], [("blocking", "hello")])

    def test_an_explicit_needs_wins_and_a_bare_marker_is_empty(self):
        _, _, res = self.send(to="impl", text="[needs: blocking] soon please", needs="reply")
        self.assertEqual((res["message"]["needs"], res["message"]["body"]), ("reply", "soon please"))
        self.assertEqual(self.send(to="impl", text="[needs: reply]")[0], 400, "a tag and nothing else")

    def test_api_send_too(self):
        proc = next(p for p in self.service.fleet_now()["processes"] if p["pid"] == 500)
        status, _, _ = self.request(self.handler, "/api/send", method="POST",
                                    body={"pid": 500, "created": proc["created"], "text": "[needs: reply] ready?"})
        self.assertEqual(status, 200)
        [row] = self.store.messages()
        self.assertEqual((row["needs"], row["body"]), ("reply", "ready?"))


class ReservedNameTests(ElasticRig):
    """n1.py: a member called operator, almanac-hub or hub would speak as the human."""

    def test_every_route_refuses_a_reserved_handle(self):
        for name in ("operator", "Almanac-Hub", "HUB", "all", "squad", "peer"):
            status, _, res = self.request(self.handler, "/api/spawn", method="POST",
                                          body={"cwd": str(self.ws), "role_id": "implementer-claude", "agent_name": name})
            self.assertEqual(status, 400, name)
            self.assertIn("reserved", res["error"])
        self.assertEqual(self.request(self.handler, "/api/squad/member", method="POST",
                                      body={"squad_id": "sq-1", "role_id": "implementer-claude", "task": TASK, "name": "operator"})[0], 400)
        self.assertEqual(self.spawn(name="almanac-hub")[0], 400)
        status, _, res = self.request(self.handler, "/api/squads", method="POST", body={
            "name": "x", "cwd": str(self.ws), "members": [{"role_id": "implementer-claude", "agent_name": "hub", "workspace_mode": "shared"}]})
        self.assertEqual(res["failed"], 1)
        self.assertIn("reserved", res["squad"]["agents"][0]["error"])
        self.assertEqual(self.argv, [], "nothing was launched")

    def test_a_reserved_bearer_is_refused_at_the_door(self):
        for name in ("operator", "almanac-hub"):
            self.assertEqual(self.request(self.handler, "/api/mesh", token=web.agent_token(TOKEN, name))[0], 403)


class HandleReuseTests(ElasticRig):
    def test_an_explicit_name_cannot_take_a_handle_the_squad_has_had(self):
        self.assertEqual(self.spawn(name="impl-claude")[0], 200)
        status, _, res = self.spawn(name="impl-claude")              # it is not live (Popen is mocked), yet it was a member
        self.assertEqual(status, 400)
        self.assertIn("already a member of squad sq-1", res["error"])
        self.assertEqual(self.spawn(name="arch")[0], 400, "an old member's handle, live or not")


class ReportToTests(ElasticRig):
    def kickoff(self, **body):
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST",
                                      body={"squad_id": "sq-1", "task": TASK, **body})
        self.assertEqual(status, 200, res)
        return self.argv[-1][-1]

    def test_a_member_that_may_not_page_reports_to_a_squadmate_who_may(self):
        prompt = self.kickoff(role_id="implementer-claude")
        self.assertIn("Report to `arch` over `mesh_send`", prompt)
        self.assertNotIn('mesh_send(to="operator")', prompt)

    def test_a_member_that_may_page_reports_to_the_operator(self):
        self.assertIn('Report to the operator (`mesh_send(to="operator")`)', self.kickoff(role_id="architect-claude", name="arch-2"))

    def test_with_nobody_who_may_page_it_posts_to_the_squad(self):
        self.store.upsert_role(dict(self.store.role("architect-claude"), may_page_operator=False))
        self.assertIn("Report to `#squad`", self.kickoff(role_id="implementer-claude"))


class CookieHeaderTests(Fixture):
    """c1.py: another local app's cookie on 127.0.0.1 must not sign the operator out."""

    def test_the_session_survives_odd_neighbours(self):
        token, name = "x" * 40, web.cookie_name(8812)
        good = web.new_session(token)
        for raw in (f"{name}={good}", f"other=1; {name}={good}", f"bad[key]=1; {name}={good}",
                    f'a="unterminated; {name}={good}', f"{name}={good}; 2fa=on", f'{name}="{good}"'):
            self.assertTrue(web.valid_session(raw, token, name=name), raw[:24])
        for raw in (f"{web.COOKIE}={good}", f"x{name}={good}", f"{name}=", "", None, f"{name}={good[:-2]}00"):
            self.assertFalse(web.valid_session(raw, token, name=name), raw)
