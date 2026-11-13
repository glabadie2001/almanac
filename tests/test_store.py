import json
import time
from datetime import datetime, timezone

from support import Fixture, assistant, result, tool_use, write_jsonl

from almanac.store import IMAGE_CHARS, Store


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class StoreTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(self.store.close)
        self.now = time.time()
        self.t0 = self.now - 3600
        self.path = self.projects / "D---proj" / "s1.jsonl"
        write_jsonl(self.path, [
            assistant("s1", iso(self.t0), "m1", [tool_use("u1", "Bash", command="grep -rn foo src")]),
            result("s1", iso(self.t0 + 2), "u1", "x" * 3000),
            assistant("s1", iso(self.t0 + 3), "m2", [tool_use("u2", "Read", file_path="a.png")]),
            result("s1", iso(self.t0 + 4), "u2", "", image=True),
        ])

    def calls(self):
        return {r[0]: r[1:] for r in self.store.db.execute(
            "select tool_use_id, tool, family, search, out_chars, seconds, source from calls")}

    def test_ingest_pairs_results_and_keeps_usage(self):
        stats = self.store.ingest(self.projects)
        self.assertEqual((stats["files"], stats["read"], stats["calls"], stats["results"]), (1, 1, 2, 2))
        calls = self.calls()
        self.assertEqual(calls["u1"][:4], ("Bash", "search", 1, 3000))
        self.assertAlmostEqual(calls["u1"][4], 2, places=3)
        self.assertEqual(calls["u2"][3], IMAGE_CHARS, "a screenshot counts as a fixed size, not its base64")
        self.assertEqual(self.store.db.execute("select count(*), sum(output) from usage").fetchone(), (2, 40))
        self.assertEqual(self.store.db.execute("select project from sessions").fetchone()[0], "D---proj")

    def test_incremental_reread_and_truncation_are_idempotent(self):
        self.store.ingest(self.projects)
        self.assertEqual(self.store.ingest(self.projects)["read"], 0, "unchanged file is skipped")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant("s1", iso(self.t0 + 10), "m3", [tool_use("u3", "Bash", command="rg foo .")])) + "\n")
            fh.write('{"type": "user", "partial line without newline')
        stats = self.store.ingest(self.projects)
        self.assertEqual((stats["read"], stats["calls"]), (1, 1), "only the new complete line is read")
        self.assertEqual(self.calls()["u3"][2], 2)
        write_jsonl(self.path, [assistant("s1", iso(self.t0), "m1", [tool_use("u1", "Bash", command="grep -rn foo src")])])
        self.store.ingest(self.projects)
        self.assertEqual(len(self.calls()), 3, "a shrunken file is read again without duplicating rows")

    def test_legacy_rows_fill_gaps_and_step_aside_for_live_ones(self):
        seed = self.root / "tooluses.jsonl"
        gone = self.now - 20 * 86400
        write_jsonl(seed, [
            {"tool": "Bash", "cmd": "grep -rn foo src", "proj": "D---proj", "ts": iso(self.t0)},   # same call as u1
            {"tool": "Bash", "cmd": "curl http://127.0.0.1:8781/x", "proj": "D---old", "ts": iso(gone)},
        ])
        self.assertEqual(self.store.seed_legacy(seed), 2)
        self.store.ingest(self.projects)
        rows = self.store.db.execute("select source, family from calls order by ts").fetchall()
        self.assertEqual(rows, [("legacy", "http"), ("live", "search"), ("live", None)])
        self.assertEqual(self.store.seed_legacy(seed), 0, "seeding twice adds nothing")

    def test_report_shape_and_tiles(self):
        self.store.ingest(self.projects)
        r = self.store.report(days=7, bucket="day", now=self.now)
        self.assertEqual(len(r["series"]), 8)
        today = r["series"][-1] if r["series"][-1]["calls"] else r["series"][-2]
        self.assertEqual((today["calls"], today["shell_calls"], today["families"]), (2, 1, {"search": 1}))
        self.assertEqual(today["adoption"]["search"], 0.0)
        tiles = {t["id"]: t for t in r["tiles"]}
        self.assertEqual(tiles["kb_per_call"]["value"], 4.5)
        self.assertEqual(tiles["ctx_per_out"]["value"], 50.0)
        self.assertEqual(tiles["adoption"]["value"], 0.0)
        self.assertIsNone(tiles["kb_per_call"]["previous"])
        self.assertEqual(r["families"][0]["family"], "search")
        self.assertEqual(self.store.report(days=0, bucket="week", now=self.now)["coverage"]["calls"], 2)
        with self.assertRaises(ValueError):
            self.store.report(bucket="month")

    def test_missing_folder_is_reported_not_raised(self):
        self.assertIn("not a folder", self.store.ingest(self.root / "nope")["error"])


class BriefUpgradeTests(Fixture):
    """T-29: a stored brief equal to any earlier shipped text ends at the current one; an edited brief stays."""
    CASES = (("reviewer-claude", "REVIEWER", "PREV_REVIEWERS"),
             ("implementer-claude", "IMPLEMENTER", "PREV_IMPLEMENTERS"),
             ("architect-claude", "ARCHITECT", None),
             ("orchestrator-claude", "ORCHESTRATOR", None))

    def reopen(self, role_id, text):
        path = self.root / "state" / "almanac.db"
        s = Store(path)
        s.db.execute("update roles set system_prompt = ? where id = ?", (text, role_id))
        s.db.commit()
        s.close()
        again = Store(path)
        self.addCleanup(again.close)
        return again.role(role_id)["system_prompt"]

    def test_every_earlier_text_is_upgraded_and_an_edited_one_kept(self):
        from almanac import store as st
        for rid, cur, prevs in self.CASES:
            current = getattr(st, cur)
            earlier = getattr(st, prevs) if prevs else (getattr(st, "PREV_" + cur),)
            for i, prev in enumerate(earlier):
                with self.subTest(role=rid, earlier=i):
                    self.assertNotEqual(prev, current)
                    self.assertEqual(self.reopen(rid, prev), current)
            with self.subTest(role=rid, edited=True):
                self.assertEqual(self.reopen(rid, "mine"), "mine")

    def test_the_briefs_route_findings_by_rating(self):
        from almanac import store as st
        for text in (st.REVIEWER, st.IMPLEMENTER, st.ARCHITECT, st.ORCHESTRATOR):
            self.assertIn("low", text)
        self.assertNotIn("architect", st.REVIEWER)
