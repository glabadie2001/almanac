"""Gemini usage is agy's running totals per conversation (they carry on across a relaunch with --conversation);
ingest stores each turn's own counts, and a one-time re-read repairs what was stored as totals."""
import time

from support import Fixture, assistant, write_jsonl

from almanac.store import Store
from test_models_report import iso

NOW = time.time()


def turn(session, i, inp, out, cache_read=0, provider="gemini"):
    rec = assistant(session, iso(NOW - 3600 + i), f"{session}-m{i}", [{"type": "text", "text": "ok"}],
                    usage={"input_tokens": inp, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cache_read,
                           "output_tokens": out})
    rec["message"]["model"] = "gemini-4-carbon" if provider == "gemini" else "claude-opus-5-5"
    rec["provider"] = provider
    return rec


# agy's totals as measured on 2026-11-11 (turn three came after a kill and a relaunch with --conversation)
MEASURED = [(10439, 28, 0), (12598, 56, 8385), (14884, 84, 16880)]


class GeminiUsageTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.store = Store(self.state / "almanac.db")
        self.addCleanup(lambda: self.store.close())     # whichever store the test ends with
        self.gem = self.state / "gemini" / "projects"
        self.path = self.gem / "C--x" / "g1.jsonl"

    def write(self, rows, session="g1", path=None, provider="gemini"):
        write_jsonl(path or self.path, [turn(session, i, *r, provider=provider) for i, r in enumerate(rows)])

    def rows(self, session="g1"):
        return self.store.db.execute("select input, output, cache_read from usage where session = ? order by ts",
                                     (session,)).fetchall()

    def test_running_totals_become_turns_that_sum_to_the_last_total(self):
        self.write(MEASURED)
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.rows(), [(10439, 28, 0), (2159, 28, 8385), (2286, 28, 8495)])
        u = self.store.session_usage("g1")
        self.assertEqual((u["input"], u["output"], u["cache_read"]), MEASURED[-1])

    def test_turns_read_in_later_passes_use_the_stored_totals(self):
        self.write(MEASURED[:2])
        self.store.ingest(self.gem, provider="gemini")
        self.write(MEASURED)                              # the file grew by a turn
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.rows()[-1], (2286, 28, 8495))

    def test_a_file_read_again_from_the_top_starts_over(self):
        self.write(MEASURED)
        self.store.ingest(self.gem, provider="gemini")
        self.store.db.execute("delete from files")        # e.g. a forced re-read
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.rows(), [(10439, 28, 0), (2159, 28, 8385), (2286, 28, 8495)], "not subtracted twice")

    def test_a_drop_is_a_reset_taken_as_it_is(self):
        self.write([(1000, 10, 0), (1500, 20, 0), (300, 5, 0), (700, 9, 0)])
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.rows(), [(1000, 10, 0), (500, 10, 0), (300, 5, 0), (400, 4, 0)])

    def test_a_failed_turn_with_zero_usage_is_not_a_reset(self):
        self.write([MEASURED[0], (0, 0, 0), MEASURED[1]])           # agy's error result carries no usage
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.rows(), [(10439, 28, 0), (0, 0, 0), (2159, 28, 8385)])
        self.assertEqual(self.store.session_usage("g1")["input"], MEASURED[1][0], "not counted twice")
        [(context,)] = self.store.db.execute("select context from sessions where id = 'g1'").fetchall()
        self.assertEqual(context, 2159 + 8385, "the last real turn's, not zeroed by the failed one")

    def test_a_claude_transcript_is_untouched(self):
        claude = self.root / "claude" / "projects"
        self.write([(100, 10, 0), (150, 20, 0)], session="c1", path=claude / "D--x" / "c1.jsonl", provider="claude")
        self.store.ingest(claude, provider="claude")
        self.assertEqual(self.rows("c1"), [(100, 10, 0), (150, 20, 0)], "Claude usage is per message already")

    def test_history_stored_as_totals_is_repaired_once_and_by_model_reads_true(self):
        self.write(MEASURED)
        self.store.ingest(self.gem, provider="gemini")
        # what the old ingest left: the totals themselves, and no repair flag
        for (inp, out, cr), (mid,) in zip(MEASURED, self.store.db.execute(
                "select message_id from usage where session = 'g1' order by ts").fetchall()):
            self.store.db.execute("update usage set input = ?, output = ?, cache_read = ? where message_id = ?",
                                  (inp, out, cr, mid))
        self.store.db.execute("delete from meta where key = 'gemini_usage_deltas' or key like 'gemini_usage:%'")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.state / "almanac.db")      # the upgrade
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(sum(r[0] for r in self.rows()), MEASURED[-1][0])
        [model] = [m for m in self.store.report(days=1, now=NOW)["models"] if m.get("model") == "gemini-4-carbon"]
        self.assertEqual(model["output_tokens"], MEASURED[-1][1])
        self.store.close()
        self.store = Store(self.state / "almanac.db")      # and only once
        self.assertTrue(self.store.db.execute("select count(*) from files").fetchone()[0])

    def test_ingest_stores_the_step_figure_as_the_sessions_context(self):
        own = turn("g1", 1, 13302, 84, 8263)
        own["context_tokens"] = 11248
        write_jsonl(self.path, [own])
        self.store.ingest(self.gem, provider="gemini")
        self.assertEqual(self.store.session_context("g1"), 11248)
        self.assertEqual(self.rows(), [(13302, 84, 8263)])          # usage keeps the turn's counts


class GeminiStepContextTests(Fixture):
    """A step's own re-read is `input_tokens + cache_read_tokens` of its step_update usage; result.usage is the total."""
    STEP1 = {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "DONE", "usage": {
        "input_tokens": 10317, "output_tokens": 24, "thinking_tokens": 19, "cache_read_tokens": 0, "total_tokens": 10341}}}
    STEP3 = {"event": "step_update", "step_update": {"step_index": 3, "step_type": "agent_response", "state": "DONE", "usage": {
        "input_tokens": 2985, "output_tokens": 784, "thinking_tokens": 27, "cache_read_tokens": 8263, "total_tokens": 3769}}}
    RESULT = {"event": "result", "result": {"usage": {"input_tokens": 13302, "cache_read_tokens": 8263}}}

    def test_a_step_is_its_own_input_plus_cache_read(self):
        from almanac.gemini_host import step_context
        self.assertEqual([step_context(e) for e in (self.STEP1, self.STEP3)], [10317, 11248])
        self.assertIsNone(step_context(self.RESULT))                      # the running total is not a step
        self.assertIsNone(step_context({"event": "step_update", "step_update": {"step_type": "tool"}}))

    def test_read_tail_prefers_the_step_figure_and_marks_a_total_only_when_it_is_all_there_is(self):
        from almanac import fleet as fl
        path = self.root / "g.jsonl"
        own = turn("g1", 1, 13302, 84, 8263)
        own["context_tokens"] = 11248
        write_jsonl(path, [own])
        meta = fl.read_tail(path)[1]
        self.assertEqual((meta["context"], meta["context_total"]), (11248, False))
        write_jsonl(path, [turn("g1", 1, 13302, 84, 8263)])               # an old transcript: the total, marked
        meta = fl.read_tail(path)[1]
        self.assertEqual((meta["context"], meta["context_total"]), (21565, True))
