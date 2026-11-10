"""The mesh's server side: agent tokens, who may broadcast where, the contact stream's dedupe, the v4 migration,
spawn records per session, feed text kept as written, UTF-8 over MCP stdin, the faster read while agents run."""
import io
import json
import sqlite3
import time
from unittest import mock

from support import TOKEN, Fixture, write_jsonl
from test_fleet import CLI, FakeMachine

from almanac import mcp_mesh, spawner, store as store_mod, uds, web
from almanac.door import Service, make_handler
from almanac.fleet import Fleet, prose, tail_transcript
from almanac.store import Store


class MeshRig(Fixture):
    """Four live peers: arch (sq-1, #squad #architecture), impl (sq-1, #squad #impl), other (sq-2, #squad),
    and outsider: an unrelated session in no squad and no channel (the unifiedserver-79 case)."""

    PEERS = (("arch", 100, "sq-1", ["#squad", "#architecture"]), ("impl", 500, "sq-1", ["#squad", "#impl"]),
             ("other", 700, "sq-2", ["#squad"]), ("outsider", 200, None, []))

    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.store = Store(self.state / "almanac.db")
        self.addCleanup(self.store.close)
        sessions = self.root / "claude" / "sessions"
        sessions.mkdir(parents=True)
        self.machine = FakeMachine()
        self.inbox = {}
        for name, pid, squad, channels in self.PEERS:
            self.inbox[name] = []
            peer = uds.PeerServer(sessions, name=name, pid=pid, session_id=f"sess-{name}", cwd=str(self.root),
                                  provider="claude", squad_id=squad, channels=channels,
                                  on_message=self.inbox[name].append, in_memory=True).start()
            self.addCleanup(peer.close)
            self.machine.procs[pid] = dict(ppid=1, exe="claude.exe", path=CLI, cmd=f'"{CLI}"',
                                           created=int(peer.proc_start), cpu=0, mem=1 << 20)
        self.fleet = Fleet(self.root / "claude", table=self.machine.table, facts=self.machine.facts, lines=self.machine.lines)
        self.service = Service(self.store, self.projects, 300, fleet=self.fleet, state=self.state)
        self.handler = make_handler(self.service, TOKEN)

    def heard(self):
        return {name for name, got in self.inbox.items() if got}

    def send(self, token=TOKEN, **body):
        return self.request(self.handler, "/api/mesh/send", token=token, method="POST", body=body)


class AgentTokenTests(MeshRig):
    def test_agent_token_opens_the_mesh_and_nothing_else(self):
        agent = web.agent_token(TOKEN, "impl")
        self.assertEqual(self.request(self.handler, "/api/mesh", token=agent)[0], 200)
        self.assertEqual(self.request(self.handler, "/api/fleet", token=agent)[0], 403)
        self.assertEqual(self.request(self.handler, "/api/kill", token=agent, method="POST", body={"pid": 1})[0], 403)
        self.assertEqual(self.request(self.handler, "/api/spawn", token=agent, method="POST", body={})[0], 403)
        self.assertEqual(self.request(self.handler, "/api/%6Bill", token=agent, method="POST", body={})[0], 403)
        self.assertEqual(self.request(self.handler, "/api/mesh", token="agent.impl." + "0" * 64)[0], 401)
        self.assertEqual(self.request(self.handler, "/api/mesh", token=agent.replace("impl", "arch", 1))[0], 401)
        self.assertEqual(self.request(self.handler, "/api/fleet")[0], 200, "the operator's token is unchanged")

    def test_keep_alive_does_not_carry_an_agent_into_the_next_request(self):
        seen = []

        class Spy(self.handler):
            def _dispatch(self):
                seen.append(self.agent)
                return super()._dispatch()

        agent = web.agent_token(TOKEN, "impl")
        wire = (f"GET /api/mesh HTTP/1.1\r\nHost: 127.0.0.1:8812\r\nAuthorization: Bearer {agent}\r\n\r\n"
                "GET /ident HTTP/1.1\r\nHost: 127.0.0.1:8812\r\nConnection: close\r\n\r\n").encode()
        from support import MemorySocket
        from types import SimpleNamespace
        Spy(MemorySocket(wire), ("127.0.0.1", 9), SimpleNamespace(server_port=8812, server_name="localhost"))
        self.assertEqual(seen, ["impl", None])

    def test_an_agent_speaks_only_as_itself_and_never_to_all(self):
        agent = web.agent_token(TOKEN, "impl")
        status, _, res = self.send(agent, to="arch", text="hi", from_name="operator")
        self.assertEqual((status, res["message"]["from_name"]), (200, "impl"))
        self.assertEqual(self.inbox["arch"][0]["from_name"], "impl")
        status, _, res = self.send(agent, to="@all", text="everyone")
        self.assertEqual(status, 400)
        self.assertEqual(self.heard(), {"arch"})

    def test_an_agent_cannot_name_a_foreign_squad(self):
        status, _, res = self.send(web.agent_token(TOKEN, "impl"), to="@squad", text="x", squad_id="sq-2")
        self.assertEqual((status, res["delivered"]), (200, ["arch"]))
        self.assertEqual(self.heard(), {"arch"})


class BroadcastScopeTests(MeshRig):
    def test_operator_channel_post_reaches_subscribers_only(self):
        _, _, res = self.send(to="#squad", text="x")
        self.assertEqual(set(res["delivered"]), {"arch", "impl", "other"})
        self.assertNotIn("outsider", self.heard())

    def test_squad_post_stays_in_the_squad(self):
        _, _, res = self.send(web.agent_token(TOKEN, "arch"), to="@squad", text="x")
        self.assertEqual(res["delivered"], ["impl"])
        _, _, res = self.send(web.agent_token(TOKEN, "arch"), to="#squad", text="y")
        self.assertEqual(res["delivered"], ["impl"])
        self.assertEqual(self.heard(), {"impl"})

    def test_operator_squad_post_needs_exactly_one_live_squad(self):
        status, _, res = self.send(to="@squad", text="x")
        self.assertEqual(status, 400)
        del self.machine.procs[700]                    # sq-2 ends: one live squad left
        self.fleet.last["at"] = 0
        status, _, res = self.send(to="@squad", text="x")
        self.assertEqual((status, set(res["delivered"])), (200, {"arch", "impl"}))
        self.assertNotIn("outsider", self.heard())

    def test_a_sender_that_is_not_live_is_placed_by_its_spawn_record(self):
        self.store.record_spawn(agent_name="ghost", session_id="sess-ghost", squad_id="sq-2", role_id=None,
                                provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=9)
        _, _, res = self.send(web.agent_token(TOKEN, "ghost"), to="#squad", text="x")
        self.assertEqual(res["delivered"], ["other"])

    def test_an_agent_in_no_squad_reaches_only_peers_in_none(self):
        self.assertEqual(self.send(web.agent_token(TOKEN, "loner"), to="#squad", text="x")[0], 400)
        self.assertEqual(self.send(web.agent_token(TOKEN, "loner"), to="@squad", text="x")[0], 400)
        status, _, res = self.send(web.agent_token(TOKEN, "loner"), to="#impl", text="x")
        self.assertEqual((status, res["delivered"]), (200, []), "impl subscribes to #impl but is in a squad")
        self.assertEqual(self.heard(), set())


class AgentsLiveTests(MeshRig):
    def test_reads_faster_only_while_a_spawned_agent_runs(self):
        self.assertFalse(self.service.agents_live())
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=500)
        self.assertTrue(self.service.agents_live())

    def test_a_launched_session_counts_before_it_has_a_spawn_record(self):
        self.service.launched["not-yet-named"] = 200
        self.assertTrue(self.service.agents_live())


def peer_line(session, ts, sender, body):
    text = uds.format_cross_session(body, from_pipe="p", from_name=sender)
    return {"type": "user", "sessionId": session, "timestamp": ts, "cwd": "c", "message": {"content": text}}


class ContactStreamTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        for name in ("a", "b"):
            self.store.record_spawn(agent_name=f"agent-{name}", session_id=f"sess-{name}", squad_id="sq", role_id=None,
                                    provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def test_a_transcript_copy_is_dropped_only_for_the_recipient_the_hub_journaled(self):
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        self.store.record_message(from_name="arch", to_name="agent-a", to_session="sess-a", body="LGTM", ts=now)
        self.store.record_message(from_name="arch", to_name="@squad", body="standup", ts=now, squad_id="sq",
                                  transport="uds-broadcast")
        self.store.record_message(from_name="arch", to_name="@squad", body="retro", ts=now, squad_id="other-sq",
                                  transport="uds-broadcast")
        for who in ("a", "b"):
            write_jsonl(self.projects / "p" / f"sess-{who}.jsonl",
                        [peer_line(f"sess-{who}", iso, "arch", body) for body in ("LGTM", "standup", "retro")])
        self.store.ingest(self.projects)
        copies = {(m["to_name"], m["body"]) for m in self.store.messages() if m["transport"] == "uds-transcript"}
        # B's LGTM came by native SendMessage, and a broadcast in another squad stands for nothing here: both are new
        self.assertEqual(copies, {("agent-b", "LGTM"), ("agent-a", "retro"), ("agent-b", "retro")})


OLD_SPAWNED = """create table spawned_agents (agent_name text primary key, session_id text, squad_id text, role_id text,
    provider text not null, model text, effort text, cwd text not null, worktree_path text, pid integer,
    created_ts real not null)"""


class MigrationTests(Fixture):
    def test_v4_rekeys_spawns_cleans_messages_and_upgrades_untouched_briefs_once(self):
        path = self.root / "old.db"
        Store(path).close()                                    # today's schema, then wind it back to v3
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("drop table spawned_agents")
        db.execute(OLD_SPAWNED)
        db.execute("insert into spawned_agents values ('impl', 'sess-1', 'sq-1', null, 'claude', null, null, '.', null, 1, 1.0)")
        db.execute("insert into files values ('x.jsonl', 1, 1, 1)")
        db.execute("insert into messages (msg_id, ts, from_name, to_name, to_session, body, transport) "
                   "values ('h', 100, 'arch', 'impl', 'sess-1', 'go', 'uds')")
        for mid, ts in (("t1", 150), ("t2", 5000)):           # a copy of the hub's row, and the same words much later
            db.execute("insert into messages (msg_id, ts, from_name, to_name, to_session, body, transport) "
                       "values (?, ?, 'arch', 'sess-1', 'sess-1', 'go', 'uds-transcript')", (mid, ts))
        db.execute("update roles set system_prompt = ? where id = 'architect-claude'", (store_mod.OLD_ARCHITECT,))
        db.execute("update roles set system_prompt = 'mine' where id = 'reviewer-claude'")
        db.execute("update meta set value = '3' where key = 'schema'")
        db.commit()
        db.close()

        s = Store(path)
        self.addCleanup(s.close)
        pk = {r[1] for r in s.db.execute("pragma table_info(spawned_agents)") if r[5]}
        self.assertEqual(pk, {"session_id"})
        self.assertEqual([(a["agent_name"], a["squad_id"], a["channels"]) for a in s.spawned()], [("impl", "sq-1", [])])
        self.assertEqual([(m["msg_id"], m["to_name"], m["squad_id"]) for m in s.messages() if m["transport"] == "uds-transcript"],
                         [("t2", "impl", "sq-1")])
        self.assertEqual(s.role("architect-claude")["system_prompt"], store_mod.ARCHITECT)
        self.assertEqual(s.role("reviewer-claude")["system_prompt"], "mine", "an edited brief is never overwritten")
        self.assertEqual(s.db.execute("select count(*) from files").fetchone()[0], 1, "v4 does not force a re-read")
        s.db.execute("update roles set system_prompt = ? where id = 'architect-gemini'", (store_mod.OLD_ARCHITECT,))
        s.db.commit()
        s.close()
        again = Store(path)                                    # v4 ran once: a second start leaves things alone
        self.addCleanup(again.close)
        self.assertEqual(again.role("architect-gemini")["system_prompt"], store_mod.OLD_ARCHITECT)


class HalfDoneRekeyTests(Fixture):
    def old_db(self):
        path = self.root / "old.db"
        Store(path).close()
        db = sqlite3.connect(path)
        db.execute("drop table spawned_agents")
        db.execute(OLD_SPAWNED)
        db.execute("insert into spawned_agents values ('impl', 'sess-1', 'sq-1', null, 'claude', null, null, '.', null, 1, 1.0)")
        db.commit()
        db.close()
        return path

    def test_rows_stranded_by_an_interrupted_rebuild_are_recovered(self):
        path = self.old_db()
        db = sqlite3.connect(path)                     # what a crash right after the rename left behind
        db.execute("alter table spawned_agents rename to spawned_agents_by_name")
        db.commit()
        db.close()
        s = Store(path)                                # __init__ makes an empty new table; the rows must come back
        self.addCleanup(s.close)
        self.assertEqual([a["agent_name"] for a in s.spawned()], ["impl"])
        self.assertIsNone(s.db.execute("select 1 from sqlite_master where name = 'spawned_agents_by_name'").fetchone())

    def test_a_failed_rebuild_leaves_the_old_table_untouched(self):
        path = self.old_db()
        with mock.patch.object(store_mod, "SPAWNED_COLUMNS", "no_such_column"), self.assertRaises(sqlite3.Error):
            Store(path)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        pk = [r[1] for r in db.execute("pragma table_info(spawned_agents)") if r[5]]
        self.assertEqual((pk, db.execute("select agent_name from spawned_agents").fetchall()), (["agent_name"], [("impl",)]))
        self.assertIsNone(db.execute("select 1 from sqlite_master where name = 'spawned_agents_by_name'").fetchone())


class SpawnRecordTests(Fixture):
    def test_a_spawn_without_a_session_is_refused(self):
        s = Store(self.root / "a.db")
        self.addCleanup(s.close)
        with self.assertRaises(ValueError):
            s.record_spawn(agent_name="x", session_id=None, squad_id=None, role_id=None, provider="claude",
                           model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def test_a_second_squad_reusing_a_handle_keeps_the_first_squads_roster(self):
        s = Store(self.root / "a.db")
        self.addCleanup(s.close)
        for sid, squad, ts in (("s1", "sq-1", 1.0), ("s2", "sq-2", 2.0)):
            with mock.patch.object(store_mod.time, "time", return_value=ts):
                s.record_spawn(agent_name="impl", session_id=sid, squad_id=squad, role_id=None, provider="claude",
                               model=None, effort=None, cwd=".", worktree_path=None, pid=1)
        self.assertEqual([a["squad_id"] for a in s.spawned()], ["sq-2", "sq-1"], "newest first")
        self.assertEqual({sq["id"]: len(sq["agents"]) for sq in s.squads()}, {})       # no squad rows were created
        s.update_spawn_model(agent_name="impl", model="m2")
        self.assertEqual([a["model"] for a in s.spawned()], ["m2", None], "a name means its newest spawn")
        s.db.execute("update spawned_agents set channels = 'not json' where session_id = 's1'")
        self.assertEqual(s.spawned()[1]["channels"], [])


class FeedTextTests(Fixture):
    def test_prose_keeps_indentation_and_drops_blank_ends(self):
        self.assertEqual(prose("\n\n    indented\n  next\n\n\n\nend  \n\n"), "    indented\n  next\n\nend")

    def test_an_error_made_of_several_blocks_keeps_its_lines(self):
        path = self.projects / "p" / "s.jsonl"
        write_jsonl(path, [{"type": "user", "sessionId": "s", "timestamp": "t", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u", "is_error": True,
             "content": [{"type": "text", "text": "Traceback:"}, {"type": "text", "text": "  boom"}]}]}}])
        self.assertEqual(tail_transcript(path)[-1]["text"], "Traceback:\n  boom")


class McpStdinTests(Fixture):
    def test_mcp_server_reads_its_pipe_as_utf8(self):
        sent = []

        def fake_api(base, token, path, method="GET", body=None, timeout=30.0):
            if body is not None:
                sent.append(body)
            return {"latest_id": 0, "messages": []}

        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "mesh_send", "arguments": {"to": "arch", "message": "a—b"}}}
        stdin = io.TextIOWrapper(io.BytesIO((json.dumps(call, ensure_ascii=False) + "\n").encode("utf-8")), encoding="cp1252")
        with mock.patch.object(mcp_mesh, "http_api", fake_api), mock.patch("sys.stdin", stdin), \
                mock.patch("sys.stdout", io.StringIO()):
            mcp_mesh.main(["--self", "me", "--url", "http://x", "--token", "t"])
        self.assertEqual(sent[0]["text"], "a—b")
