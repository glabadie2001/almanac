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
