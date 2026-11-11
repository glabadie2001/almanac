import json
import time
from unittest import mock
from datetime import datetime, timezone

from support import TOKEN, Fixture, assistant, result, tool_use, write_jsonl

from almanac.config import load
from almanac.door import Service, make_handler
from almanac.store import Store
from test_fleet import FT0


class DoorTests(Fixture):
    def setUp(self):
        super().setUp()
        ts = datetime.fromtimestamp(time.time() - 60, timezone.utc).isoformat()
        write_jsonl(self.projects / "p" / "s.jsonl", [
            assistant("s", ts, "m1", [tool_use("u1", "Bash", command="rg -n foo .")]), result("s", ts, "u1", "hit")])
        self.store = Store(self.root / "almanac.db")
        self.addCleanup(self.store.close)
        from test_fleet import FakeMachine
        from almanac.fleet import Fleet
        self.home = self.root / "claude"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "sessions" / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "s", "status": "idle", "name": "one"}), encoding="utf-8")
        self.machine = FakeMachine()
        self.service = Service(self.store, self.projects, 300,
                               fleet=Fleet(self.home, table=self.machine.table, facts=self.machine.facts, lines=self.machine.lines))
        self.handler = make_handler(self.service, TOKEN)

    def test_probes_are_public(self):
        status, _, doc = self.request(self.handler, "/ident", token="")
        self.assertEqual((status, doc["kind"]), (200, "almanac"))
        status, _, doc = self.request(self.handler, "/healthz", token="")
        self.assertEqual((status, doc["ok"]), (200, True))
        status, _, _ = self.request(self.handler, "/api/heartbeat")
        self.assertEqual(status, 404, "no hub heartbeat in the standalone build")

    def test_report_ingest_and_page(self):
        status, _, doc = self.request(self.handler, "/api/ingest", method="POST")
        self.assertEqual((status, doc["calls"]), (200, 1))
        status, _, doc = self.request(self.handler, "/api/report?days=7&bucket=day")
        self.assertEqual(status, 200)
        self.assertEqual(sum(s["calls"] for s in doc["series"]), 1)
        self.assertEqual([t["id"] for t in doc["tiles"]], ["kb_per_call", "ctx_per_out", "shell_min", "adoption", "error_rate"])
        status, _, doc = self.request(self.handler, "/api/report?bucket=month")
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/report?days=-1")
        self.assertEqual(status, 400)
        status, headers, body = self.request(self.handler, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Almanac</title>", body)
        status, headers, _ = self.request(self.handler, "/app.js")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/javascript; charset=utf-8"))


    def test_fleet_session_and_actions(self):
        status, _, doc = self.request(self.handler, "/api/fleet")
        self.assertEqual((status, doc["summary"]["sessions"]), (200, 2))
        status, _, doc = self.request(self.handler, "/api/session?pid=100")
        self.assertEqual((status, doc["process"]["name"]), (200, "one"))
        self.assertIn("usage", doc)
        status, _, doc = self.request(self.handler, "/api/session?pid=5")
        self.assertEqual(status, 404)
        status, _, doc = self.request(self.handler, "/api/sessions?pids=100,200,5&limit=5")
        self.assertEqual(status, 200)
        self.assertEqual([s.get("gone") or s["process"]["pid"] for s in doc["sessions"]], [100, 200, True])
        self.assertIn("meta", doc["sessions"][0])
        from almanac import fleet as fl
        from types import SimpleNamespace
        with mock.patch.object(fl.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"SUCCESS", stderr=b"")):
            status, _, doc = self.request(self.handler, "/api/kill", method="POST", body={"pid": 100, "created": str(FT0 + 1)})
        self.assertEqual((status, doc["ok"], doc["action"], doc["who"]), (200, True, "kill", "operator"))
        status, _, doc = self.request(self.handler, "/api/kill", method="POST", body={"pid": 100, "created": "1"})
        self.assertEqual((status, doc["ok"]), (409, False))
        with mock.patch.object(fl.console, "send", return_value=(True, "typed 5 characters and Enter")):
            status, _, doc = self.request(self.handler, "/api/send", method="POST", body={"pid": 100, "created": str(FT0 + 1), "text": "hello"})
        self.assertEqual((status, doc["detail"]), (200, "typed 5 characters and Enter: hello"))
        status, _, doc = self.request(self.handler, "/api/send", method="POST", body={"pid": 100, "created": str(FT0 + 1), "text": " "})
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/kill", method="POST", body={"pid": "100", "created": "1"})
        self.assertEqual(status, 400)
        status, _, doc = self.request(self.handler, "/api/actions")
        self.assertEqual([a["action"] for a in doc["actions"]], ["send", "kill", "kill"])

    def test_browser_login_cookie_host_and_origin(self):
        status, _, _ = self.request(self.handler, "/login?token=wrong", token="")
        self.assertEqual(status, 401)
        status, headers, _ = self.request(self.handler, f"/login?token={TOKEN}", token="")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, _, _ = self.request(self.handler, "/api/fleet", token="", headers={"Cookie": cookie})
        self.assertEqual(status, 200, "the cookie signs the browser in")
        forged = cookie[:-4] + "0000"
        status, _, _ = self.request(self.handler, "/api/fleet", token="", headers={"Cookie": forged})
        self.assertEqual(status, 401)
        status, _, doc = self.request(self.handler, "/api/ingest", token="", method="POST",
                                      headers={"Cookie": cookie, "Origin": "http://127.0.0.1:8812"})
        self.assertEqual(status, 200)
        status, _, doc = self.request(self.handler, "/api/kill", token="", method="POST", body={"pid": 100, "created": str(FT0 + 1)},
                                      headers={"Cookie": cookie, "Origin": "https://evil.example"})
        self.assertEqual(status, 403, "cross-origin POST refused")
        status, _, _ = self.request(self.handler, "/api/fleet", headers={"Host": "evil.example:8812"})
        self.assertEqual(status, 403, "DNS rebinding: wrong Host refused even with the token")
        hosted = make_handler(self.service, TOKEN, hosts=["box.example:443"])
        status, _, _ = self.request(hosted, "/api/fleet", headers={"Host": "box.example:443"})
        self.assertEqual(status, 200, "configured hosts pass")

    def test_token_file_and_loopback_only(self):
        from almanac import web
        path = self.root / "state" / "token"
        token = web.ensure_token(path)
        self.assertEqual((len(token), web.ensure_token(path)), (64, token), "made once, then reused")
        with self.assertRaises(ValueError):
            web.serve(object, 0, host="0.0.0.0")

    def test_auth_and_unknown_paths(self):
        status, _, _ = self.request(self.handler, "/api/report", token="")
        self.assertIn(status, (401, 403))
        status, _, _ = self.request(self.handler, "/api/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.request(self.handler, "/api/report", method="DELETE")
        self.assertEqual(status, 405)

    def test_config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"port": 8812, "interval": 60}), encoding="utf-8")
        self.assertEqual(load(path)["interval"], 60)
        path.write_text(json.dumps({"prices": {"claude-opus-5-5": 5}, "hosts": ["a:1"], "naming": False}), encoding="utf-8")
        self.assertEqual(load(path)["prices"], {"claude-opus-5-5": 5})
        for bad in ({"port": 0}, {"interval": 5}, {"nope": 1}, {"port": "8812"}, {"prices": {"m": "cheap"}}, {"hosts": [""]}):
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load(path)


class PortCookieTests(Fixture):
    """Two Almanacs on one host: browsers keep cookies per host, not per port, so each door names its own."""

    def setUp(self):
        super().setUp()
        from almanac import web
        self.web = web
        store = Store(self.root / "a.db")
        self.addCleanup(store.close)
        service = Service(store, self.projects, 300)
        self.doors = {8812: (make_handler(service, TOKEN), TOKEN), 8897: (make_handler(service, "other-" + TOKEN), "other-" + TOKEN)}

    def call(self, port, path, cookie=None, method="GET"):
        from types import SimpleNamespace
        from support import MemorySocket
        handler = self.doors[port][0]
        hdr = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
        hdr += "Content-Length: 0\r\n" if method == "POST" else ""
        hdr += f"Cookie: {cookie}\r\n" if cookie else ""
        conn = MemorySocket((hdr + "\r\n").encode())
        handler(conn, ("127.0.0.1", 9), SimpleNamespace(server_port=port, server_name="localhost"))
        head = conn.output.getvalue().partition(b"\r\n\r\n")[0].decode().split("\r\n")
        return int(head[0].split()[1]), dict(line.split(": ", 1) for line in head[1:] if ": " in line)

    def login(self, port):
        status, headers = self.call(port, f"/login?token={self.doors[port][1]}")
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_a_second_door_on_the_same_host_cannot_sign_the_first_out(self):
        a, b = self.login(8812), self.login(8897)
        self.assertTrue(a.startswith("almanac_session_8812=") and b.startswith("almanac_session_8897="))
        jar = f"{a}; {b}"                                          # the browser sends both to either port
        self.assertEqual(self.call(8812, "/api/fleet", jar)[0], 200)
        self.assertEqual(self.call(8897, "/api/fleet", jar)[0], 200)
        self.assertEqual(self.call(8812, "/api/fleet", b)[0], 401, "the other door's cookie is not ours")

    def test_the_old_shared_name_is_not_read(self):
        value = self.login(8812).split("=", 1)[1]
        self.assertEqual(self.call(8812, "/api/fleet", f"almanac_session={value}")[0], 401)

    def test_logout_clears_the_port_cookie(self):
        status, headers = self.call(8812, "/logout", self.login(8812), method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Set-Cookie"].startswith("almanac_session_8812=; "))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
