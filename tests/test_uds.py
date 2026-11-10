import json
import secrets
import sys
import threading
import unittest

from support import Fixture

from almanac import uds


class UdsProtocolTests(Fixture):
    def test_format_and_parse_cross_session_roundtrip(self):
        pipe = r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef0123456789abcdef"
        body = 'Line 1 with "quotes" & <tags>\nLine 2 markdown **bold**'
        raw = uds.format_cross_session(
            body,
            from_pipe=pipe,
            from_name="arch-claude",
            from_mode="prompting",
            channel="#architecture",
        )
        self.assertTrue(raw.startswith("<cross-session-message "))
        parsed = uds.parse_cross_session(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["from_pipe"], pipe)
        self.assertEqual(parsed["from_name"], "arch-claude")
        self.assertEqual(parsed["from_mode"], "prompting")
        self.assertEqual(parsed["channel"], "#architecture")
        self.assertEqual(parsed["body"], body)
        self.assertIsNone(uds.parse_cross_session("plain user prompt"))

    def test_sha256_pipe_lowercases_path(self):
        upper = r"\\.\PIPE\LOCAL\CC-MSG-AABBCCDD"
        lower = r"\\.\pipe\local\cc-msg-aabbccdd"
        self.assertEqual(uds.sha256_pipe(upper), uds.sha256_pipe(lower))

    def test_in_memory_peer_server_auth_discovery_and_cleanup(self):
        sessions = self.root / "sessions"
        inbox = self.root / "inbox.jsonl"
        received = []
        srv = uds.PeerServer(
            sessions,
            name="impl-gemini",
            pid=4242,
            session_id="sess-gem-1",
            cwd=str(self.root),
            provider="gemini",
            model="gemini-2.5-pro",
            role_name="implementer-gemini",
            squad_id="sq-1",
            channels=["#squad", "#impl"],
            inbox_path=inbox,
            on_message=received.append,
            in_memory=True,
        ).start()
        self.addCleanup(srv.close)

        self.assertTrue(srv.session_file.exists())
        self.assertTrue(srv.key_file.exists())

        peers = uds.discover_peers(sessions, alive_fn=lambda pid: pid == 4242)
        self.assertEqual(len(peers), 1)
        self.assertEqual((peers[0]["name"], peers[0]["provider"], peers[0]["has_key"]), ("impl-gemini", "gemini", True))
        self.assertEqual(uds.resolve_peer(sessions, "impl-gemini", alive_fn=lambda _: True)["pid"], 4242)
        self.assertEqual(uds.resolve_peer(sessions, "sess-gem-1", alive_fn=lambda _: True)["pid"], 4242)
        self.assertEqual(uds.resolve_peer(sessions, "4242", alive_fn=lambda _: True)["pid"], 4242)

        # Forged token must be ignored
        uds.send_frame(
            srv.pipe_path,
            "wrong-token",
            body="should be dropped",
            from_pipe="uds:attacker",
            from_name="evil",
        )
        self.assertEqual(received, [])

        # Valid peer send succeeds and preserves multiline content
        res = uds.send_peer(
            sessions,
            "impl-gemini",
            "Wave 1 API ready:\n- GET /api/mesh\n- POST /api/mesh/send",
            from_name="arch-claude",
            channel="#squad",
            alive_fn=lambda _: True,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["target_provider"], "gemini")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["from_name"], "arch-claude")
        self.assertEqual(received[0]["channel"], "#squad")
        self.assertIn("POST /api/mesh/send", received[0]["body"])

        inbox_lines = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(inbox_lines), 1)
        self.assertEqual(inbox_lines[0]["decoded"]["from_name"], "arch-claude")

        srv.close()
        self.assertFalse(srv.session_file.exists())
        self.assertFalse(srv.key_file.exists())

    @unittest.skipUnless(sys.platform == "win32", "Win32 Named Pipes only")
    def test_real_win32_named_pipe_roundtrip(self):
        sessions = self.root / "win32-sessions"
        arrived = threading.Event()
        received = []

        def on_msg(msg):
            received.append(msg)
            arrived.set()

        pipe_path = uds.default_pipe_path("test" + secrets.token_hex(14))
        srv = uds.PeerServer(
            sessions,
            name="win32-peer",
            provider="gemini",
            pipe_path=pipe_path,
            on_message=on_msg,
            in_memory=False,
        ).start()
        self.addCleanup(srv.close)

        # Reject invalid token over the real OS Named Pipe
        uds.send_frame(
            pipe_path,
            "bad-token",
            body="ignore me",
            from_pipe=r"\\.\pipe\LOCAL\cc-msg-sender",
            from_name="intruder",
        )

        # Deliver valid authenticated frame over the real OS Named Pipe
        uds.send_peer(
            sessions,
            "win32-peer",
            "Real Win32 Named Pipe hello\nsecond line intact!",
            from_name="claude-lead",
            channel="#review",
        )
        self.assertTrue(arrived.wait(timeout=3.0), "real Named Pipe message did not arrive within 3s")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["from_name"], "claude-lead")
        self.assertEqual(received[0]["channel"], "#review")
        self.assertEqual(received[0]["body"], "Real Win32 Named Pipe hello\nsecond line intact!")


if __name__ == "__main__":
    unittest.main()
