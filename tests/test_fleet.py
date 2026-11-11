import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import Fixture, assistant, result, tool_use, write_jsonl

from almanac import fleet as fl
from almanac.fleet import Fleet, classify, tail_transcript

CLI = r"C:\Users\G\.local\bin\claude.exe"
APP = r"C:\Program Files\WindowsApps\Claude_4.1_x64__abc\app\claude.exe"
FT0 = 134380000000000000


class ClassifyTests(Fixture):
    def test_roles(self):
        self.assertEqual(classify(CLI, f'"{CLI}"', None), "session")
        self.assertEqual(classify(CLI, f"{CLI} --remote-control the-realm", None), "session")
        self.assertEqual(classify(CLI, f"{CLI} --resume C:\\x.jsonl", None), "session")
        self.assertEqual(classify(CLI, f"{CLI} -p --output-format stream-json", None), "headless")
        self.assertEqual(classify(CLI, f'{CLI} daemon run --origin transient', None), "daemon")
        self.assertEqual(classify(CLI, f"{CLI} --bg-pty-host \\\\.\\pipe\\x 199 46 -- {CLI}", None), "pty-host")
        self.assertEqual(classify(CLI, f"{CLI} --version", None), "other")
        self.assertEqual(classify(CLI, f"{CLI} --version", {"pid": 1}), "session", "a session record wins")
        self.assertEqual(classify(APP, f'"{APP}"', None), "desktop")
        self.assertEqual(classify(APP, f'"{APP}" --type=renderer --user-data-dir=x', None), "desktop-helper")


class TailTests(Fixture):
    def test_tail_reads_the_conversation(self):
        path = self.projects / "p" / "s.jsonl"
        write_jsonl(path, [
            {"type": "user", "timestamp": "t0", "message": {"content": "fix the build please"}},
            {"type": "user", "timestamp": "t0", "message": {"content": "<command-name>/clear</command-name>"}},
            assistant("s", "t1", "m1", [{"type": "text", "text": "Looking."}, tool_use("u1", "Bash", command="npm run build", description="Build it")]),
            result("s", "t2", "u1", "ok   built"),
            {"type": "user", "isSidechain": True, "timestamp": "t3", "message": {"content": "subagent noise"}},
            {"type": "user", "timestamp": "t4", "message": {"content": [{"type": "tool_result", "tool_use_id": "u9", "is_error": True, "content": "boom"}]}},
        ])
        events = tail_transcript(path)
        self.assertEqual([(e["kind"], e["text"]) for e in events], [
            ("you", "fix the build please"), ("claude", "Looking."), ("tool", "Bash: Build it"),
            ("result", "ok built"), ("error", "boom")])
        self.assertEqual(len(tail_transcript(path, limit=2)), 2)
        self.assertEqual(tail_transcript(self.projects / "missing.jsonl"), [])

    def test_meta_context_is_the_newest_main_step_reread(self):
        path = self.projects / "p" / "c.jsonl"
        side = assistant("s", "t3", "m3", [{"type": "text", "text": "side"}], usage={"input_tokens": 9_000_000})
        side["isSidechain"] = True
        err = assistant("s", "t4", "m4", [{"type": "text", "text": "x"}], usage={"input_tokens": 7})
        err["message"]["model"] = "<synthetic>"
        write_jsonl(path, [assistant("s", "t1", "m1", [{"type": "text", "text": "a"}]),
                           assistant("s", "t2", "m2", [{"type": "text", "text": "b"}], usage={"input_tokens": 5, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 99}),
                           side, err])
        self.assertEqual(fl.read_tail(path)[1]["context"], 115)
        self.assertIsNone(fl.read_tail(self.projects / "none.jsonl")[1]["context"])


class FakeMachine:
    """A process table the Fleet can scan: two sessions, a daemon, the desktop app and a helper."""

    def __init__(self):
        self.procs = {
            100: dict(ppid=1, exe="claude.exe", path=CLI, cmd=f'"{CLI}"', created=FT0 + 1, cpu=0, mem=400 << 20),
            200: dict(ppid=1, exe="claude.exe", path=CLI, cmd=f"{CLI} --remote-control bank", created=FT0 + 2, cpu=0, mem=300 << 20),
            300: dict(ppid=1, exe="claude.exe", path=CLI, cmd=f"{CLI} daemon run", created=FT0 + 3, cpu=0, mem=100 << 20),
            400: dict(ppid=1, exe="claude.exe", path=APP, cmd=f'"{APP}"', created=FT0 + 4, cpu=0, mem=200 << 20),
            401: dict(ppid=400, exe="claude.exe", path=APP, cmd=f'"{APP}" --type=gpu-process', created=FT0 + 5, cpu=0, mem=50 << 20),
            999: dict(ppid=1, exe="notepad.exe", path="n", cmd="n", created=FT0, cpu=0, mem=0),
        }
        self.asked = []

    def table(self):
        return [dict(pid=pid, ppid=p["ppid"], exe=p["exe"]) for pid, p in self.procs.items()] + [dict(pid=1, ppid=0, exe="explorer.exe")]

    def facts(self, pid):
        p = self.procs.get(pid)
        return dict(created=p["created"], cpu=p["cpu"], mem=p["mem"], path=p["path"]) if p else None

    def lines(self, pids):
        self.asked.append(list(pids))
        return {pid: self.procs[pid]["cmd"] for pid in pids if pid in self.procs}


class FleetTests(Fixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "claude"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "sessions" / "100.json").write_text(json.dumps({
            "pid": 100, "sessionId": "sess-a", "cwd": "D:\\x\\realm", "status": "busy", "name": "realm-1", "kind": "interactive",
            "version": "2.1.319", "bridgeSessionId": "session_01ABC", "updatedAt": 1794000000000}), encoding="utf-8")
        write_jsonl(self.home / "projects" / "D--x-realm" / "sess-a.jsonl", [
            {"type": "user", "timestamp": "t", "message": {"content": "hello"}},
            dict(assistant("sess-a", "t1", "m1", [{"type": "text", "text": "hi"}]), effort="high"),
            {"type": "assistant", "isSidechain": True, "timestamp": "t2", "effort": "low",
             "message": {"id": "m9", "model": "claude-haiku-4-5", "content": []}},
        ])
        self.machine = FakeMachine()
        self.fleet = Fleet(self.home, table=self.machine.table, facts=self.machine.facts, lines=self.machine.lines)

    def test_snapshot_joins_records_folds_helpers_and_caches_command_lines(self):
        snap = self.fleet.snapshot()
        rows = {r["pid"]: r for r in snap["processes"]}
        self.assertNotIn(999, rows)
        self.assertEqual(rows[100]["role"], "session")
        self.assertEqual((rows[100]["name"], rows[100]["status"], rows[100]["session_id"]), ("realm-1", "busy", "sess-a"))
        self.assertEqual(rows[100]["remote_url"], "https://claude.ai/code/session_01ABC")
        self.assertEqual((rows[100]["model"], rows[100]["effort"]), ("claude-test", "high"), "the main thread's latest, not a subagent's")
        self.assertIsNone(rows[300]["model"])
        self.assertTrue(rows[100]["can_send"] and rows[100]["can_kill"])
        self.assertEqual((rows[200]["role"], rows[200]["status"]), ("session", None), "a session with no record yet")
        self.assertEqual((rows[300]["role"], rows[300]["can_send"]), ("daemon", False))
        self.assertEqual((rows[400]["role"], rows[400]["helpers"]), ("desktop", 1))
        self.assertEqual((rows[401]["role"], rows[401]["can_kill"]), ("desktop-helper", False))
        self.assertEqual(snap["summary"]["sessions"], 2)
        self.assertEqual(snap["summary"]["busy"], 1)
        self.assertEqual(snap["processes"][0]["role"], "session", "sessions sort first")
        self.fleet.snapshot()
        self.assertEqual(len(self.machine.asked[-1]), 0, "command lines are fetched once per process")

    def test_a_row_is_stalled_only_while_idle_on_an_api_error(self):
        path = self.home / "projects" / "D--x-realm" / "sess-a.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "server_error", "timestamp": "t3",
                                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: x"}]}}) + "\n")
        row = next(r for r in self.fleet.snapshot()["processes"] if r["pid"] == 100)
        self.assertIsNone(row["stall"], "busy: a try may be in flight")
        rec = self.home / "sessions" / "100.json"
        rec.write_text(rec.read_text(encoding="utf-8").replace('"busy"', '"idle"'), encoding="utf-8")
        row = next(r for r in self.fleet.snapshot()["processes"] if r["pid"] == 100)
        self.assertEqual((row["stall"]["error"], row["stall"]["ts"]), ("server_error", "t3"))
        self.assertEqual(self.fleet.detail(100)["events"][-1]["kind"], "error")

    def test_cpu_comes_from_process_time_deltas(self):
        with mock.patch.object(fl.time, "time", side_effect=[1000.0, 1002.0]):
            self.fleet.snapshot()
            self.machine.procs[100]["cpu"] = int(1e7 * fl.CPUS)       # one second per CPU over two seconds
            snap = self.fleet.snapshot()
        self.assertEqual(next(r for r in snap["processes"] if r["pid"] == 100)["cpu"], 50.0)

    def test_detail_reads_the_transcript(self):
        self.fleet.snapshot()
        d = self.fleet.detail(100)
        self.assertTrue(d["transcript"].endswith("sess-a.jsonl"))
        self.assertEqual(d["events"], [dict(ts="t", kind="you", text="hello"), dict(ts="t1", kind="claude", text="hi")])
        self.assertEqual(d["meta"], dict(model="claude-test", effort="high", last_ts="t1", stall=None, context=1000))
        self.assertEqual(len(self.fleet.detail(100, limit=1)["events"]), 1)
        path = Path(d["transcript"])
        before = self.fleet.tails[path][0]
        self.fleet.detail(100)
        self.assertIs(self.fleet.tails[path][0], before, "an unchanged transcript is not parsed again")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(assistant("sess-a", "t3", "m2", [{"type": "text", "text": "more"}]), effort="max")) + "\n")
        self.assertEqual(self.fleet.detail(100)["meta"]["effort"], "max")
        self.assertIsNone(self.fleet.detail(12345))

    def test_kill_and_send_refuse_reused_pids_and_wrong_roles(self):
        self.fleet.snapshot()
        created = str(FT0 + 1)
        ok, msg, _ = self.fleet.kill(100, str(FT0 + 99))
        self.assertFalse(ok)
        self.assertIn("different process", msg)
        ok, msg, _ = self.fleet.send(300, str(FT0 + 3), "hi")
        self.assertFalse(ok)
        self.assertIn("daemon", msg)
        ok, msg, _ = self.fleet.kill(401, str(FT0 + 5))
        self.assertFalse(ok)
        self.assertIn("parent", msg)
        with mock.patch.object(fl.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"SUCCESS: killed", stderr=b"")) as run:
            ok, msg, row = self.fleet.kill(100, created)
        self.assertEqual((ok, msg, row["name"]), (True, "SUCCESS: killed", "realm-1"))
        self.assertEqual(run.call_args[0][0], ["taskkill", "/PID", "100", "/T", "/F"])
        with mock.patch.object(fl.console, "send", return_value=(True, "typed 2 characters and Enter")) as send:
            ok, msg, _ = self.fleet.send(100, created, "hi")
        self.assertTrue(ok)
        self.assertEqual(send.call_args[0][:2], (100, "hi"))


class ConsoleTests(Fixture):
    def test_flatten_and_records(self):
        from almanac import console
        self.assertEqual(console.flatten("  one\r\ntwo\n\nthree  "), "one two three")
        self.assertEqual(len(console.flatten("x" * 9000)), console.MAX_CHARS)
        recs = console.records("hé", True)
        self.assertEqual(len(recs), 6, "down + up for each of h, é, Enter")
        self.assertEqual(recs[-2].Event.KeyEvent.wVirtualKeyCode, console.VK_RETURN)
        self.assertEqual(console.records("😀", False)[0].Event.KeyEvent.uChar, "\ud83d", "a surrogate pair goes as two units")
        self.assertEqual(console.send(1, "   "), (False, "nothing to send"))
