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
