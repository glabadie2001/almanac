"""Stalled agents (2026-11-11: impl-plan stopped on "API Error: Connection lost mid-response" and sat idle until
the operator typed continue). Detection from the transcript tail; recover.py's tries, delays and one escalation."""
import json
import time

from support import Fixture

from almanac import fleet as fl, recover
from almanac.store import Store

T0 = 1_800_000_000.0


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def api_error(ts, error="server_error"):
    return {"type": "assistant", "isApiErrorMessage": True, "error": error, "isSidechain": False, "timestamp": iso(ts),
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: Connection lost mid-response."}]}}


def said(ts, text="working"):
    return {"type": "assistant", "timestamp": iso(ts), "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}}


def prompt(ts, text="continue"):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text}}


class DetectTests(Fixture):
    def tail(self, *records):
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return fl.read_tail(path)

    def test_an_api_error_last_is_a_stall_and_an_error_event(self):
        events, meta = self.tail(prompt(T0, "build it"), said(T0 + 1), api_error(T0 + 2))
        self.assertEqual((meta["stall"]["error"], meta["stall"]["ts"]), ("server_error", iso(T0 + 2)))
        self.assertIn("Connection lost", meta["stall"]["text"])
        self.assertEqual(events[-1]["kind"], "error")
        self.assertEqual(meta["model"], "claude-opus-5-5", "the synthetic record does not change the model")

    def test_only_assistant_output_ends_a_stall(self):
        _, meta = self.tail(api_error(T0), prompt(T0 + 20))
        self.assertIsNotNone(meta["stall"], "our own `continue` must not end it, or every try would start over")
        _, meta = self.tail(api_error(T0), prompt(T0 + 20), said(T0 + 25))
        self.assertIsNone(meta["stall"])

    def test_an_unbroken_run_of_errors_keeps_its_start(self):
        _, meta = self.tail(api_error(T0), prompt(T0 + 20), api_error(T0 + 25))
        self.assertEqual((meta["stall"]["since"], meta["stall"]["ts"]), (iso(T0), iso(T0 + 25)))
        _, meta = self.tail(api_error(T0), said(T0 + 10), api_error(T0 + 25))
        self.assertEqual(meta["stall"]["since"], iso(T0 + 25), "a reply in between starts a new stall")

    def test_a_sidechain_error_is_not_a_stall(self):
        _, meta = self.tail(said(T0), dict(api_error(T0 + 1), isSidechain=True))
        self.assertIsNone(meta["stall"])


class FakeFleet:
    def __init__(self):
        self.rows, self.stalls, self.sent = [], {}, []

    def view(self, sid):
        return None, [], dict(stall=self.stalls.get(sid))

    def send(self, pid, created, text, **kw):
        self.sent.append((pid, text, kw.get("from_name")))
        return True, "sent via uds-messaging", {"pid": pid}


class FakeService:
    def __init__(self, store):
        self.store, self.fleet, self.hub_peer, self.auto_continue = store, FakeFleet(), None, True
        self.live = {}                                    # handle -> its stall (None: live and well)

    def fleet_now(self):
        return dict(processes=self.fleet.rows)

    def mesh_peers(self, squad_id=None):
        rows = {r["session_id"]: r for r in self.fleet.rows}
        return [dict(rows[f"s-{n}"], name=n, stall=s) for n, s in self.live.items()]


class RecoverTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        self.store.create_squad("sq-1", "alpha", str(self.root), "Ship it.")
        self.svc = FakeService(self.store)
        self.now = T0
        self.rec = recover.Recover(clock=lambda: self.now)
        self.member("arch", 100, None, "architect-claude")
        self.member("impl", 200, "arch", "implementer-claude")
        self.svc.live = {"arch": None, "impl": None}

    def member(self, name, pid, by, role, squad="sq-1"):
        self.store.record_spawn(agent_name=name, session_id=f"s-{name}", squad_id=squad, role_id=role, provider="claude",
                                model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=pid, spawned_by=by)
        self.svc.fleet.rows.append(dict(pid=pid, created="1", role="session", session_id=f"s-{name}", status="idle"))

    def stall(self, name, error="server_error", at=None):
        self.svc.fleet.stalls[f"s-{name}"] = dict(error=error, text="API Error: Connection lost", ts=iso(at or self.now))

    def at(self, ts):
        self.now = ts
        return self.rec.tick(self.svc)

    def tries(self):
        return [pid for pid, text, who in self.svc.fleet.sent if text == "continue" and who == "almanac-hub"]

    @property
    def told(self):
        """Who heard an escalation, in order: the journal, and for an agent also the wire."""
        rows = [m for m in reversed(self.store.messages(limit=100)) if m["body"].startswith("[stalled]")]
        self.assertTrue(all(m["transport"] == "system" and m["from_name"] == "almanac-hub" for m in rows))
        wire = [pid for pid, text, _ in self.svc.fleet.sent if text.startswith("[stalled]")]
        pids = {f"s-{n}": r["pid"] for r in self.svc.fleet.rows for n in [r["session_id"][2:]]}
        self.assertEqual(wire, [pids[m["to_session"]] for m in rows if m["to_name"] != "operator"])
        return sorted(m["to_name"] for m in rows)        # one pass journals them in the same millisecond

    def test_three_tries_at_the_delays_then_one_escalation(self):
        self.stall("impl", at=T0)
        for ts, expect in ((T0 + 19, 0), (T0 + 20, 1), (T0 + 79, 1), (T0 + 80, 2), (T0 + 259, 2), (T0 + 260, 3)):
            self.at(ts)
            self.assertEqual(len(self.tries()), expect, ts - T0)
        self.at(T0 + 279)
        self.assertEqual(self.told, [], "the third try gets its grace")
        self.at(T0 + 280)
        self.assertEqual(self.told, ["arch", "operator"])
        last = self.store.messages(limit=1)[0]["body"]
        self.assertIn("impl", last)
        self.assertIn("server_error", last)
        self.at(T0 + 5000)
        self.assertEqual((len(self.tries()), len(self.told)), (3, 2), "once per stall")
        actions = [a["action"] for a in self.store.actions(20)]
        self.assertEqual(actions.count("continue"), 3)
        self.assertEqual(actions.count("escalate"), 1)

    def test_a_class_that_is_not_transient_escalates_at_once(self):
        self.stall("impl", error="authentication_failed")
        self.at(T0)
        self.at(T0 + 1000)
        self.assertEqual(self.tries(), [])
        self.assertEqual(self.told, ["arch", "operator"])
        self.stall("arch", error="something_new")
        self.at(T0 + 1001)
        self.assertEqual(self.tries(), [], "an unknown class is not retried")

    def test_a_recovered_agent_starts_over(self):
        self.stall("impl", at=T0)
        self.at(T0 + 20)
        self.assertEqual(len(self.tries()), 1)
        del self.svc.fleet.stalls["s-impl"]              # it said something: working again
        self.at(T0 + 30)
        self.stall("impl", at=T0 + 500)
        self.at(T0 + 519)
        self.assertEqual(len(self.tries()), 1)
        self.at(T0 + 520)
        self.assertEqual(len(self.tries()), 2, "a new stall gets its own three tries from its own start")

    def test_a_busy_session_is_left_alone(self):
        self.stall("impl", at=T0)
        self.svc.fleet.rows[1]["status"] = "busy"
        self.at(T0 + 500)
        self.assertEqual(self.tries(), [])

    def test_a_session_almanac_did_not_spawn_into_a_squad_is_never_sent_to(self):
        self.svc.fleet.rows.append(dict(pid=300, created="1", role="session", session_id="s-mine", status="idle"))
        self.svc.fleet.stalls["s-mine"] = dict(error="server_error", text="x", ts=iso(T0))
        self.store.record_spawn(agent_name="solo", session_id="s-solo", squad_id=None, role_id=None, provider="claude",
                                model=None, effort=None, cwd=".", worktree_path=None, pid=400)
        self.svc.fleet.rows.append(dict(pid=400, created="1", role="session", session_id="s-solo", status="idle"))
        self.svc.fleet.stalls["s-solo"] = dict(error="server_error", text="x", ts=iso(T0))
        for ts in (T0 + 20, T0 + 100, T0 + 400, T0 + 900):
            self.at(ts)
        self.assertEqual((self.tries(), self.told), ([], []))

    def test_a_squad_that_is_not_active_is_left_alone(self):
        self.store.db.execute("update squads set status = 'ended' where id = 'sq-1'")
        self.stall("impl", at=T0)
        self.at(T0 + 500)
        self.assertEqual(self.tries(), [])

    def test_the_switch_off_sends_nothing(self):
        self.svc.auto_continue = False
        self.stall("impl", at=T0)
        self.stall("arch", error="authentication_failed")
        for ts in (T0 + 20, T0 + 100, T0 + 400, T0 + 900):
            self.at(ts)
        self.assertEqual((self.tries(), self.told), ([], []))

    def test_a_stalled_architect_is_continued_and_escalates_to_the_operator_only(self):
        self.stall("arch", at=T0)
        for ts in (T0 + 20, T0 + 80, T0 + 260):
            self.at(ts)
        self.assertEqual(self.tries(), [100, 100, 100])
        self.at(T0 + 280)
        self.assertEqual(self.told, ["operator"], "an architect has no spawned_by")

    def test_the_operator_hears_when_the_adder_has_ended_or_is_stalled(self):
        self.svc.live = {"impl": None}                   # arch has ended
        self.stall("impl", error="authentication_failed")
        self.at(T0)
        self.assertEqual(self.told, ["operator"])
        self.member("impl2", 500, "arch", "implementer-claude")
        self.svc.live = {"arch": {"error": "server_error"}, "impl2": None}     # arch is live but stalled itself
        self.stall("impl2", error="authentication_failed")
        self.at(T0 + 1)
        self.assertEqual(self.told, ["operator", "operator"])

    def test_recovery_closes_no_open_flag(self):
        asked = self.store.record_message(from_name="impl", to_name="operator", squad_id="sq-1", body="Which schema?",
                                          ts=T0 - 10, needs="blocking")
        broadcast = self.store.record_message(from_name="impl", to_name="#squad", channel="#squad", squad_id="sq-1",
                                              body="Anyone own door.py?", ts=T0 - 5, needs="reply")
        self.stall("impl", error="authentication_failed")          # escalates at once
        self.at(T0)
        self.stall("arch", at=T0)
        self.at(T0 + 20)                                            # a `continue` to arch
        late = self.store.record_message(from_name="arch", to_name="operator", squad_id="sq-1", body="Go?",
                                         ts=T0 + 1, needs="reply")  # journaled after the continue it predates
        still_open = {m["id"] for m in self.store.open_needs()}
        self.assertTrue({asked["id"], broadcast["id"], late["id"]} <= still_open)

    def test_the_door_hook_never_raises(self):
        class Broken(FakeService):
            def fleet_now(self):
                raise RuntimeError("scan failed")
        self.assertEqual(recover.tick(Broken(self.store)), [])

    def test_a_new_stall_inside_one_tick_starts_over_and_closes_the_old_alert(self):
        self.stall("impl", error="authentication_failed")
        self.at(T0)                                                  # escalated
        self.assertEqual(len(self.store.open_needs(to_name="operator")), 1)
        # between two ticks it worked again (a reply), then failed anew: fleet gives a new `since`
        self.svc.fleet.stalls["s-impl"] = dict(error="server_error", text="x", ts=iso(T0 + 300), since=iso(T0 + 300))
        self.at(T0 + 310)
        self.assertEqual(self.store.open_needs(to_name="operator"), [], "the old stall's alert is closed")
        self.at(T0 + 320)
        self.assertEqual(len(self.tries()), 1, "the new stall gets its own tries")

    def test_a_continue_that_fails_again_is_the_same_stall(self):
        self.svc.fleet.stalls["s-impl"] = dict(error="server_error", text="x", ts=iso(T0), since=iso(T0))
        self.at(T0 + 20)
        self.svc.fleet.stalls["s-impl"] = dict(error="server_error", text="x", ts=iso(T0 + 25), since=iso(T0))
        for ts in (T0 + 80, T0 + 260, T0 + 280, T0 + 900):
            self.at(ts)
        self.assertEqual((len(self.tries()), self.told), (3, ["arch", "operator"]), "no fresh tries, no loop")

    def test_every_open_escalation_of_an_agent_closes_on_recovery(self):
        for _ in range(2):
            self.store.record_message(from_name="almanac-hub", to_name="operator", squad_id="sq-1", ts=T0,
                                      body="[stalled] impl (squad sq-1) ...", transport="system", needs="blocking", closes=False)
        self.at(T0 + 1)
        self.assertEqual(self.store.open_needs(to_name="operator"), [])

    def test_a_live_session_is_found_by_session_however_old_its_spawn(self):
        rows = {f"s-{n}": self.store.spawn_of(n) for n in ("arch", "impl")}
        scan = mock.patch.object(self.store, "spawned", side_effect=AssertionError("no scan of the newest 500"))
        lookup = mock.patch.object(self.store, "spawn_by_session", create=True, side_effect=rows.get)
        with scan, lookup:
            self.stall("impl", at=T0)
            self.at(T0 + 20)
        self.assertEqual(len(self.tries()), 1)

    def test_an_open_escalation_survives_a_restart_and_closes_on_recovery(self):
        self.stall("impl", error="authentication_failed")
        self.at(T0)
        self.assertEqual(self.told, ["arch", "operator"])
        self.rec = recover.Recover(clock=lambda: self.now)          # Almanac restarted: no memory of the stall
        for ts in (T0 + 20, T0 + 100, T0 + 400):
            self.at(ts)
        self.assertEqual((self.tries(), self.told), ([], ["arch", "operator"]), "not tried or escalated again")
        self.assertEqual(len(self.store.open_needs(to_name="operator")), 1)
        del self.svc.fleet.stalls["s-impl"]                          # it works again
        self.assertEqual([d["action"] for d in self.at(T0 + 500)], ["resolved"])
        self.assertEqual(self.store.open_needs(to_name="operator"), [])


from support import TOKEN                                            # noqa: E402
from test_server_fixes import MeshRig                                # noqa: E402


class OperatorSeesItTests(MeshRig):
    """The escalation is in GET /api/operator/inbox and raises the alert a blocking question raises."""

    def test_an_escalation_raises_the_blocking_alert(self):
        self.store.create_squad("sq-1", "alpha", str(self.root), "Ship it.")
        self.store.record_spawn(agent_name="impl", session_id="s-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=900)
        svc = FakeService(self.store)
        svc.fleet.rows.append(dict(pid=900, created="1", role="session", session_id="s-impl", status="idle"))
        svc.fleet.stalls["s-impl"] = dict(error="authentication_failed", text="Not logged in", ts=iso(T0))
        rec = recover.Recover(clock=lambda: T0)
        rec.tick(svc)
        status, _, doc = self.request(self.handler, "/api/operator/inbox", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual((doc["open"]["blocking"], doc["unread"]), (1, 1))
        self.assertIsNotNone(doc["oldest_blocking_ts"], "the header's '▲ blocked on operator' age")
        [m] = [m for m in doc["messages"] if m["body"].startswith("[stalled] impl")]
        self.assertEqual((m["from_name"], m["to_name"], m["needs"], m["squad_id"]), ("almanac-hub", "operator", "blocking", "sq-1"))
        _, _, mesh = self.request(self.handler, "/api/mesh?squad=sq-1", token=TOKEN)
        self.assertIn(m["id"], [o["id"] for o in mesh["open"]], "the squad strip's 'blocked on operator'")
        del svc.fleet.stalls["s-impl"]
        rec.tick(svc)
        _, _, doc = self.request(self.handler, "/api/operator/inbox?limit=0", token=TOKEN)
        self.assertEqual(doc["open"]["blocking"], 0, "recovered: the alert clears itself")

    def test_a_continue_is_not_the_operator_writing(self):
        self.store.create_squad("sq-1", "alpha", str(self.root), "Ship it.")
        self.store.record_spawn(agent_name="impl", session_id="s-impl", squad_id="sq-1", role_id="implementer-claude",
                                provider="claude", model=None, effort=None, cwd=str(self.root), worktree_path=None, pid=900)
        self.assertFalse(self.service.may_page_operator("impl")[0])
        svc = FakeService(self.store)
        svc.fleet.rows.append(dict(pid=900, created="1", role="session", session_id="s-impl", status="idle"))
        svc.fleet.stalls["s-impl"] = dict(error="server_error", text="x", ts=iso(T0))
        recover.Recover(clock=lambda: T0 + 20).tick(svc)
        self.assertEqual(len(svc.fleet.sent), 1)
        self.assertFalse(self.service.may_page_operator("impl")[0], "no paging pass from an automatic continue")
        _, _, doc = self.request(self.handler, "/api/operator/inbox", token=TOKEN)
        self.assertEqual([m for m in doc["messages"] if m["body"] == "continue"], [], "not the operator's outgoing mail")
        self.assertEqual(self.store.message_graph("sq-1"), [], "no operator edge in the topology")
        self.assertEqual(self.store.timeline_messages(squad_id="sq-1", start=0, end=T0 + 100), [], "nor in the swimlane")


import io                                                            # noqa: E402
import os                                                            # noqa: E402
from unittest import mock                                            # noqa: E402

from almanac import gemini_host, uds                                 # noqa: E402
from almanac.fleet import Fleet                                      # noqa: E402
from test_fleet import FakeMachine                                   # noqa: E402


class GeminiTests(Fixture):
    def host(self):
        self.sessions = self.root / "claude" / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.tx = self.root / "gem.jsonl"
        h = gemini_host.GeminiHost(name="gem", cwd=str(self.root), transcript_path=self.tx, sessions_dir=self.sessions,
                                   session_id="s-gem", agy_bin="agy", in_memory_uds=True, quiet=True)
        self.addCleanup(h.close)
        return h

    def turn(self, h, *events):
        proc = mock.MagicMock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in events)))
        with mock.patch.object(h, "_ensure_stream_proc", return_value=proc):
            h.run_one_turn(dict(raw_content="go", prompt_text="go"))
        return fl.read_tail(self.tx)

    def test_an_error_result_is_marked_like_claudes(self):
        h = self.host()
        events, meta = self.turn(h, {"type": "result", "is_error": True,
                                     "error": {"code": "RESOURCE_EXHAUSTED", "message": "quota"}})
        self.assertEqual((meta["stall"]["error"], meta["stall"]["text"]), ("RESOURCE_EXHAUSTED", "[error] quota"))
        self.assertEqual(events[-1]["kind"], "error")
        _, meta = self.turn(h, {"type": "result", "is_error": True, "error": "something broke"})
        self.assertEqual(meta["stall"]["error"], "gemini_error")
        _, meta = self.turn(h, {"type": "result", "result": {"response": "done"}})
        self.assertIsNone(meta["stall"], "a good turn ends it")

    def test_a_runner_that_died_mid_turn_is_its_own_class(self):
        h = self.host()
        _, meta = self.turn(h, {"type": "init", "model": "gemini-4-carbon"})          # no result: stdout closed
        self.assertEqual(meta["stall"]["error"], gemini_host.RUNNER_EXITED)
        self.assertNotIn(gemini_host.RUNNER_EXITED, recover.TRANSIENT)
        self.assertNotIn("gemini_error", recover.TRANSIENT)

    def test_a_runner_that_cannot_start_leaves_the_same_error_record(self):
        h = self.host()
        with mock.patch.object(h, "_ensure_stream_proc", side_effect=FileNotFoundError("agy")):
            h.run_one_turn(dict(raw_content="go", prompt_text="go"))
        events, meta = fl.read_tail(self.tx)
        self.assertEqual(meta["stall"]["error"], gemini_host.RUNNER_ERROR)
        self.assertIn("Gemini runner not found", meta["stall"]["text"])
        self.assertEqual(events[-1]["kind"], "error")
        self.assertNotIn(gemini_host.RUNNER_ERROR, recover.TRANSIENT)

    def test_a_cancelled_turn_writes_no_completed_reply(self):
        h = self.host()
        _, before = self.turn(h, {"type": "result", "is_error": True, "error": "boom"})
        h.stop_event.set()                                       # shutting down: agy's output ends with no result
        events, meta = self.turn(h)
        self.assertEqual(meta["stall"], before["stall"], "the earlier stall is not cleared by a fake reply")
        self.assertNotIn("(completed)", [e["text"] for e in events])

    def test_continue_reaches_a_gemini_agent_through_fleet_send(self):
        h = self.host()
        h.peer.start()
        machine = FakeMachine()
        machine.procs[os.getpid()] = dict(ppid=1, exe="agy.exe", path=r"C:\x\agy.exe", cmd="agy.exe",
                                          created=int(h.peer.proc_start), cpu=0, mem=1 << 20)
        fleet = Fleet(self.root / "claude", table=machine.table, facts=machine.facts, lines=machine.lines)
        row = next(r for r in fleet.snapshot()["processes"] if r.get("session_id") == "s-gem")
        self.assertEqual((row["provider"], row["has_uds"]), ("gemini", True))
        ok, message, _ = fleet.send(row["pid"], row["created"], recover.TEXT, from_name=recover.HUB)
        self.assertTrue(ok, message)
        item = h.turns.get(timeout=2)
        parsed = uds.parse_cross_session(item["raw_content"])
        self.assertEqual((parsed["from_name"], parsed["body"].strip()), ("almanac-hub", "continue"))
