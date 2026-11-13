import json
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from support import TOKEN, Fixture, assistant, runner_probe, tool_use, write_jsonl
from test_fleet import CLI, FT0, FakeMachine

from almanac import gemini_host, mcp_mesh, spawner, uds
from almanac.door import Service, make_handler
from almanac.fleet import Fleet, tail_transcript
from almanac.store import Store


class MeshTranscriptAndFleetTests(Fixture):
    def test_tail_parses_peer_messages_gemini_and_antigravity_steps(self):
        tx = self.projects / "p" / "mesh-chat.jsonl"
        inbound_xml = uds.format_cross_session(
            "Please review auth.py lines 10-40",
            from_pipe=r"\\.\pipe\LOCAL\cc-msg-1111",
            from_name="impl-gemini",
            channel="#review",
        )
        write_jsonl(
            tx,
            [
                {"type": "user", "timestamp": "t0", "message": {"content": inbound_xml}},
                assistant(
                    "s1",
                    "t1",
                    "m1",
                    [
                        {"type": "text", "text": "Reviewing auth.py now."},
                        tool_use("u1", "SendMessage", to="impl-gemini", message="LGTM, tests green!"),
                        tool_use("u2", "mcp__almanac-mesh__mesh_send", to="@squad", message="Wave 1 approved"),
                    ],
                ),
                {
                    "type": "assistant",
                    "provider": "gemini",
                    "sessionId": "s1",
                    "timestamp": "t2",
                    "message": {"id": "m2", "model": "gemini-2.5-pro", "content": [{"type": "text", "text": "Gemini synthesis."}]},
                },
                {"type": "USER_INPUT", "created_at": "t3", "content": "Direct Antigravity prompt"},
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "t4",
                    "content": "Antigravity reply",
                    "tool_calls": [{"name": "run_command", "arguments": {"toolSummary": "Run unit tests"}}],
                },
            ],
        )
        events = tail_transcript(tx, limit=20)
        kinds_texts = [(e["kind"], e["text"]) for e in events]
        self.assertIn(("peer_in", "impl-gemini: [#review] Please review auth.py lines 10-40"), kinds_texts)
        self.assertIn(("claude", "Reviewing auth.py now."), kinds_texts)
        self.assertIn(("peer_out", "impl-gemini: LGTM, tests green!"), kinds_texts)
        self.assertIn(("peer_out", "@squad: Wave 1 approved"), kinds_texts)
        self.assertIn(("gemini", "Gemini synthesis."), kinds_texts)
        self.assertIn(("you", "Direct Antigravity prompt"), kinds_texts)
        self.assertIn(("gemini", "Antigravity reply"), kinds_texts)
        self.assertIn(("tool", "run_command: Run unit tests"), kinds_texts)

    def test_fleet_discovers_gemini_hides_hub_and_prefers_uds_send(self):
        home = self.root / "claude"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)

        # Register a live Gemini peer (pid 500) and Almanac's internal hub (pid 600)
        received = []
        gem_peer = uds.PeerServer(
            sessions,
            name="impl-gemini",
            pid=500,
            session_id="gem-sess-500",
            cwd=str(self.root),
            provider="gemini",
            model="gemini-2.5-pro",
            effort="high",
            role_name="implementer-gemini",
            squad_id="sq-alpha",
            on_message=received.append,
            in_memory=True,
        ).start()
        self.addCleanup(gem_peer.close)

        hub_peer = uds.PeerServer(
            sessions,
            name="almanac-hub",
            pid=600,
            provider="almanac",
            in_memory=True,
        ).start()
        self.addCleanup(hub_peer.close)

        machine = FakeMachine()
        machine.procs[500] = dict(
            ppid=1,
            exe="python.exe",
            path=r"C:\Python\python.exe",
            cmd="python -m almanac.gemini_host --name impl-gemini",
            created=int(gem_peer.proc_start),
            cpu=0,
            mem=180 << 20,
        )
        machine.procs[600] = dict(
            ppid=1,
            exe="python.exe",
            path=r"C:\Python\python.exe",
            cmd="python -m almanac",
            created=int(hub_peer.proc_start),
            cpu=0,
            mem=90 << 20,
        )
        (sessions / "100.json").write_text(
            json.dumps({"pid": 100, "sessionId": "sess-c", "cwd": str(self.root), "status": "idle", "name": "arch-claude"}),
            encoding="utf-8",
        )

        fleet = Fleet(home, table=machine.table, facts=machine.facts, lines=machine.lines)
        snap = fleet.snapshot()
        by_pid = {p["pid"]: p for p in snap["processes"]}

        self.assertIn(500, by_pid, "managed Gemini host must appear in Fleet")
        self.assertNotIn(600, by_pid, "almanac-hub internal listener must stay hidden from Fleet")
        self.assertEqual(by_pid[500]["provider"], "gemini")
        self.assertEqual(by_pid[500]["agent_role"], "implementer-gemini")
        self.assertTrue(by_pid[500]["has_uds"])
        self.assertEqual(snap["summary"]["providers"]["gemini"], 1)
        self.assertEqual(snap["summary"]["providers"]["claude"], 2)

        # Sending to PID 500 routes over uds-messaging (preserving multiline text) without touching Win32 console
        ok, detail, _ = fleet.send(500, str(machine.procs[500]["created"]), "line one\nline two", from_name="operator")
        self.assertTrue(ok)
        self.assertIn("uds-messaging", detail)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["body"], "line one\nline two")


class MeshDoorAndMcpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        self.store = Store(self.state / "almanac.db")
        self.addCleanup(self.store.close)
        self.home = self.root / "claude"
        (self.home / "sessions").mkdir(parents=True)

        ts = datetime.fromtimestamp(time.time() - 30, timezone.utc).isoformat()
        claude_projects = self.home / "projects"
        write_jsonl(
            claude_projects / "p" / "sess-claude.jsonl",
            [
                {"type": "user", "timestamp": ts, "message": {"content": "Design the mesh API"}},
                assistant("sess-claude", ts, "m1", [{"type": "text", "text": "Drafting schema and routes."}]),
            ],
        )

        self.claude_inbox = []
        self.gem_inbox = []
        self.machine = FakeMachine()
        self.claude_peer = uds.PeerServer(
            self.home / "sessions",
            name="arch-claude",
            pid=100,
            session_id="sess-claude",
            cwd=str(self.root),
            provider="claude",
            model="claude-opus-4-6",
            role_name="architect-claude",
            squad_id="sq-1",
            channels=["#squad", "#architecture"],
            on_message=self.claude_inbox.append,
            in_memory=True,
        ).start()
        self.addCleanup(self.claude_peer.close)
        self.machine.procs[100]["created"] = int(self.claude_peer.proc_start)

        self.gem_peer = uds.PeerServer(
            self.home / "sessions",
            name="impl-gemini",
            pid=500,
            session_id="sess-gemini",
            cwd=str(self.root),
            provider="gemini",
            model="gemini-2.5-pro",
            role_name="implementer-gemini",
            squad_id="sq-1",
            channels=["#squad", "#impl"],
            on_message=self.gem_inbox.append,
            in_memory=True,
        ).start()
        self.addCleanup(self.gem_peer.close)
        self.machine.procs[500] = dict(
            ppid=1,
            exe="python.exe",
            path=r"C:\Python\python.exe",
            cmd="python -m almanac.gemini_host --name impl-gemini",
            created=int(self.gem_peer.proc_start),
            cpu=0,
            mem=160 << 20,
        )

        fleet = Fleet(
            self.home,
            table=self.machine.table,
            facts=self.machine.facts,
            lines=self.machine.lines,
            extra_projects=[self.state / "gemini" / "projects"],
        )
        self.service = Service(self.store, claude_projects, 300, fleet=fleet, state=self.state)
        self.service.hub_peer = uds.PeerServer(
            self.home / "sessions", name="almanac-hub", pid=601, provider="almanac", on_message=self.service._on_hub_message, in_memory=True
        ).start()
        self.addCleanup(self.service.close_hub)
        self.handler = make_handler(self.service, TOKEN)

    def test_roles_crud_and_mesh_endpoints(self):
        status, _, doc = self.request(self.handler, "/api/roles")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(doc["roles"]), 6)

        status, _, up = self.request(
            self.handler,
            "/api/roles",
            method="POST",
            body={
                "id": "sec-gemini",
                "title": "Security Auditor (Gemini)",
                "provider": "gemini",
                "model": "gemini-2.5-pro",
                "effort": "high",
                "workspace_mode": "shared",
                "system_prompt": "Audit auth boundaries and report over mesh_send.",
            },
        )
        self.assertEqual((status, up["ok"], up["role"]["id"]), (200, True, "sec-gemini"))

        status, _, mesh = self.request(self.handler, "/api/mesh?squad=sq-1")
        self.assertEqual(status, 200)
        names = {p["name"] for p in mesh["peers"]}
        self.assertEqual(names, {"arch-claude", "impl-gemini"})

        # Context stream of live peer
        status, _, ctx = self.request(self.handler, "/api/mesh/context?peer=arch-claude&kinds=claude")
        self.assertEqual(status, 200)
        self.assertTrue(ctx["live"])
        self.assertEqual(ctx["events"], [{"ts": ctx["events"][0]["ts"], "kind": "claude", "text": "Drafting schema and routes."}])

        # Broadcast from arch-claude to @squad delivers to impl-gemini (excluding sender)
        status, _, sent = self.request(
            self.handler,
            "/api/mesh/send",
            method="POST",
            body={"from_name": "arch-claude", "to": "@squad", "text": "Schema locked; start impl!", "squad_id": "sq-1"},
        )
        self.assertEqual((status, sent["ok"], sent["delivered"]), (200, True, ["impl-gemini"]))
        self.assertEqual(len(self.gem_inbox), 1)
        self.assertEqual(len(self.claude_inbox), 0, "sender is not echoed its own @squad broadcast")

        # Delete custom role
        status, _, deleted = self.request(self.handler, "/api/roles?id=sec-gemini", method="DELETE")
        self.assertEqual((status, deleted["ok"]), (200, True))

    def test_long_poll_wait_wakes_immediately_on_incoming_message(self):
        since = self.store.latest_message_id()
        result_box = {}

        def waiter():
            _, _, doc = self.request(self.handler, f"/api/mesh/wait?peer=arch-claude&since_id={since}&timeout=5")
            result_box["doc"] = doc

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.08)
        self.request(
            self.handler,
            "/api/mesh/send",
            method="POST",
            body={"from_name": "impl-gemini", "to": "arch-claude", "text": "PR ready for review", "channel": "#review"},
        )
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(result_box["doc"]["timed_out"])
        self.assertEqual(result_box["doc"]["messages"][0]["body"], "PR ready for review")

    def test_mcp_mesh_jsonrpc_and_tools(self):
        def fake_api(path, method="GET", body=None, timeout=30.0):
            status, _, doc = self.request(self.handler, path, method=method, body=body)
            if status >= 400 and status != 409:
                raise RuntimeError(doc.get("error") or str(status))
            return doc

        client = mcp_mesh.MeshClient(url="http://127.0.0.1:8812", token=TOKEN, self_name="impl-gemini", squad_id="sq-1", api_fn=fake_api)

        init_resp = mcp_mesh.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, client)
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "almanac-mesh")

        tools_resp = mcp_mesh.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, client)
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        self.assertEqual(
            tool_names,
            ["mesh_roster", "mesh_context_stream", "mesh_contact_stream", "mesh_send", "mesh_spawn",
             "mesh_objects", "mesh_object", "mesh_diff", "mesh_file", "mesh_log", "mesh_amend", "mesh_respawn", "mesh_restart"],
        )
        # mesh_wait is the CLI's: over MCP it is refused by name, with what to do instead
        wait_rpc = mcp_mesh.handle_rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "mesh_wait", "arguments": {}}}, client)
        self.assertEqual(wait_rpc["error"]["code"], -32601)
        self.assertIn("end your turn", wait_rpc["error"]["message"])

        # Call mesh_send via JSON-RPC tools/call
        send_rpc = mcp_mesh.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "mesh_send", "arguments": {"to": "arch-claude", "message": "Hello from MCP!", "channel": "#impl"}},
            },
            client,
        )
        send_payload = json.loads(send_rpc["result"]["content"][0]["text"])
        self.assertTrue(send_payload["ok"])
        self.assertEqual(self.claude_inbox[-1]["body"], "Hello from MCP!")

        # Call mesh_contact_stream & mesh_context_stream
        contacts = client.call_tool("mesh_contact_stream", {"peer": "arch-claude"})
        self.assertEqual(contacts["messages"][-1]["body"], "Hello from MCP!")
        ctx = client.call_tool("mesh_context_stream", {"peer": "arch-claude", "limit": 5})
        self.assertEqual(ctx["peer"]["provider"], "claude")

    def test_models_catalog_and_live_fleet_model_override(self):
        from almanac import console

        status, _, catalog = self.request(self.handler, "/api/models")
        self.assertEqual(status, 200)
        for expected in ("fable", "claude-fable-5-2", "claude-fable-5-1", "fable-5-2", "mythos", "claude-mythos-5-2", "claude-mythos-5-1", "opus-5-5", "claude-opus-5-5"):
            self.assertIn(expected, catalog["claude"])
        for gem_base in ("gemini-4-carbon", "gemini-4-argon", "gemini-3.8-flash", "gpt-oss-120b"):
            self.assertIn(gem_base, catalog["gemini"])
        for suffixed in ("gemini-4-carbon-high", "gemini-4-carbon-medium", "gemini-4-carbon-low"):
            self.assertNotIn(suffixed, catalog["gemini"])
        self.assertEqual(catalog["efforts"]["gemini"]["gemini-4-carbon"], ["low", "medium", "high"])
        self.assertEqual(catalog["efforts"]["gemini"]["gpt-oss-120b"], ["medium"])
        self.assertEqual(catalog["efforts"]["claude"]["*"], ["low", "medium", "high", "xhigh", "max"])
        self.assertTrue(set(catalog["claude"]).issubset(set(catalog["all"])))

        # Live Claude Fleet row: shorthand "opus-5-5" normalizes to "claude-opus-5-5" and types /model
        created_100 = str(self.machine.procs[100]["created"])
        with mock.patch.object(console, "send", return_value=(True, "typed")) as mock_con:
            status, _, mres = self.request(
                self.handler,
                "/api/model",
                method="POST",
                body={"pid": 100, "created": created_100, "model": "opus-5-5"},
            )
        self.assertEqual((status, mres["ok"], mres["model"]), (200, True, "claude-opus-5-5"))
        mock_con.assert_called_once_with(100, "/model claude-opus-5-5")

        # Effort-only update on live Claude row normalizes "med" -> "medium" and types /effort
        with mock.patch.object(console, "send", return_value=(True, "typed")) as mock_con_eff:
            status, _, eres = self.request(
                self.handler,
                "/api/model",
                method="POST",
                body={"pid": 100, "created": created_100, "effort": "med"},
            )
        self.assertEqual((status, eres["ok"], eres["model"], eres["effort"]), (200, True, "claude-opus-5-5", "medium"))
        mock_con_eff.assert_called_once_with(100, "/effort medium")

        # Live Gemini Fleet row: friendly alias + "med" separates cleanly into ("gemini-4-carbon", "medium")
        created_500 = str(self.machine.procs[500]["created"])
        status, _, gres = self.request(
            self.handler,
            "/api/model",
            method="POST",
            body={"pid": 500, "created": created_500, "model": "Gemini Carbon", "effort": "med"},
        )
        self.assertEqual((status, gres["ok"], gres["model"], gres["effort"]), (200, True, "gemini-4-carbon", "medium"), gres)
        self.assertEqual(self.gem_inbox[-1]["channel"], "__almanac_control__")
        self.assertEqual(
            json.loads(self.gem_inbox[-1]["body"]),
            {"op": "set_model", "model": "gemini-4-carbon", "effort": "medium"},
        )

        # Effort-only update on live Gemini row keeps base model and updates effort column independently
        status, _, gres_eff = self.request(
            self.handler,
            "/api/model",
            method="POST",
            body={"pid": 500, "created": created_500, "effort": "low"},
        )
        self.assertEqual((status, gres_eff["ok"], gres_eff["model"], gres_eff["effort"]), (200, True, "gemini-4-carbon", "low"))

        # Legacy suffixed Gemini model string splits into clean model + effort
        status, _, gres_split = self.request(
            self.handler,
            "/api/model",
            method="POST",
            body={"pid": 500, "created": created_500, "model": "gemini-4-carbon-high"},
        )
        self.assertEqual((status, gres_split["ok"], gres_split["model"], gres_split["effort"]), (200, True, "gemini-4-carbon", "high"))

        # Overrides reflect immediately in /api/fleet and /api/session as separate model & effort fields
        _, _, fleet_doc = self.request(self.handler, "/api/fleet")
        by_pid = {p["pid"]: p for p in fleet_doc["processes"]}
        self.assertEqual((by_pid[100]["model"], by_pid[100]["effort"]), ("claude-opus-5-5", "medium"))
        self.assertEqual((by_pid[500]["model"], by_pid[500]["effort"]), ("gemini-4-carbon", "high"))

        status, _, sdet = self.request(self.handler, "/api/session?pid=100")
        self.assertEqual((status, sdet["process"]["model"], sdet["meta"]["model"]), (200, "claude-opus-5-5", "claude-opus-5-5"))


class GeminiHostAndSpawnerTests(Fixture):
    def test_gemini_host_turn_execution_uds_wakeup_and_store_ingest(self):
        state = self.root / "state"
        gem_projects = state / "gemini" / "projects"
        tx_path = gem_projects / "repo" / "gem-session-1.jsonl"
        sessions = self.root / "claude" / "sessions"

        # Verify NDJSON stream event normalizer (flat & real nested agy.exe wire format)
        self.assertEqual(
            gemini_host.parse_agy_stream_event({"type": "init", "conversation_id": "conv-9", "model": "gemini-2.5-pro"}),
            {"kind": "init", "conversation_id": "conv-9", "model": "gemini-2.5-pro"},
        )
        self.assertEqual(
            gemini_host.parse_agy_stream_event(
                {"event": "init", "conversation_id": "conv-live-1", "init": {"cwd": "C:/repo", "permission_mode": "auto"}}
            ),
            {"kind": "init", "conversation_id": "conv-live-1", "model": None},
        )
        tool_ev = gemini_host.parse_agy_stream_event(
            {"type": "step_update", "tool_info": {"id": "t1", "tool_name": "read_file", "parameters": {"Path": "a.py"}, "output": "ok"}}
        )
        self.assertEqual((tool_ev["kind"], tool_ev["tool_name"], tool_ev["tool_output"]), ("tool", "read_file", "ok"))
        active_tool = gemini_host.parse_agy_stream_event(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-live-1",
                    "step_index": 2,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "write_file",
                    "tool_info": {"name": "write_file", "parameters": {"Path": "out.txt"}},
                },
            }
        )
        done_tool = gemini_host.parse_agy_stream_event(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-live-1",
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "write_file",
                    "duration_seconds": 1.25,
                },
            }
        )
        self.assertEqual((active_tool["phase"], active_tool["tool_id"]), ("start", done_tool["tool_id"]))
        self.assertEqual((done_tool["phase"], done_tool["tool_output"]), ("done", "completed in 1.25s"))
        delta_ev = gemini_host.parse_agy_stream_event(
            {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "PONG"}}
        )
        self.assertEqual((delta_ev["kind"], delta_ev["text"], delta_ev["is_delta"]), ("text", "PONG", True))
        res_ev = gemini_host.parse_agy_stream_event(
            {"type": "result", "result": "Done!", "model": "gemini-2.5-pro", "usage": {"input_tokens": 120, "output_tokens": 45}}
        )
        self.assertEqual((res_ev["kind"], res_ev["text"], res_ev["usage"]["output_tokens"]), ("result", "Done!", 45))
        nested_res = gemini_host.parse_agy_stream_event(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-live-1",
                    "status": "SUCCESS",
                    "response": "PONG\n",
                    "usage": {"input_tokens": 14000, "cache_read_tokens": 9000, "output_tokens": 64},
                },
            }
        )
        self.assertEqual(
            (nested_res["kind"], nested_res["text"], nested_res["usage"]["cache_read_input_tokens"]),
            ("result", "PONG\n", 9000),
        )

        prompts_seen = []

        def fake_runner(prompt_text, host):
            prompts_seen.append(prompt_text)
            return (
                f"Processed turn #{len(prompts_seen)}",
                [],
                {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50, "output_tokens": 30},
                "gemini-2.5-pro",
            )

        host = gemini_host.GeminiHost(
            name="verif-gemini",
            cwd=str(self.root),
            transcript_path=tx_path,
            sessions_dir=sessions,
            session_id="gem-session-1",
            model="gemini-2.5-pro",
            effort="high",
            role_name="reviewer-gemini",
            squad_id="sq-2",
            system_prompt="Verify edge cases carefully.",
            in_memory_uds=True,
            runner_fn=fake_runner,
            quiet=True,
        )
        host.peer.start()
        self.addCleanup(host.close)

        # Turn 1: initial prompt includes system role charter
        host.enqueue_prompt("Audituds.py")
        host.run_one_turn(host.turns.get(timeout=1))
        self.assertIn("<system-role-instructions>", prompts_seen[0])

        # Turn 2: inbound `<cross-session-message>` over uds-messaging queues a wakeup turn
        uds.send_peer(sessions, "verif-gemini", "Check door.py next", from_name="arch-claude", channel="#review", alive_fn=lambda _: True)
        peer_turn = host.turns.get(timeout=1)
        host.run_one_turn(peer_turn)
        self.assertIn("<cross-session-message", prompts_seen[1])

        # Verify clean model/effort separation on GeminiHost + agy.exe wire recomposition
        self.assertEqual(
            gemini_host.resolve_agy_wire_model("gemini-4-carbon", "low"),
            ("gemini-4-carbon-low", "low"),
        )
        self.assertEqual(
            gemini_host.resolve_agy_wire_model("gemini-4-carbon", "med"),
            ("gemini-4-carbon-medium", "medium"),
        )
        self.assertEqual(
            gemini_host.resolve_agy_wire_model("gpt-oss-120b", "high"),
            ("gpt-oss-120b-medium", "medium"),
        )

        # Control frame (__almanac_control__) switches GeminiHost model & effort without enqueuing a user prompt turn
        uds.send_peer(
            sessions,
            "verif-gemini",
            json.dumps({"op": "set_model", "model": "gemini-4-carbon-high", "effort": "medium"}),
            from_name="almanac-hub",
            channel="__almanac_control__",
            alive_fn=lambda _: True,
        )
        self.assertEqual((host.model, host.effort), ("gemini-4-carbon", "medium"))
        self.assertTrue(host.turns.empty(), "__almanac_control__ must not enqueue a prompt turn")
        # host.model is set before the registration file is rewritten (on the pipe thread), so wait for the file
        deadline = time.monotonic() + 5
        while json.loads(host.peer.session_file.read_text(encoding="utf-8"))["model"] != "gemini-4-carbon" and time.monotonic() < deadline:
            time.sleep(0.01)
        reg_doc = json.loads(host.peer.session_file.read_text(encoding="utf-8"))
        self.assertEqual(reg_doc["model"], "gemini-4-carbon")

        # Ingest into Store & verify Gemini chat + cross-session message indexing
        store = Store(state / "almanac.db")
        self.addCleanup(store.close)
        service = Service(store, self.projects, 300, state=state)
        ing = service.ingest()
        self.assertGreaterEqual(ing["messages"], 2)

        handler = make_handler(service, TOKEN)
        status, _, chats_doc = self.request(handler, "/api/chats")
        self.assertEqual(status, 200)
        gem_chat = next(c for c in chats_doc["chats"] if c["id"] == "gem-session-1")
        self.assertEqual((gem_chat["provider"], gem_chat["on_disk"], gem_chat["resumable"]), ("gemini", True, False))

        status, _, det_doc = self.request(handler, "/api/chat?id=gem-session-1")
        self.assertEqual(status, 200)
        ev_kinds = [e["kind"] for e in det_doc["events"]]
        self.assertIn("peer_in", ev_kinds)
        self.assertIn("gemini", ev_kinds)

    def test_spawner_builds_claude_and_gemini_squad_configs(self):
        state = self.root / "state"
        store = Store(state / "almanac.db")
        self.addCleanup(store.close)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        fleet = Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {})
        service = Service(store, self.projects, 300, fleet=fleet, state=state, claude="claude.exe", gemini="agy.exe",
                          probe=runner_probe(self.root))
        handler = make_handler(service, TOKEN)

        repo = self.root / "workspace"
        repo.mkdir()

        launched_cmds = []
        next_pid = iter([7001, 7002, 7003])

        def fake_popen(argv, **kwargs):
            pid = next(next_pid)
            launched_cmds.append((pid, list(argv), kwargs))
            proc = mock.MagicMock()
            proc.pid = pid
            proc.poll.return_value = None
            return proc

        self.assertEqual(spawner.normalize_model("claude", "opus-5-5"), "claude-opus-5-5")
        self.assertEqual(spawner.normalize_model("claude", "opus-5.5"), "claude-opus-5-5")
        self.assertEqual(spawner.normalize_model("claude", "opus"), "opus")
        self.assertEqual(spawner.normalize_model("claude", "fable"), "fable")
        self.assertEqual(spawner.normalize_model("claude", "fable-5-2"), "claude-fable-5-2")
        self.assertEqual(spawner.normalize_model("claude", "fable-5.2"), "claude-fable-5-2")
        self.assertEqual(spawner.normalize_model("claude", "mythos"), "mythos")
        self.assertEqual(spawner.normalize_model("claude", "mythos-5.1"), "claude-mythos-5-1")
        self.assertEqual(spawner.normalize_model("gemini", "gemini-3.8-flash-high"), "gemini-3.8-flash")
        self.assertEqual(spawner.normalize_model("gemini", "Gemini Carbon"), "gemini-4-carbon")
        self.assertEqual(
            spawner.split_model_effort("gemini", "gemini-4-carbon-high"),
            ("gemini-4-carbon", "high"),
        )
        self.assertEqual(
            spawner.split_model_effort("gemini", "Gemini Carbon", "med"),
            ("gemini-4-carbon", "medium"),
        )

        with mock.patch.object(spawner.subprocess, "Popen", side_effect=fake_popen):
            status, _, sq = self.request(
                handler,
                "/api/squads",
                method="POST",
                body={
                    "name": "auth-squad",
                    "cwd": str(repo),
                    "brief": "Implement and verify OAuth PKCE flow.",
                    "members": [
                        {"role_id": "architect-claude", "agent_name": "lead-claude", "model": "opus-5-5", "effort": "high", "workspace_mode": "shared"},
                        {"role_id": "implementer-gemini", "agent_name": "dev-gemini", "model": "gemini-4-carbon", "effort": "med", "workspace_mode": "shared"},
                    ],
                },
            )

        self.assertEqual((status, sq["ok"], sq["launched"]), (200, True, 2))
        self.assertEqual(len(launched_cmds), 2)

        # Verify Claude invocation includes normalized --model claude-opus-5-5, --effort high, --mcp-config, and --append-system-prompt
        _, claude_argv, _ = launched_cmds[0]
        self.assertEqual(claude_argv[0], "claude.exe")
        self.assertEqual(claude_argv[claude_argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(claude_argv[claude_argv.index("--effort") + 1], "high")
        self.assertIn("--mcp-config", claude_argv)
        mcp_file = Path(claude_argv[claude_argv.index("--mcp-config") + 1])
        self.assertTrue(mcp_file.exists())
        mcp_doc = json.loads(mcp_file.read_text(encoding="utf-8"))
        self.assertIn("almanac-mesh", mcp_doc["mcpServers"])

        # Verify Gemini invocation runs almanac.gemini_host with separated --model and --effort and staged agy-bundle plugin
        _, gem_argv, _ = launched_cmds[1]
        self.assertIn("almanac.gemini_host", gem_argv)
        self.assertEqual(gem_argv[gem_argv.index("--model") + 1], "gemini-4-carbon")
        self.assertEqual(gem_argv[gem_argv.index("--effort") + 1], "medium")
        self.assertIn("--add-dir", gem_argv)
        bundle_dir = Path(gem_argv[gem_argv.index("--add-dir") + 1])
        self.assertTrue((bundle_dir / ".agents" / "plugins" / "almanac-mesh" / "mcp_config.json").exists())

        # Squad & spawned agents persisted in SQLite with separated normalized models & efforts
        self.assertEqual(len(store.squads()), 1)
        spawned_by_name = {s["agent_name"]: s for s in store.spawned()}
        self.assertEqual(set(spawned_by_name), {"lead-claude", "dev-gemini"})
        self.assertEqual((spawned_by_name["lead-claude"]["model"], spawned_by_name["lead-claude"]["effort"]), ("claude-opus-5-5", "high"))
        self.assertEqual((spawned_by_name["dev-gemini"]["model"], spawned_by_name["dev-gemini"]["effort"]), ("gemini-4-carbon", "medium"))

        # Verify headless Claude keeps stream-json stdin open for multi-turn uds-messaging
        stdin_writes = []

        def fake_headless_popen(argv, **kwargs):
            proc = mock.MagicMock()
            proc.pid = 7010
            proc.poll.return_value = None
            proc.stdin = mock.MagicMock()
            proc.stdin.write.side_effect = stdin_writes.append
            launched_cmds.append((7010, list(argv), kwargs))
            return proc

        with mock.patch.object(spawner.subprocess, "Popen", side_effect=fake_headless_popen):
            status, _, hres = self.request(
                handler,
                "/api/spawn",
                method="POST",
                body={
                    "provider": "claude",
                    "agent_name": "bg-claude",
                    "cwd": str(repo),
                    "headless": True,
                    "initial_prompt": "Coordinate with dev-gemini",
                },
            )
        self.assertEqual((status, hres["ok"]), (200, True))
        _, h_argv, _ = launched_cmds[-1]
        self.assertIn("stream-json", h_argv)
        self.assertTrue(any("Coordinate with dev-gemini" in w for w in stdin_writes))
        self.assertIn(hres["session_id"], service.headless_procs)


class ReviewTestRunTests(Fixture):
    """T-39: agy allows exact lines only, so a reviewer gets one exact line per test module, and only plain names."""

    def test_a_full_suite_role_gets_one_exact_line_per_plain_test_module(self):
        from almanac.store import REVIEWER_COMMANDS
        tests = self.root / "repo" / "tests"
        tests.mkdir(parents=True)
        plain = ["test_b", "test_a", "test_x9_y"]
        hostile = ["test_a;del x", "test_a&calc", "test_a$(id)", "test_a`id`", "test_a b", "test_a.b", "test_a-b",
                   "test_é", "test_", "tests_a", "test_a(1)", "test_a,b", "test_a'b"]
        for name in plain + hostile:
            try:
                (tests / f"{name}.py").write_text("", encoding="utf-8")
            except OSError:                  # a name this filesystem refuses cannot reach a rule either
                pass
        (tests / "support.py").write_text("", encoding="utf-8")
        cmds = spawner.role_commands(REVIEWER_COMMANDS, self.root / "repo")
        self.assertEqual(cmds[:len(REVIEWER_COMMANDS)], REVIEWER_COMMANDS, "the stored lines come first, unchanged")
        self.assertEqual(cmds[len(REVIEWER_COMMANDS):],
                         ["python -m unittest discover tests -p test_a.py", "python -m unittest discover tests -p test_b.py",
                          "python -m unittest discover tests -p test_x9_y.py"])
        for line in cmds[len(REVIEWER_COMMANDS):]:
            self.assertRegex(line, r"\Apython -m unittest discover tests -p test_[A-Za-z0-9_]+\.py\Z")
        self.assertEqual(cmds.count("python -m unittest discover tests -p test_a.py"), 1)
        self.assertEqual(spawner.role_commands(cmds, self.root / "repo"), cmds, "no line twice")

    def test_a_role_without_the_full_suite_line_and_a_folder_without_tests_get_nothing_more(self):
        tests = self.root / "repo" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_a.py").write_text("", encoding="utf-8")
        self.assertEqual(spawner.role_commands(["git diff"], self.root / "repo"), ["git diff"])
        self.assertEqual(spawner.role_commands(None, self.root / "repo"), [])
        self.assertEqual(spawner.role_commands(["python -m unittest discover tests"], self.root / "empty"),
                         ["python -m unittest discover tests"])

    def test_the_squad_charter_says_who_runs_what(self):
        text = spawner.build_mesh_charter(agent_name="r", provider="gemini", role_title="R", role_prompt="p",
                                          almanac_url="u", squad_id="sq",
                                          allowed_commands=["git diff", "python -m unittest discover tests",
                                                            "python -m unittest discover tests -p test_a.py",
                                                            "python -m unittest discover tests -p test_b.py"])
        self.assertIn("on a fix round, the builder and the reviewer run only the test modules of the files touched", text)
        self.assertIn("by the lead on the merged tree; that run is the merge gate", text)
        self.assertIn("only permits these exact command lines, typed character for character: `git diff`, "
                      "`python -m unittest discover tests`, one test module at a time, "
                      "`python -m unittest discover tests -p test_<name>.py`, for the test files that existed when "
                      "you were spawned; anything else is denied, so do not try variants.", text)
        self.assertNotIn("test_a.py", text, "the module lines are described once, not listed")
        solo = spawner.build_mesh_charter(agent_name="r", provider="claude", role_title="R", role_prompt="p",
                                          almanac_url="u")
        self.assertNotIn("merge gate", solo, "no squad, no review rounds")


import urllib.parse  # noqa: E402
import test_objects  # noqa: E402  (a module, not its names: its test classes must not run twice)
from almanac import door  # noqa: E402
from test_spawn_fixes import git  # noqa: E402


class TaskDiffTests(test_objects.ObjectRig):
    """T-38: `mesh_diff(ref)` reads a handed-in task's `base..sha` from the squad folder, read-only, own squad only."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        (self.repo / "a.py").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.store.db.execute("update squads set cwd = ? where id = 'sq-1'", (str(self.repo),))
        self.wt = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")
        branch, base = spawner.branch_and_base(self.wt)
        self.store.record_spawn(agent_name="impl", session_id="sess-impl", squad_id="sq-1", role_id=None,
                                provider="claude", model=None, effort=None, cwd=str(self.wt),
                                worktree_path=str(self.wt), pid=0, branch=branch, base=base)

    commit = test_objects.HandinTests.commit
    hand_in = test_objects.HandinTests.hand_in

    def diff(self, who="arch", **query):
        return self.request(self.handler, f"/api/mesh/diff?{urllib.parse.urlencode(query)}", token=self.tok(who))

    def handed_in(self):
        self.file(owner="impl")
        (self.wt / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        first = self.commit(self.wt, "b.py")
        second = self.commit(self.wt, "c.py")
        self.hand_in("T-1")
        return first, second

    def test_the_diff_of_a_hand_in_its_stat_one_path_and_one_commit(self):
        first, second = self.handed_in()
        status, _, res = self.diff(ref="T-1")
        self.assertEqual(status, 200, res)
        self.assertEqual((res["base"], res["sha"], res["truncated"]), (self.base, second, False))
        for name in ("a/a.py", "a/b.py", "a/c.py", "+two"):
            self.assertIn(name, res["diff"])
        stat = self.diff(ref="T-1", stat=1)[2]["diff"]
        self.assertIn("3 files changed", stat)
        self.assertNotIn("+two", stat)
        only = self.diff(ref="T-1", path="a.py")[2]["diff"]
        self.assertIn("+two", only)
        self.assertNotIn("b.py", only)
        one = self.diff(ref="T-1", commit=second[:8])[2]
        self.assertEqual(one["commit"], second)
        self.assertIn("c.py", one["diff"])
        self.assertNotIn("b.py", one["diff"], "one commit, not the range")
        self.assertNotIn("+two", one["diff"])

    def test_a_windows_spelled_path_inside_the_repo_still_works(self):
        self.file(owner="impl")
        (self.wt / "sub").mkdir()
        self.commit(self.wt / "sub", "d.py")
        self.hand_in("T-1")
        # git turns `\` into `/` only on Windows; elsewhere, with --literal-pathspecs, it is part of a file name
        for path in ("sub/d.py",) + (("sub\\d.py",) if sys.platform == "win32" else ()):
            status, _, res = self.diff(ref="T-1", path=path)
            self.assertEqual(status, 200, res)
            self.assertIn("sub/d.py", res["diff"], path)

    def test_a_commit_outside_the_task_another_squad_and_hostile_values_are_refused(self):
        self.handed_in()
        main = self.commit(self.repo, "main.py")
        for commit in (self.base, main, "HEAD", "--output=x", "abc;x", "ab", "main"):
            status, _, res = self.diff(ref="T-1", commit=commit)
            self.assertEqual(status, 400, (commit, res))
        for path in ("../x", "a/../../x", "/etc/passwd", "C:/Windows", "\\\\host\\share", ":(top)a.py", ":!a.py",
                     "..\\x", "a\\..\\..\\x", "\\x"):
            status, _, res = self.diff(ref="T-1", path=path)
            self.assertEqual(status, 400, (path, res))
        self.assertEqual(self.diff("other", ref="T-1")[0], 403, "another squad's task")
        self.assertEqual(self.diff(ref="T-9")[0], 404)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "", "nothing written to the squad folder")
        self.assertEqual(git(self.wt, "status", "--porcelain"), "", "nor to the worktree")

    def test_a_task_not_handed_in_answers_with_a_reason(self):
        self.file(owner="impl")
        status, _, res = self.diff(ref="T-1")
        self.assertEqual(status, 200, res)
        self.assertIsNone(res["diff"])
        self.assertIn("has no sha", res["reason"])

    def test_a_large_diff_is_cut_with_its_stat(self):
        self.handed_in()
        with mock.patch.object(door, "DIFF_CAP", 40):
            res = self.diff(ref="T-1")[2]
        self.assertEqual(len(res["diff"]), 40)
        self.assertTrue(res["truncated"])
        self.assertGreater(res["chars"], 40)
        self.assertIn("3 files changed", res["stat_text"])

    def test_the_tool_and_the_cli_verb(self):
        _, second = self.handed_in()
        api = test_objects.McpTests.client(self, "arch")
        self.assertIn("c.py", api.call_tool("mesh_diff", {"ref": "T-1", "stat": True})["diff"])
        self.assertEqual(api.call_tool("mesh_diff", {"ref": "T-1", "commit": second})["commit"], second)
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            mcp_mesh._run_cli(["diff", "T-1", "--path", "a.py"], api)
        self.assertIn("+two", out.getvalue())


if __name__ == "__main__":
    unittest.main()
