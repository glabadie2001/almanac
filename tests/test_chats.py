import json
from pathlib import Path
import os
import time
from datetime import datetime, timezone
from unittest import mock

from support import TOKEN, Fixture, assistant, result, tool_use, write_jsonl

from almanac import archive, resume
from almanac.door import Service, make_handler
from almanac.store import Store

SID = "0b6f1e2a-1111-4222-8333-444455556666"
OTHER = "9c9c9c9c-1111-4222-8333-444455556666"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def chat_records(sid, cwd, t0, title=None):
    recs = [{"type": "user", "sessionId": sid, "timestamp": iso(t0), "cwd": cwd, "message": {"content": "build the archive"}}]
    a = assistant(sid, iso(t0 + 1), f"m-{sid[:4]}", [{"type": "text", "text": "on it"}, tool_use(f"u-{sid[:4]}", "Bash", command="rg x")],
                  usage={"input_tokens": 5, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 99_000, "output_tokens": 50})
    a["cwd"], a["effort"] = cwd, "high"
    a["message"]["model"] = "claude-opus-5-5"
    recs += [a, dict(result(sid, iso(t0 + 2), f"u-{sid[:4]}", "hit"), cwd=cwd)]
    if title:
        recs.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
    return recs


class ArchiveTests(Fixture):
    def test_sync_copies_appends_keeps_prev_and_never_deletes(self):
        src, dst = self.projects, self.root / "archive"
        main = src / "D--x" / f"{SID}.jsonl"
        write_jsonl(main, [{"a": 1}])
        (src / "D--x" / SID / "tool-results").mkdir(parents=True)
        (src / "D--x" / SID / "tool-results" / "big.txt").write_text("v1", encoding="utf-8")
        self.assertEqual(archive.sync(src, dst)["new"], 2)
        self.assertEqual(archive.sync(src, dst)["bytes"], 0, "nothing changed, nothing copied")
        before = main.stat().st_size
        write_jsonl(main, [{"b": 2}], mode="a")
        stats = archive.sync(src, dst)
        self.assertEqual((stats["appended"], stats["bytes"]), (1, main.stat().st_size - before), "only the new bytes")
        self.assertEqual((dst / "D--x" / f"{SID}.jsonl").read_text(encoding="utf-8"), main.read_text(encoding="utf-8"))
        (src / "D--x" / SID / "tool-results" / "big.txt").write_text("v2!", encoding="utf-8")
        self.assertEqual(archive.sync(src, dst)["replaced"], 1)
        self.assertEqual((dst / "D--x" / SID / "tool-results" / "big.txt.prev").read_text(encoding="utf-8"), "v1")
        main.unlink()
        archive.sync(src, dst)
        self.assertTrue((dst / "D--x" / f"{SID}.jsonl").exists(), "a pruned transcript stays in the archive")
        out = archive.restore(SID, dst, src)
        self.assertEqual(sorted(out["restored"]), [f"D--x/{SID}.jsonl"], "only what is missing or shorter comes back")
        self.assertEqual(main.read_text(encoding="utf-8").count("\n"), 2)
        with self.assertRaises(FileNotFoundError):
            archive.restore(OTHER, dst, src)


class ResumeTests(Fixture):
    def test_command_settings_and_estimate(self):
        settings = resume.settings_file(self.root)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"cleanupPeriodDays": 3650})
        argv = resume.command("claude.exe", SID, settings, 'Big "title" <x>', True)
        self.assertEqual(argv[:5], ["claude.exe", "--settings", str(settings), "--resume", SID])
        self.assertEqual(argv[5:], ["--remote-control", "Big title x"])
        self.assertNotIn("--remote-control", resume.command("claude.exe", SID, settings, "t", False))
        with self.assertRaises(ValueError):
            resume.command("claude.exe", "../../etc", settings, "t", True)
        prices = resume.Prices({"claude-opus-5-5": 4.0})
        self.assertEqual(prices.estimate("claude-opus-5-5", 250_000), 1.0)
        self.assertIsNone(prices.estimate("claude-mythos-5-1", 250_000), "no price is unpriced, never $0")
        self.assertIsNone(resume.Prices().estimate("claude-opus-5-5", 1000))


class ChatRouteTests(Fixture):
    def setUp(self):
        super().setUp()
        now = time.time()
        self.cwd = str(self.root)
        write_jsonl(self.projects / "D--x" / f"{SID}.jsonl", chat_records(SID, self.cwd, now - 7200, title="Archive every chat"))
        write_jsonl(self.projects / "D--x" / f"{OTHER}.jsonl", chat_records(OTHER, self.cwd, now - 3600))
        self.store = Store(self.root / "state" / "almanac.db")
        self.addCleanup(self.store.close)
        prices = resume.Prices({"claude-opus-5-5": 4.0})
        self.service = Service(self.store, self.projects, 300, fleet=None, state=os.path.relpath(self.root / "state"), claude="claude.exe", prices=prices)
        self.service.ingest()
        self.handler = make_handler(self.service, TOKEN)

    def test_list_and_read(self):
        status, _, doc = self.request(self.handler, "/api/chats")
        self.assertEqual(status, 200)
        rows = {c["id"]: c for c in doc["chats"]}
        self.assertEqual([c["id"] for c in doc["chats"]], [OTHER, SID], "newest first")
        c = rows[SID]
        self.assertEqual((c["title"], c["cwd"], c["model"], c["context"], c["replies"]), ("Archive every chat", self.cwd, "claude-opus-5-5", 100_005, 1))
        self.assertEqual(rows[OTHER]["title"], "build the archive", "no AI title: the first prompt stands in")
        self.assertEqual((c["archived"], c["on_disk"], c["resumable"], c["resume_usd"]), (True, True, True, 0.4))
        self.assertEqual(doc["archive"]["new"], 2)
        status, _, doc = self.request(self.handler, "/api/chats?q=Archive%20every")
        self.assertEqual([c["id"] for c in doc["chats"]], [SID])
        status, _, doc = self.request(self.handler, f"/api/chat?id={SID}")
        self.assertEqual((status, doc["read_from"], doc["chat"]["effort"]), (200, "transcript", "high"))
        self.assertEqual([e["kind"] for e in doc["events"]], ["you", "claude", "tool", "result"])
        (self.projects / "D--x" / f"{SID}.jsonl").unlink()            # Claude Code prunes it
        status, _, doc = self.request(self.handler, f"/api/chat?id={SID}")
        self.assertEqual((doc["read_from"], doc["chat"]["on_disk"]), ("archive", False))
        self.assertEqual(self.request(self.handler, "/api/chat?id=nope")[0], 404)

    def test_resume_restores_then_launches_once(self):
        (self.projects / "D--x" / f"{SID}.jsonl").unlink()
        with mock.patch.object(resume.subprocess, "Popen") as popen, mock.patch.object(resume, "WATCH", 0.05):
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": SID, "remote": True})
        self.assertEqual((status, doc["ok"], doc["pid"], doc["action"]), (200, True, 4242, "resume"))
        self.assertTrue((self.projects / "D--x" / f"{SID}.jsonl").exists(), "the pruned transcript came back first")
        self.assertIn("restored 1 file", doc["detail"])
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertEqual((argv[5], kwargs["cwd"]), ("--remote-control", self.cwd))
        self.assertEqual(self.service.launched, {SID: 4242})
        self.assertTrue(popen.call_args[1]["startupinfo"] is not None, "a hidden service must hand Claude a fresh console")
        self.assertIn("--debug-file", argv)
        self.assertTrue(Path(argv[2]).is_absolute() and Path(argv[argv.index("--debug-file") + 1]).is_absolute(),
                        "Claude runs in the chat's folder: every path handed to it must be absolute")
        self.assertNotIn("CLAUDECODE", popen.call_args[1]["env"], "a resumed Claude must not think it is nested")
        with mock.patch.object(resume.subprocess, "Popen") as again:
            status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": SID})
        self.assertEqual((status, again.call_count), (409, 0), "never a second copy while the first runs")
        status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": "not-an-id"})
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": "12345678-1111-4222-8333-444455556666"})
        self.assertEqual((status, doc["error"]), (409, "Almanac has never seen that chat"))


class DuplicateTests(ChatRouteTests):
    def test_a_launched_resume_counts_as_live_before_its_record_exists(self):
        from test_fleet import FakeMachine
        from almanac.fleet import Fleet
        machine = FakeMachine()
        self.service.fleet = Fleet(self.root / "claude", table=machine.table, facts=machine.facts, lines=machine.lines)
        (self.root / "claude" / "sessions").mkdir(parents=True)
        self.service.launched[SID] = 100           # pid 100 is alive in the fake table, with no record for SID
        status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": SID})
        self.assertEqual((status, doc["error"]), (409, "already running as pid 100; open it in the fleet"))
        del machine.procs[100]
        self.service.fleet.last["at"] = 0
        status, _, doc = self.request(self.handler, "/api/chats")
        self.assertNotIn(SID, self.service.launched, "forgotten once its process is gone")


class LaunchDiesTests(ChatRouteTests):
    def test_a_resume_that_dies_at_once_is_a_failure_with_its_reason(self):
        log = self.root / "state" / "resume-logs" / f"{SID}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("2026 [DEBUG] fine\n2026 [ERROR] No conversation found with session ID x\n", encoding="utf-8")
        with mock.patch.object(resume.subprocess, "Popen") as popen, mock.patch.object(resume, "WATCH", 0.5), \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "KEEP": "y"}):
            popen.return_value.pid = 77
            popen.return_value.poll.return_value = 1
            status, _, doc = self.request(self.handler, "/api/resume", method="POST", body={"id": SID})
        self.assertEqual((status, doc["ok"]), (409, False))
        self.assertIn("exited at once (code 1): No conversation found", doc["error"])
        env = popen.call_args[1]["env"]
        self.assertEqual((env.get("KEEP"), "CLAUDECODE" in env, "CLAUDE_CODE_ENTRYPOINT" in env), ("y", False, False))
        self.assertNotIn(SID, self.service.launched)
