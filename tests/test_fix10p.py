"""FIX-10P, the Python half of the Gemini cross-model review (rev-carbon): ingest's last write under the lock; one
BY MODEL row per Gemini model; worktime's open turn, image prompts, waits in flight and one sliver rule; a question
answered before it reached the journal; the inbox without the operator's broadcasts; kind filters past 120
events; the join notice not sent to the newcomer; exact-command allow rules for Gemini roles."""
import json
import sqlite3
import threading
import time
from unittest import mock

from support import TOKEN, Fixture, assistant, result, runner_probe, tool_use, write_jsonl
from test_elastic import TASK, ElasticRig
from test_models_report import outcome, reply
from test_server_fixes import MeshRig, peer_line

from almanac import gemini_host, spawner, store as st, uds
from almanac.store import Store


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class IngestLockTests(Fixture):
    def test_ingest_and_the_message_journal_share_the_connection_safely(self):
        store = Store(self.root / "a.db")
        self.addCleanup(store.close)
        write_jsonl(self.projects / "p" / "s.jsonl", [assistant("s", iso(time.time()), "m", [{"type": "text", "text": "x"}])])
        errors = []

        def ingest():
            try:
                for _ in range(30):
                    with store.lock:                           # every pass reads again
                        store.db.execute("delete from files")
                        store.db.commit()
                    store.ingest(self.projects)
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        def journal():
            try:
                for i in range(200):
                    store.record_message(from_name="a", to_name="b", body=f"x{i}")
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=ingest), threading.Thread(target=journal)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(store.messages(limit=500)), 200)


class GeminiModelTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(self.store.close)
        self.now = time.time()
        self.t0 = self.now - 3600

    def ingest(self, name, records, provider="claude"):
        write_jsonl(self.root / f"tx-{provider}" / "D---proj" / f"{name}.jsonl", records)
        self.store.ingest(self.root / f"tx-{provider}", provider=provider)

    def test_the_wire_id_and_the_base_id_are_one_model(self):
        t = self.t0
        self.ingest("g1", [
            reply("g1", t, "g-m1", [tool_use("gu1", "run_command", command="ls")], "gemini-4-carbon-high", output=0),
            outcome("g1", t + 1, "gu1"),
            reply("g1", t + 2, "g-m2", [{"type": "text", "text": "done"}], "gemini-4-carbon", output=500),
        ], provider="gemini")
        rows = [(m["model"], m["calls"], m["output_tokens"]) for m in self.store.report(days=7, now=self.now)["models"]
                if m["provider"] == "gemini"]
        self.assertEqual(rows, [("gemini-4-carbon", 1, 500)])

    def test_a_reread_corrects_calls_stored_with_the_wire_id(self):
        t = self.t0
        self.ingest("g1", [reply("g1", t, "g-m1", [tool_use("gu1", "run_command", command="ls")], "gemini-4-carbon-high")],
                    provider="gemini")
        self.store.db.execute("update calls set model = 'gemini-4-carbon-high'")    # as rows ingested before the fix
        self.store.db.execute("delete from files")
        self.store.db.commit()
        self.store.ingest(self.root / "tx-gemini", provider="gemini")
        self.assertEqual(self.store.db.execute("select model from calls").fetchone()[0], "gemini-4-carbon")
        self.assertEqual(gemini_host.split_agy_model_effort("gemini-4-carbon-high")[0], "gemini-4-carbon",
                         "the host writes this base id on its tool records")


class OpenTurnTests(Fixture):
    """The reviewer's snippet: a turn that ended long ago is not stretched across the idle gap."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)

    def test_a_busy_session_after_a_long_gap_gets_a_provisional_turn(self):
        self.store._turn("s1", 0.0, "user", "hi")
        self.store._turn("s1", 60.0, "assistant", [])
        [a] = self.store.worktime(["s1"], start=0.0, end=3600.0, open_until={"s1": 3600.0})["agents"]
        self.assertEqual(a["work_s"], 60.0 + st.OPEN_TURN_S, "60 s of turn, then the last 30 s, not the idle hour")

    def test_a_turn_that_just_ended_is_stretched(self):
        self.store._turn("s1", 0.0, "user", "hi")
        self.store._turn("s1", 3500.0, "assistant", [])
        [a] = self.store.worktime(["s1"], start=0.0, end=3600.0, open_until={"s1": 3600.0})["agents"]
        self.assertEqual(a["work_s"], 3600.0)

    def test_an_image_only_prompt_opens_a_turn(self):
        self.store._turn("s1", 0.0, "user", "hi")
        self.store._turn("s1", 60.0, "assistant", [])
        self.store._turn("s1", 3600.0, "user", [{"type": "image", "source": {}}])
        self.assertEqual(self.store.work_summary("s1"), dict(work_s=60.0, wait_s=0.0, turns=2))

    def test_a_wait_still_in_flight_is_waiting(self):
        now = time.time()
        write_jsonl(self.projects / "p" / "s2.jsonl", [
            {"type": "user", "sessionId": "s2", "timestamp": iso(now - 50), "message": {"content": "wait for review"}},
            assistant("s2", iso(now - 40), "m1", [tool_use("w1", "mcp__almanac-mesh__mesh_wait", timeout_seconds=120)]),
        ])
        self.store.ingest(self.projects)
        [a] = self.store.worktime(["s2"], open_until={"s2": now})["agents"]
        self.assertAlmostEqual(a["wait_s"], 40.0, delta=1.5)
        self.assertAlmostEqual(a["work_s"], 10.0, delta=1.5)

    def test_one_sliver_rule_for_every_figure(self):
        got = st.concurrency({"A": [(0, 0.4)], "B": [(0.1, 0.5)]}, start=0, end=10)
        self.assertEqual((got["peak"], got["any_s"], got["parallel_s"], got["pairs"]), (0, 0.0, 0.0, []))
        self.assertEqual(got["by_count"], {0: 10.0})


class LateQuestionTests(MeshRig):
    def setUp(self):
        super().setUp()
        for name in ("arch", "impl"):
            self.store.record_spawn(agent_name=name, session_id=f"sess-{name}", squad_id="sq-1", role_id=None,
                                    provider="claude", model=None, effort=None, cwd=".", worktree_path=None, pid=1)

    def test_a_native_question_answered_through_the_hub_first(self):
        t = float(int(time.time()))
        answer = self.store.record_message(from_name="impl", to_name="arch", squad_id="sq-1", body="yes, v6", ts=t + 5)
        # arch's question went by native SendMessage, so it reaches the journal at impl's next ingest
        write_jsonl(self.projects / "p" / "sess-impl.jsonl",
                    [peer_line("sess-impl", iso(t), "arch", uds.needs_marker("blocking", "arch") + "which schema?")])
        self.store.ingest(self.projects)
        [question] = [m for m in self.store.messages() if m["needs"]]
        self.assertEqual((question["answered_by"], question["answered_ts"]), (answer["id"], t + 5))
        self.assertEqual(self.store.open_needs("sq-1"), [])

    def test_a_hub_question_answered_by_a_native_reply_with_a_whole_second_time(self):
        t = float(int(time.time()))
        question = self.store.record_message(from_name="arch", to_name="impl", squad_id="sq-1", body="which schema?",
                                             needs="reply", ts=t + 0.8)
        write_jsonl(self.projects / "p" / "sess-arch.jsonl", [peer_line("sess-arch", iso(t), "impl", "v6")])
        self.store.ingest(self.projects)
        self.assertIsNotNone(self.store.message(question["id"])["answered_ts"], "within the 2 s clock tolerance")


class InboxBroadcastTests(MeshRig):
    def test_the_operators_broadcasts_are_not_inbox_threads(self):
        for frm, to, body in (("operator", "@squad", "[Squad x launched]"), ("operator", "#review", "look"),
                              ("operator", "impl", "status?"), ("impl", "operator", "green")):
            self.store.record_message(from_name=frm, to_name=to, body=body, squad_id="sq-1")
        _, _, doc = self.request(self.handler, "/api/operator/inbox")
        self.assertEqual([m["body"] for m in doc["messages"]], ["status?", "green"])


class LongFeedTests(Fixture):
    def setUp(self):
        super().setUp()
        from almanac.door import Service, make_handler
        from almanac.fleet import Fleet
        from test_fleet import FakeMachine
        self.home = self.root / "claude"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "sessions" / "100.json").write_text(json.dumps({
            "pid": 100, "sessionId": "sess-a", "cwd": "D:\\x", "status": "busy", "name": "a", "kind": "interactive"}),
            encoding="utf-8")
        machine = FakeMachine()
        store = Store(self.root / "a.db")
        self.addCleanup(store.close)
        self.handler = make_handler(Service(store, self.projects, 300, fleet=Fleet(
            self.home, table=machine.table, facts=machine.facts, lines=machine.lines)), TOKEN)

    def test_a_prompt_before_200_tool_calls_is_still_found(self):
        home = self.home
        records = [{"type": "user", "timestamp": "t0", "message": {"content": "the long task"}}]
        for i in range(200):
            records.append(assistant("sess-a", f"t{i}a", f"x{i}", [tool_use(f"x{i}", "Bash", command=f"echo {i}")]))
            records.append(result("sess-a", f"t{i}b", f"x{i}", f"out {i}"))
        write_jsonl(home / "projects" / "D--x" / "sess-a.jsonl", records)
        _, _, doc = self.request(self.handler, "/api/session?pid=100&limit=5&kinds=you")
        self.assertEqual([e["text"] for e in doc["events"]], ["the long task"])


class JoinNoticeTests(ElasticRig):
    def test_the_squad_hears_the_join_but_the_newcomer_does_not(self):
        sent = []
        real = self.service.mesh_send
        with mock.patch.object(self.service, "mesh_send", side_effect=lambda **kw: sent.append(kw) or real(**kw)):
            status, _, res = self.spawn()
        self.assertEqual(status, 200)
        [notice] = sent
        self.assertIn(res["agent_name"], notice["exclude"])
        self.assertIn(res["session_id"], notice["exclude"])

    def test_exclude_keeps_a_live_peer_out_of_a_broadcast(self):
        status, _, res = self.send(to="@squad", text="hello", squad_id="sq-1")
        self.assertEqual(sorted(res["delivered"]), ["arch", "impl"])
        res = self.service.mesh_send(to="@squad", text="joined", squad_id="sq-1", exclude={"impl"})
        self.assertEqual(res["delivered"], ["arch"])


class AllowedCommandTests(Fixture):
    def test_the_role_field_round_trips_and_reviewers_ship_with_it(self):
        s = Store(self.root / "a.db")
        self.addCleanup(s.close)
        self.assertEqual(s.role("reviewer-gemini")["allowed_commands"], st.REVIEWER_COMMANDS)
        self.assertEqual(s.role("implementer-gemini")["allowed_commands"], [])
        base = dict(id="scribe", provider="gemini", system_prompt="write")
        self.assertEqual(s.upsert_role(dict(base, allowed_commands="git log -1\n\ngit status --short"))["allowed_commands"],
                         ["git log -1", "git status --short"])
        self.assertEqual(s.upsert_role(base)["allowed_commands"], ["git log -1", "git status --short"], "left out keeps it")
        self.assertEqual(s.upsert_role(dict(base, allowed_commands=[]))["allowed_commands"], [])

    def test_v7_gains_the_column_and_the_reviewer_defaults(self):
        path = self.root / "v7.db"
        Store(path).close()
        db = sqlite3.connect(path)
        db.execute("alter table roles drop column allowed_commands")
        db.execute("update meta set value = '7' where key = 'schema'")
        db.commit()
        db.close()
        s = Store(path)
        self.addCleanup(s.close)
        self.assertEqual({r["id"]: r["allowed_commands"] for r in s.roles() if r["allowed_commands"]},
                         {"reviewer-gemini": st.REVIEWER_COMMANDS, "reviewer-claude": st.REVIEWER_COMMANDS})

    def test_the_host_grants_exactly_those_lines(self):
        allow = json.loads(gemini_host.grants(["git diff", "python -m unittest discover tests"]))["permissions"]["allow"]
        self.assertEqual(allow[-2:], ["command(git diff)", "command(python -m unittest discover tests)"])
        self.assertEqual(gemini_host.grants([]), gemini_host.MESH_GRANTS)
        host = gemini_host.GeminiHost(name="g", cwd=str(self.root), transcript_path=self.root / "t.jsonl",
                                      sessions_dir=self.root / "s", agy_bin="agy", in_memory_uds=True, quiet=True,
                                      allowed_commands=["git diff"])
        self.addCleanup(host.peer.close)
        with mock.patch.object(gemini_host.subprocess, "Popen") as popen:
            popen.return_value.stderr = None
            host._ensure_stream_proc()
        argv = popen.call_args[0][0]
        self.assertIn("command(git diff)", argv[argv.index("--settings") + 1])

    def test_the_mode_fallback_reaches_the_session_record(self):
        host = gemini_host.GeminiHost(name="g", cwd=str(self.root), transcript_path=self.root / "t.jsonl",
                                      sessions_dir=self.root / "s", agy_bin="agy", in_memory_uds=True, quiet=True)
        self.addCleanup(host.peer.close)
        host.note_mode("Warning: --mode auto is not available for this account; continuing in the default mode")
        [peer] = [p for p in uds.discover_peers(self.root / "s", alive_fn=lambda _: True) if p["name"] == "g"]
        self.assertIn("not available for this account", peer["mode_note"])

    def test_the_charter_names_the_lines_for_gemini_only(self):
        text = spawner.build_mesh_charter(agent_name="r", provider="gemini", role_title="R", role_prompt="p",
                                          almanac_url="u", allowed_commands=["git diff", "git status --short"])
        self.assertIn("only permits these exact command lines, typed character for character: `git diff`, "
                      "`git status --short`; anything else is denied, so do not try variants.", text)
        claude = spawner.build_mesh_charter(agent_name="r", provider="claude", role_title="R", role_prompt="p",
                                            almanac_url="u", allowed_commands=["git diff"])
        self.assertNotIn("exact command lines", claude, "informational for Claude agents")


class GeminiSpawnTests(ElasticRig):
    def test_a_gemini_reviewer_is_launched_with_its_commands(self):
        self.service.probe = runner_probe(self.root, claude=True, gemini=True)
        self.service._runners = None
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST",
                                      body={"squad_id": "sq-1", "role_id": "reviewer-gemini", "task": TASK})
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        cmds = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-command"]
        self.assertEqual(cmds, st.REVIEWER_COMMANDS)
        self.assertIn("mode_note", res)
