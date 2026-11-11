"""Slice W1: turns read from transcripts, work / wait / idle per agent, exact overlap between agents, the buckets the
heatmap draws, calls.model, and GET /api/worktime."""
import time

from support import TOKEN, Fixture, assistant, result, tool_use, write_jsonl

from almanac import store as st, uds, web
from almanac.door import Service, make_handler
from almanac.fleet import Fleet
from almanac.store import Store

BASE = 1_794_000_000.0


def iso(sec):
    """BASE + sec as the transcripts write it (milliseconds, Z)."""
    whole = int(BASE + sec)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{int(round((BASE + sec - whole) * 1000)):03d}Z"


def prompt(session, sec, text):
    return {"type": "user", "sessionId": session, "timestamp": iso(sec), "cwd": "c", "message": {"content": text}}


def peer(session, sec, sender, body, needs=None):
    wire = uds.format_cross_session(uds.needs_marker(needs, sender) + body, from_pipe="p", from_name=sender)
    return prompt(session, sec, wire)


def said(session, sec, mid, text="ok", model="claude-test", sidechain=False):
    rec = assistant(session, iso(sec), mid, [{"type": "text", "text": text}])
    rec["message"]["model"] = model
    rec["isSidechain"] = sidechain
    return rec


def used(session, sec, mid, use_id, tool="Bash", model="claude-test", **inp):
    rec = assistant(session, iso(sec), mid, [tool_use(use_id, tool, **inp)])
    rec["message"]["model"] = model
    return rec


class TurnTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        for name, sid in (("agent-a", "s1"), ("agent-b", "s2")):
            self.store.record_spawn(agent_name=name, session_id=sid, squad_id="sq", role_id=None, provider="claude",
                                    model=None, effort=None, cwd=".", worktree_path=None, pid=1)
        write_jsonl(self.projects / "p" / "s1.jsonl", [
            prompt("s1", 0, "fix the bug"),
            used("s1", 5, "m1", "u1", command="pytest"),
            result("s1", iso(65), "u1", "60 seconds of tests"),          # a long tool result still counts as the turn
            said("s1", 70, "m2", "done"),
            said("s1", 100, "m3", "subagent chatter", sidechain=True),   # a subagent's record never opens or stretches a turn
            peer("s1", 200, "agent-b", "Which schema version?", needs="blocking"),
            said("s1", 210, "m4", "v6"),
        ])
        write_jsonl(self.projects / "p" / "s2.jsonl", [
            said("s2", 0, "n1", "resumed mid-turn"),                      # no prompt first: an 'other' turn opens
            said("s2", 30, "n2", "still going"),
            peer("s2", 250, "agent-a", "v6, answered"),
        ])
        self.store.ingest(self.projects)

    def turns(self, sid):
        return [(round(a - BASE, 3), round(b - BASE, 3), k) for a, b, k in self.store.db.execute(
            "select start_ts, end_ts, kind from turns where session = ? order by start_ts", (sid,))]

    def test_turns_follow_prompts_and_ignore_subagents(self):
        self.assertEqual(self.turns("s1"), [(0, 70, "prompt"), (200, 210, "peer")])
        self.assertEqual(self.turns("s2"), [(0, 30, "other"), (250, 250, "peer")])

    def test_work_idle_and_counts(self):
        doc = self.store.worktime(["s1"])
        [a] = doc["agents"]
        self.assertEqual((a["work_s"], a["wait_s"], a["turns"], a["longest_turn_s"]), (80.0, 0.0, 2, 70.0))
        self.assertEqual((a["wall_s"], a["idle_s"], a["calls"], a["output_tokens"]), (210.0, 130.0, 1, 80))
        self.assertEqual(doc["range"]["bucket_s"], 10)
        self.assertEqual(sum(a["work"]), 80.0)
        self.assertEqual(sum(a["events"]), 5, "one tool call and four assistant messages (the subagent's too)")

    def test_a_second_full_read_changes_nothing(self):
        flag = next(m for m in self.store.messages() if m["needs"] == "blocking")
        self.assertIsNotNone(flag["answered_ts"], "agent-a's reply in s2 answered agent-b's blocking question")
        count = lambda: [self.store.db.execute(f"select count(*) from {t}").fetchone()[0]   # noqa: E731
                         for t in ("turns", "calls", "usage", "messages")]
        before, turns = count(), self.turns("s1")
        self.store.db.execute("delete from files")
        self.store.db.commit()
        self.store.ingest(self.projects)
        self.assertEqual((count(), self.turns("s1")), (before, turns))
        again = self.store.message(flag["id"])
        self.assertEqual((again["answered_ts"], again["answered_by"]), (flag["answered_ts"], flag["answered_by"]))

    def test_open_until_stretches_a_busy_session(self):
        [a] = self.store.worktime(["s1"], open_until={"s1": BASE + 300})["agents"]
        self.assertEqual(a["work_s"], 70.0 + 100.0)


class WaitAndModelTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        write_jsonl(self.projects / "p" / "s3.jsonl", [
            prompt("s3", 0, "review when ready"),
            used("s3", 10, "w1", "u1", tool="mcp__almanac-mesh__mesh_wait", model="claude-a"),
            result("s3", iso(100), "u1", "a message arrived"),
            used("s3", 105, "w2", "u2", model="claude-b", command="git diff"),     # the session switched model
            result("s3", iso(108), "u2", "diff"),
            said("s3", 110, "w3", "LGTM", model="claude-b"),
        ])
        self.store.ingest(self.projects)

    def test_mesh_wait_is_waiting_not_work(self):
        [a] = self.store.worktime(["s3"])["agents"]
        self.assertEqual((a["work_s"], a["wait_s"]), (20.0, 90.0))
        u = self.store.session_usage("s3")
        self.assertEqual((u["work_s"], u["wait_s"], u["turns"]), (20.0, 90.0, 1))

    def test_each_call_carries_the_model_that_issued_it_and_a_reread_backfills(self):
        models = lambda: dict(self.store.db.execute("select tool_use_id, model from calls"))   # noqa: E731
        self.assertEqual(models(), {"u1": "claude-a", "u2": "claude-b"})
        self.store.db.execute("update calls set model = null")          # as rows ingested before v6 look
        self.store.db.execute("delete from files")
        self.store.db.commit()
        self.store.ingest(self.projects)
        self.assertEqual(models(), {"u1": "claude-a", "u2": "claude-b"})


class IntervalTests(Fixture):
    def test_merge_subtract_intersect(self):
        self.assertEqual(st.merge_intervals([(5, 9), (0, 3), (3, 4), (7, 12), (8, 8), (9, 2)]), [(0, 4), (5, 12)])
        self.assertEqual(st.subtract_intervals([(0, 10)], [(2, 3), (5, 20)]), [(0, 2), (3, 5)])
        self.assertEqual(st.intersect_intervals([(0, 10), (20, 30)], [(5, 25)]), [(5, 10), (20, 25)])

    def test_bucket_edges(self):
        self.assertEqual(st.bucket_seconds([(5, 25)], 0, 10, 3), [5, 10, 5], "straddles three buckets")
        self.assertEqual(st.bucket_seconds([(7, 7), (9, 3)], 0, 10, 3), [0, 0, 0], "zero-length and backwards")
        self.assertEqual(st.bucket_seconds([(-10, 2), (25, 40)], 0, 10, 3), [2, 0, 5], "cut at both ends")
        self.assertEqual((st.default_bucket(2400), st.default_bucket(3600), st.default_bucket(10 ** 7)), (10, 30, 3600))
        self.assertTrue(all(st.is_wait_tool(t) for t in ("mesh_wait", "mcp__almanac-mesh__mesh_wait", "almanac-mesh_mesh_wait")))
        self.assertFalse(st.is_wait_tool("mesh_waiter"))

    def test_three_agents_overlap_exactly(self):
        got = st.concurrency({"A": [(0, 10)], "B": [(5, 15)], "C": [(8, 12)]}, start=0, end=20)
        self.assertEqual(got["by_count"], {0: 5.0, 1: 8.0, 2: 5.0, 3: 2.0})
        self.assertEqual(got["solo_s"], {"A": 5.0, "B": 3.0, "C": 0.0})
        self.assertEqual({(p["a"], p["b"]): p["overlap_s"] for p in got["pairs"]}, {("A", "B"): 5.0, ("A", "C"): 2.0, ("B", "C"): 4.0})
        self.assertEqual((got["peak"], got["any_s"], got["parallel_s"]), (3, 15.0, 7.0))

    def test_touching_intervals_do_not_overlap(self):
        got = st.concurrency({"A": [(0, 5)], "B": [(5, 10)]})
        self.assertEqual((got["peak"], got["pairs"], got["by_count"]), (1, [], {1: 10.0}))

    def test_store_buckets_the_overlap(self):
        s = Store(self.root / "o.db")
        self.addCleanup(s.close)
        for sid, a, b in (("A", 0, 10), ("B", 5, 15), ("C", 8, 12)):
            s.db.execute("insert into turns values (?, ?, ?, 'prompt')", (sid, a, b))
        s.db.commit()
        ov = s.worktime(["A", "B", "C"], start=0, end=20, bucket_s=5)["overlap"]
        self.assertEqual(ov["max_working"], [1, 3, 2, 0])
        self.assertEqual(ov["working"], [1.0, 2.4, 1.4, 0.0])
        self.assertEqual(ov["by_count"][0], 5.0)

    def test_nothing_to_show(self):
        s = Store(self.root / "e.db")
        self.addCleanup(s.close)
        self.assertEqual(s.worktime([])["agents"], [])
        doc = s.worktime(["nobody"])
        self.assertEqual((doc["range"]["n"], doc["agents"][0]["work_s"], doc["overlap"]), (0, 0.0, None))


class RouteTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "a.db")
        self.addCleanup(self.store.close)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        fleet = Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {})
        self.service = Service(self.store, self.projects, 300, fleet=fleet)
        self.handler = make_handler(self.service, TOKEN)
        squad = self.store.create_squad("sq-1", "alpha", ".", "brief")
        self.store.db.execute("update squads set created_ts = ?", (BASE,))
        for name, sid, role in (("arch", "s1", "architect-claude"), ("impl", "s2", "implementer-claude")):
            self.store.record_spawn(agent_name=name, session_id=sid, squad_id=squad["id"], role_id=role,
                                    provider="claude", model="opus", effort=None, cwd=".", worktree_path=None, pid=1)
        for sid, a, b in (("s1", 0, 600), ("s2", 300, 900)):
            self.store.db.execute("insert into turns values (?, ?, ?, 'prompt')", (sid, BASE + a, BASE + b))
        self.store.db.commit()

    def test_a_squad_is_labelled_and_overlap_reported(self):
        status, _, doc = self.request(self.handler, "/api/worktime?squad=sq-1&bucket=60")
        self.assertEqual(status, 200)
        self.assertEqual([(a["agent_name"], a["role_id"], a["live"], a["status"]) for a in doc["agents"]],
                         [("arch", "architect-claude", False, "ended"), ("impl", "implementer-claude", False, "ended")])
        self.assertEqual((doc["range"]["start"], doc["range"]["bucket_s"], doc["range"]["n"]), (BASE, 60, 15))
        self.assertEqual((doc["overlap"]["parallel_s"], doc["overlap"]["peak"]), (300.0, 2))
        self.assertEqual(doc["overlap"]["pairs"], [dict(a="s1", b="s2", overlap_s=300.0)])

    def test_sessions_unknown_squad_bad_bucket_and_agent_tokens(self):
        _, _, doc = self.request(self.handler, "/api/worktime?sessions=s2")
        self.assertEqual([a["work_s"] for a in doc["agents"]], [600.0])
        self.assertEqual(self.request(self.handler, "/api/worktime?squad=nope")[0], 404)
        self.assertEqual(self.request(self.handler, "/api/worktime?squad=sq-1&bucket=0")[0], 400)
        self.assertEqual(self.request(self.handler, "/api/worktime?squad=sq-1", token=web.agent_token(TOKEN, "arch"))[0], 403)


class SliverTests(Fixture):
    def test_a_millisecond_overlap_sets_no_peak_and_lists_no_pair(self):
        got = st.concurrency({"A": [(0, 10)], "B": [(5, 15)], "C": [(7, 7.001)]}, start=0, end=15)
        self.assertEqual((got["peak"], [(p["a"], p["b"]) for p in got["pairs"]]), (2, [("A", "B")]))
        self.assertNotIn(3, got["by_count"])
