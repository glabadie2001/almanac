"""Slice 6: the report by provider and model, and the tool error rate."""
import time
from datetime import datetime, timezone

from support import TOKEN, Fixture, assistant, tool_use, write_jsonl

from almanac.door import Service, make_handler
from almanac.store import Store, model_table


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def reply(session, ts, msg_id, blocks, model, output=20):
    rec = assistant(session, iso(ts), msg_id, blocks, usage={"input_tokens": 10, "cache_creation_input_tokens": 0,
                                                             "cache_read_input_tokens": 90, "output_tokens": output})
    rec["message"]["model"] = model
    return rec


def outcome(session, ts, use_id, *, error=False, text="ok"):
    return {"type": "user", "sessionId": session, "timestamp": iso(ts),
            "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "is_error": error,
                                     "content": [{"type": "text", "text": text}]}]}}


class ModelReportTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(self.store.close)
        self.now = time.time()
        self.t0 = self.now - 3600

    def ingest(self, name, records, provider="claude"):
        root = self.root / f"tx-{provider}"
        write_jsonl(root / "D---proj" / f"{name}.jsonl", records)
        self.store.ingest(root, provider=provider)

    def by_model(self, report):
        return {(m["kind"], m["provider"], m["model"]): m for m in report["models"]}

    def two_providers_and_legacy(self):
        t = self.t0
        self.ingest("c1", [
            reply("c1", t, "m1", [tool_use("u1", "Bash", command="rg -n foo .")], "claude-opus-5-5", output=100),
            outcome("c1", t + 1, "u1"),
            reply("c1", t + 2, "m2", [tool_use("u2", "Read", file_path="a.py")], "claude-opus-5-5", output=100),
            outcome("c1", t + 3, "u2", error=True),
        ])
        self.ingest("g1", [
            reply("g1", t + 10, "g-m1", [tool_use("gu1", "run_command", command="ls")], "gemini-4-pro", output=30),
            outcome("g1", t + 11, "gu1"),
        ], provider="gemini")
        seed = self.root / "tooluses.jsonl"
        write_jsonl(seed, [{"tool": "Bash", "cmd": "curl http://127.0.0.1:1/x", "proj": "D---old", "ts": iso(t + 20)}])
        self.assertEqual(self.store.seed_legacy(seed), 1)

    def test_provider_filter_keeps_only_that_provider_and_drops_legacy(self):
        self.two_providers_and_legacy()
        everything = self.store.report(days=7, now=self.now)
        self.assertEqual(sum(s["calls"] for s in everything["series"]), 4)
        rows = self.by_model(everything)
        self.assertEqual(rows[("legacy", None, None)], dict(kind="legacy", provider=None, model=None, calls=1))
        self.assertEqual(everything["models"][-1]["kind"], "legacy", "legacy sits at the bottom")
        self.assertEqual([p["provider"] for p in everything["providers"]], ["claude", "gemini"], "sorted by output")

        claude = self.store.report(days=7, now=self.now, provider="claude")
        self.assertEqual(claude["range"]["provider"], "claude")
        self.assertEqual(sum(s["calls"] for s in claude["series"]), 2)
        self.assertEqual(sum(s["output_tokens"] for s in claude["series"]), 200)
        self.assertEqual(list(self.by_model(claude)), [("model", "claude", "claude-opus-5-5")])
        self.assertEqual([p["provider"] for p in claude["providers"]], ["claude"])
        self.assertEqual(claude["coverage"], everything["coverage"], "coverage is the whole database either way")

        gemini = self.store.report(days=7, now=self.now, provider="gemini")
        self.assertEqual(sum(s["calls"] for s in gemini["series"]), 1)
        self.assertEqual(sum(s["legacy"] for s in gemini["series"]), 0)
        g = self.by_model(gemini)[("model", "gemini", "gemini-4-pro")]
        self.assertEqual((g["sessions"], g["messages"], g["output_tokens"], g["calls"]), (1, 1, 30, 1))
        self.assertEqual(g["error_rate"], 0.0)

        with self.assertRaises(ValueError):
            self.store.report(days=7, now=self.now, provider="openai")

    def test_a_session_that_switches_model_splits_between_both_rows(self):
        t = self.t0
        self.ingest("s", [
            reply("s", t, "m1", [tool_use("u1", "Bash", command="rg -n a .")], "claude-sonnet-5-5", output=20),
            outcome("s", t + 1, "u1", text="x" * 2000),
            reply("s", t + 2, "m2", [tool_use("u2", "Bash", command="rg -n b .")], "claude-opus-5-5", output=50),
            outcome("s", t + 3, "u2", error=True, text="x" * 4000),
            reply("s", t + 4, "m3", [tool_use("u3", "Read", file_path="b.py")], "claude-opus-5-5", output=50),
            reply("s", t + 5, "m4", [tool_use("u4", "Read", file_path="c.py")], "<synthetic>", output=0),
        ])
        r = self.store.report(days=7, now=self.now)
        self.assertEqual([(m["kind"], m["model"]) for m in r["models"]],
                         [("model", "claude-opus-5-5"), ("model", "claude-sonnet-5-5"), ("unattributed", None)])
        opus, sonnet, nobody = r["models"]
        self.assertEqual((opus["sessions"], opus["messages"], opus["output_tokens"], opus["calls"]), (1, 2, 100, 2))
        self.assertEqual((sonnet["sessions"], sonnet["messages"], sonnet["output_tokens"], sonnet["calls"]), (1, 1, 20, 1))
        self.assertEqual((opus["context_tokens"], opus["ctx_per_out"]), (200, 2.0))
        self.assertEqual((sonnet["kb_per_call"], opus["kb_per_call"]), (2.0, 4.0), "u3 has no result: not in KB/call")
        self.assertEqual((opus["tool_errors"], opus["error_rate"]), (1, 1.0), "u3 is still waiting: not a success")
        self.assertEqual(sonnet["error_rate"], 0.0)
        self.assertEqual((nobody["calls"], nobody["provider"]), (1, None))
        self.assertEqual(opus["previous"], dict(ctx_per_out=None, kb_per_call=None, error_rate=None))
        self.assertEqual(r["providers"][0]["calls"], 4)
        self.assertEqual(r["providers"][0]["sessions"], 1)

    def test_error_rate_counts_only_calls_whose_result_arrived(self):
        t = self.t0
        old = self.now - 10 * 86400
        self.ingest("s", [
            reply("s", old, "o1", [tool_use("o1u", "Read", file_path="a")], "claude-opus-5-5"),
            outcome("s", old + 1, "o1u", error=True),
            reply("s", old + 2, "o2", [tool_use("o2u", "Read", file_path="b")], "claude-opus-5-5"),
            outcome("s", old + 3, "o2u", error=True),
            reply("s", t, "m1", [tool_use("u1", "Read", file_path="a")], "claude-opus-5-5"),
            outcome("s", t + 1, "u1", error=True),
            reply("s", t + 2, "m2", [tool_use("u2", "Read", file_path="b")], "claude-opus-5-5"),
            outcome("s", t + 3, "u2"),
            reply("s", t + 4, "m3", [tool_use("u3", "Read", file_path="c")], "claude-opus-5-5"),
        ])
        r = self.store.report(days=7, now=self.now)
        tile = {x["id"]: x for x in r["tiles"]}["error_rate"]
        self.assertEqual((tile["value"], tile["previous"], tile["unit"], tile["better"]), (0.5, 1.0, "%", "down"))
        self.assertEqual(tile["label"], "Tool error rate")
        busy = [s for s in r["series"] if s["calls"]]
        self.assertEqual(sum(s["errors"] for s in busy), 1)
        self.assertEqual([s["error_rate"] for s in busy if s["errors"]], [0.5])
        self.assertTrue(all(s["error_rate"] is None for s in r["series"] if not s["calls"]), "an empty bucket is null")
        row = r["models"][0]
        self.assertEqual((row["error_rate"], row["previous"]["error_rate"]), (0.5, 1.0))

    def test_no_results_gives_null_not_zero(self):
        self.ingest("s", [
            reply("s", self.t0, "m1", [tool_use("u1", "Read", file_path="a")], "claude-opus-5-5"),
            reply("s", self.t0 + 1, "m2", [tool_use("u2", "Read", file_path="b")], "claude-opus-5-5"),
        ])
        r = self.store.report(days=7, now=self.now)
        self.assertIsNone({x["id"]: x for x in r["tiles"]}["error_rate"]["value"])
        self.assertIsNone(r["models"][0]["error_rate"])
        self.assertEqual(r["models"][0]["tool_errors"], 0)
        self.assertIsNone(r["providers"][0]["error_rate"])
        self.assertEqual(model_table([], [], [], []), [])


class ModelReportDoorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "almanac.db")
        self.addCleanup(self.store.close)
        self.handler = make_handler(Service(self.store, self.projects, 300), TOKEN)

    def test_provider_parameter(self):
        status, _, doc = self.request(self.handler, "/api/report?provider=bogus")
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/report?days=7&provider=gemini")
        self.assertEqual((status, doc["range"]["provider"], doc["models"]), (200, "gemini", []))
        status, _, doc = self.request(self.handler, "/api/report?days=7")
        self.assertEqual((status, doc["range"]["provider"]), (200, None))
        self.assertIn("error_rate", [t["id"] for t in doc["tiles"]])
