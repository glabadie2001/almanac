"""T-23: a role's context_policy is enforced when a task becomes accepted, in the hub. fresh-per-task retires the
owner (its session ends, its spawn row is retired and gives its slot back, its worktree is left alone); compact-at-N
sends `/compact` to the owner and to the lead that filed the task, once per context reading; a Gemini host turns the
hub's control op into a raw `/compact` turn whose empty reply is not a failure."""
import io
import json
from unittest import mock

from support import TOKEN, Fixture
from test_respawn import RespawnRig

from almanac import door, fleet as fl, gemini_host, spawner, web
from almanac.store import Store


class EnforceRig(RespawnRig):
    """RespawnRig's sq-1: arch (architect-claude, compact-at-300000) filed T-1 for impl (implementer-claude,
    fresh-per-task, live as pid 500, added by arch)."""

    def log(self, agent="arch", task=None, **body):
        token = web.agent_token(TOKEN, agent) if agent else TOKEN
        return self.request(self.handler, "/api/mesh/object/log", token=token, method="POST",
                            body={"ref": f"T-{(task or self.task)['id']}", **body})

    def accept(self, agent="arch", task=None):
        status, _, res = self.log(agent, task, status="accepted")
        self.assertEqual(status, 200, res)
        return res

    def entries(self, kind, task=None):
        return [e for e in self.store.object_log((task or self.task)["id"], 100) if e["kind"] == kind]

    def retired(self, sid="sess-impl"):
        return self.store.spawn_by_session(sid)["retired_ts"] is not None


class RetireTests(EnforceRig):
    def test_only_accepted_retires_the_owner(self):
        for status in ("in review", "blocked", "in progress"):
            self.log(status=status)
        self.assertEqual((self.killed, self.retired()), ([], False))
        self.store.db.execute("insert into sessions (id, first_ts, last_ts, cwd, context) values (?, 1, 2, ?, ?)",
                              ("sess-impl", str(self.ws), 412345))
        self.store.db.commit()
        self.accept()
        self.assertEqual(self.killed, [500], "its session was ended")
        self.assertTrue(self.retired())
        [entry] = self.entries("retired")
        self.assertEqual(entry["agent_name"], "almanac-hub")
        for text in ("impl retired", "sess-impl", "412,345 tokens", "worktree"):
            self.assertIn(text, entry["body"])

    def test_the_operator_accepting_retires_it_too(self):
        self.accept(agent=None)
        self.assertEqual((self.killed, self.retired()), ([500], True))

    def test_its_worktree_is_left_alone(self):
        wt = self.state / "worktrees" / "sq-1" / "impl"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        (wt / "half-done.py").write_text("wip", encoding="utf-8")
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(wt), worktree_path=str(wt), pid=500,
                                spawned_by="arch")
        self.accept()
        self.assertTrue(self.retired())
        self.assertEqual((wt / "half-done.py").read_text(encoding="utf-8"), "wip")
        self.assertEqual(self.store.spawn_by_session("sess-impl")["worktree_path"], str(wt))
        self.assertEqual(self.argv, [], "no git, nothing launched")

    def test_an_owner_with_another_open_task_is_kept(self):
        other = self.object(owner="sess-impl", title="The next widget")
        self.accept()
        self.assertEqual((self.killed, self.retired()), ([], False))
        [entry] = self.entries("retired")
        self.assertIn(f"still owns T-{other['id']}", entry["body"])
        self.accept(task=other)                   # its last open task: now it goes
        self.assertEqual((self.killed, self.retired()), ([500], True))

    def test_a_role_that_is_not_fresh_is_not_retired(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), context_policy="none"))
        self.accept()
        self.assertEqual((self.killed, self.retired(), self.entries("retired")), ([], False, []))

    def test_an_owner_spawned_into_another_squad_is_not_retired(self):
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-2", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.ws), worktree_path=None, pid=500)
        self.accept(agent=None)
        self.assertEqual((self.killed, self.retired()), ([], False))

    def test_a_kill_that_fails_keeps_the_member(self):
        self.fleet.kill.side_effect = lambda pid, created: (False, "Access is denied.", {"pid": pid})
        used = self.used()
        self.accept()
        self.assertFalse(self.retired())
        self.assertEqual(self.used(), used)
        self.assertIn("was not retired", self.entries("retired")[0]["body"])

    def test_an_owner_that_already_ended_is_still_retired(self):
        self.machine.procs.pop(500)
        self.accept()
        self.assertEqual((self.killed, self.retired()), ([], True))
        self.assertIn("was not running", self.entries("retired")[0]["body"])


class SelfTests(EnforceRig):
    """An owner that accepts its own task is ended only after its response is written."""

    def test_the_response_goes_out_before_the_session_ends(self):
        order = []
        real_send = web.JsonHandler._send
        kill = self.fleet.kill.side_effect
        self.fleet.kill.side_effect = lambda pid, created: (order.append("kill"), kill(pid, created))[1]
        with mock.patch.object(web.JsonHandler, "_send", lambda h, code, doc: (order.append("sent"), real_send(h, code, doc))[1]):
            status, _, res = self.log("impl", status="accepted")
        self.assertEqual(status, 200, res)
        self.assertEqual(order, ["sent", "kill"])
        self.assertTrue(self.retired())
        self.assertIn("ends once this reply is written", self.entries("retired")[0]["body"])

    def test_the_kill_waits_in_after(self):
        after = []
        caller = self.service.caller("impl", None)
        door.log_to_object(self.service, "impl", caller, {"ref": f"T-{self.task['id']}", "status": "accepted"},
                           after=after)
        self.assertEqual((self.killed, len(after), self.retired()), ([], 1, True))
        after[0]()
        self.assertEqual(self.killed, [500])

    def test_an_end_that_fails_after_the_reply_takes_the_retire_back(self):
        self.fleet.kill.side_effect = lambda pid, created: (False, "Access is denied.", {"pid": pid})
        after, used = [], self.used()
        door.log_to_object(self.service, "impl", self.service.caller("impl", None),
                           {"ref": f"T-{self.task['id']}", "status": "accepted"}, after=after)
        self.assertEqual((self.retired(), self.used()), (True, used - 1))
        after[0]()
        self.assertEqual((self.retired(), self.used()), (False, used), "still running: its row and slot come back")
        self.assertIn("was not retired", self.entries("retired")[0]["body"])

    def test_with_no_way_to_end_it_the_retire_is_not_deferred(self):
        after = []
        with mock.patch.object(door, "_live_proc", return_value=dict(pid=500)):     # no `created`: nothing to kill by
            door.log_to_object(self.service, "impl", self.service.caller("impl", None),
                               {"ref": f"T-{self.task['id']}", "status": "accepted"}, after=after)
        self.assertEqual((after, self.killed, self.retired()), ([], [], False))
        self.assertIn("was not retired", self.entries("retired")[0]["body"])


class SlotTests(EnforceRig):
    def test_a_retired_member_gives_its_slot_back_and_a_respawn_takes_none(self):
        self.assertEqual(self.used(), 1)
        self.accept()
        self.assertEqual(self.used(), 0, "retired: its slot is back")
        self.log(status="in progress", body="one more thing")
        status, _, res = self.request(self.handler, "/api/mesh/object/respawn", token=web.agent_token(TOKEN, "arch"),
                                      method="POST", body={"ref": f"T-{self.task['id']}"})
        self.assertEqual(status, 200, res)
        self.assertIn("was not running", self.store.object_log(self.task["id"], 1)[0]["body"])
        self.assertEqual(self.used(), 1, "the same handle is a member again: one slot, not two")
        self.assertFalse(self.retired(res["session_id"]))

    def test_on_a_created_ts_tie_the_newest_row_is_the_last_inserted(self):
        self.accept()
        self.store.record_spawn(agent_name="impl", session_id="sess-impl-2", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.ws), worktree_path=None, pid=501,
                                spawned_by="arch")
        self.store.db.execute("update spawned_agents set created_ts = 5 where agent_name = 'impl'")
        self.store.db.commit()
        self.assertEqual(self.store.spawned(squad_id="sq-1", agent_name="impl", limit=1)[0]["session_id"], "sess-impl-2")
        self.assertEqual(self.used(), 1, "the respawned handle is a member: its newest row is not retired")

    def test_a_retired_handle_cannot_be_given_a_task(self):
        self.accept()
        status, _, res = self.request(self.handler, "/api/mesh/object", token=web.agent_token(TOKEN, "arch"),
                                      method="POST", body={"title": "Next", "owner": "impl"})
        self.assertEqual(status, 409)
        self.assertIn("mesh_spawn(ref=", res["error"])
        queued = self.object()
        status, _, res = self.log(task=queued, owner="impl")
        self.assertEqual(status, 409)
        self.assertIn("retired", res["error"])
        self.assertIsNone(self.owner(queued))


class CompactTests(EnforceRig):
    """The fleet row of each session is set by the test; console.send and uds.send_peer are recorded."""

    def setUp(self):
        super().setUp()
        self.procs = {"sess-arch": dict(pid=100, role="session", provider="claude", context=None)}
        patcher = mock.patch.object(door, "_live_proc", side_effect=lambda service, sid: self.procs.get(sid))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.typed, self.told = [], []
        for target, attr, sink in (("almanac.console.send", None, self.typed), ("almanac.uds.send_peer", None, self.told)):
            p = mock.patch(target, side_effect=lambda *a, sink=sink, **k: sink.append((a, k)) or (True, "typed"))
            p.start()
            self.addCleanup(p.stop)

    def lead_at(self, context, total=False):
        self.procs["sess-arch"].update(context=context, context_total=total)

    def test_below_n_nothing(self):
        self.lead_at(299_999)
        self.accept()
        self.assertEqual((self.typed, self.entries("compact")), ([], []))

    def test_at_n_the_lead_is_compacted_through_its_console(self):
        self.lead_at(300_000)
        self.accept()
        self.assertEqual([a for a, _ in self.typed], [(100, "/compact")])
        [entry] = self.entries("compact")
        self.assertIn("arch at 300,000 tokens", entry["body"])

    def test_only_on_accepted(self):
        self.lead_at(400_000)
        self.log(status="in review")
        self.assertEqual(self.typed, [])

    def test_a_running_total_never_triggers(self):
        self.lead_at(900_000, total=True)
        self.accept()
        self.assertEqual(self.typed, [])

    def test_a_headless_claude_is_skipped_and_the_log_says_so(self):
        self.lead_at(400_000)
        self.procs["sess-arch"]["role"] = "headless"
        self.accept()
        self.assertEqual(self.typed, [])
        self.assertIn("headless", self.entries("compact")[0]["body"])

    def at(self, seconds):
        return mock.patch.object(door.time, "monotonic", return_value=1_000_000.0 + seconds)

    def test_no_second_compact_on_the_same_reading(self):
        self.lead_at(400_000)
        second = self.object(owner="sess-impl")
        with self.at(0):
            self.accept()
        with self.at(door.COMPACT_COOLDOWN + 1):
            self.accept(task=second)
        self.assertEqual(len(self.typed), 1, "the same reading: one /compact")

    def test_two_accepts_a_minute_apart_send_once(self):
        second = self.object(owner="sess-impl")
        self.lead_at(400_000)
        with self.at(0):
            self.accept()
        self.lead_at(410_000)                     # a busy lead: its reading moved before the queued /compact ran
        with self.at(60):
            self.accept(task=second)
        self.assertEqual(len(self.typed), 1)

    def test_after_the_cooldown_it_sends_again(self):
        second = self.object(owner="sess-impl")
        self.lead_at(400_000)
        with self.at(0):
            self.accept()
        self.lead_at(410_000)
        with self.at(door.COMPACT_COOLDOWN + 1):
            self.accept(task=second)
        self.assertEqual(len(self.typed), 2)

    def test_the_headless_note_is_given_once_within_the_cooldown(self):
        self.procs["sess-arch"]["role"] = "headless"
        second = self.object(owner="sess-impl")
        self.lead_at(400_000)
        self.accept()
        self.lead_at(410_000)
        self.accept(task=second)
        self.assertEqual((self.typed, len(self.entries("compact")), len(self.entries("compact", second))), ([], 1, 0))

    def test_another_squads_member_of_the_same_name_is_not_the_creator(self):
        self.store.record_spawn(agent_name="arch", session_id="sess-arch-2", squad_id="sq-2", role_id="architect-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.ws), worktree_path=None, pid=101)
        theirs = self.object(owner="sess-impl")
        self.store.db.execute("update objects set created_by = 'sess-arch-2' where id = ?", (theirs["id"],))
        self.store.db.commit()
        self.lead_at(400_000)
        self.accept(agent=None, task=theirs)
        self.assertEqual(self.typed, [], "filed by sq-2's arch: this squad's arch is not its creator")

    def test_owner_and_creator_both(self):
        self.store.upsert_role(dict(self.store.role("implementer-claude"), context_policy="compact-at-1000"))
        self.procs["sess-impl"] = dict(pid=500, role="session", provider="claude", context=1000)
        self.lead_at(300_000)
        self.accept()
        self.assertEqual(sorted(a[0] for a, _ in self.typed), [100, 500])
        self.assertEqual(len(self.entries("compact")), 2)

    def test_an_owner_that_filed_its_own_task_is_compacted_once(self):
        mine = self.object(owner="sess-arch")
        self.lead_at(300_000)
        self.accept(agent=None, task=mine)
        self.assertEqual(len(self.typed), 1)

    def test_a_fresh_owner_is_retired_while_the_lead_is_compacted(self):
        self.procs["sess-impl"] = dict(pid=500, created="1", role="session", provider="claude", context=None)
        self.lead_at(300_000)
        self.accept()
        self.assertEqual((self.killed, self.retired(), len(self.typed)), ([500], True, 1))

    def test_a_gemini_agent_is_compacted_through_its_host(self):
        self.procs["sess-arch"].update(provider="gemini", has_uds=True, context=350_000)
        self.accept()
        self.assertEqual(self.typed, [], "never typed into a Gemini host's console")
        [(args, kwargs)] = self.told
        self.assertEqual((args[1], json.loads(args[2]), kwargs["channel"]), ("100", {"op": "compact"}, "__almanac_control__"))
        self.assertIn("/compact sent", self.entries("compact")[0]["body"])


class GeminiHostTests(Fixture):
    def test_the_control_op_runs_a_raw_compact_turn_whose_empty_reply_is_not_a_failure(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        tx = self.root / "gem.jsonl"
        host = gemini_host.GeminiHost(name="gem", cwd=str(self.root), transcript_path=tx, sessions_dir=sessions,
                                      session_id="s-gem", in_memory_uds=True, quiet=True, system_prompt="Be a lead.")
        self.addCleanup(host.close)
        host._on_peer_message({"channel": "__almanac_control__", "body": json.dumps({"op": "compact"})})
        item = host.turns.get_nowait()
        out = [{"type": "step_update", "step_update": {"step_type": "checkpoint", "state": "DONE"}},
               {"type": "result", "result": {"status": "SUCCESS", "response": ""}}]
        proc = mock.MagicMock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in out)))
        with mock.patch.object(host, "_ensure_stream_proc", return_value=proc):
            host.run_one_turn(item)
        wire = json.loads(proc.stdin.getvalue())
        self.assertEqual(wire["message"]["content"], "/compact", "exactly /compact: no role wrap, no peer prompt")
        self.assertEqual(host._turn_index, 0, "the next real turn still carries the role instructions")
        events, meta = fl.read_tail(tx)
        self.assertIsNone(meta["stall"], "an empty SUCCESS is not a failed turn")
        self.assertEqual(events[-1]["text"], "(compacted)")


    def test_a_compact_turn_keeps_the_step_reading(self):
        """agy's checkpoint step re-reads nothing and its result.usage is running totals: neither the stored context
        nor the live one may move (to 0, or to the totals marked context_total)."""
        sessions = self.root / "sessions"
        sessions.mkdir()
        gem = self.root / "gem"
        tx = gem / "C--x" / "s-gem.jsonl"
        tx.parent.mkdir(parents=True)
        host = gemini_host.GeminiHost(name="gem", cwd=str(self.root), transcript_path=tx, sessions_dir=sessions,
                                      session_id="s-gem", in_memory_uds=True, quiet=True, system_prompt="Be a lead.",
                                      model="gemini-4-carbon")
        self.addCleanup(host.close)
        totals = {"input_tokens": 10000, "cache_read_tokens": 20000}
        turns = [
            [{"type": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "DONE",
                                                     "usage": dict(totals, output_tokens=100)}},
             {"type": "result", "result": {"status": "SUCCESS", "response": "done",
                                           "usage": dict(totals, output_tokens=100)}}],
            [{"type": "step_update", "step_update": {"step_index": 2, "step_type": "checkpoint", "state": "DONE",
                                                     "usage": {"output_tokens": 784}}},
             {"type": "result", "result": {"status": "SUCCESS", "response": "",
                                           "usage": dict(totals, output_tokens=884)}}],
        ]
        host.enqueue_prompt("build it")
        host._on_peer_message({"channel": "__almanac_control__", "body": json.dumps({"op": "compact"})})
        for out in turns:
            item = host.turns.get_nowait()
            proc = mock.MagicMock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in out)))
            with mock.patch.object(host, "_ensure_stream_proc", return_value=proc):
                host.run_one_turn(item)
        meta = fl.read_tail(tx)[1]
        self.assertEqual((meta["context"], meta["context_total"]), (30000, False))
        store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(store.close)
        store.ingest(gem, provider="gemini")
        self.assertEqual(store.session_context("s-gem"), 30000)


class CharterTests(Fixture):
    def charter(self, policy, squad="sq-1"):
        return spawner.build_mesh_charter(agent_name="a", provider="claude", role_title="Implementer", role_prompt="Do.",
                                          almanac_url="http://x", squad_id=squad, context_policy=policy)

    def test_one_line_for_a_policy_that_is_not_none(self):
        self.assertIn("your session ends when your task is accepted", self.charter("fresh-per-task"))
        self.assertIn("task log", self.charter("fresh-per-task"))
        self.assertIn("above 300,000 tokens", self.charter("compact-at-300000"))
        self.assertNotIn("Context policy", self.charter("none"))
        self.assertNotIn("Context policy", self.charter(None))
        self.assertNotIn("Context policy", self.charter("fresh-per-task", squad=None), "no squad, no tasks")
