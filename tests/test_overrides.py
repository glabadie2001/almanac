"""FIX-6: a model/effort override set on one live session survives another session's detail or tile poll; only
a look at the whole fleet drops the overrides of sessions that left it."""
from support import TOKEN
from test_server_fixes import MeshRig


class OverrideTests(MeshRig):
    def key(self, pid):
        proc = next(p for p in self.service.fleet_now()["processes"] if p["pid"] == pid)
        return f"{pid}:{proc['created']}"

    def test_a_single_session_poll_keeps_every_other_override(self):
        arch, impl = self.key(100), self.key(500)
        for k, model in ((arch, "claude-opus-5-5"), (impl, "claude-sonnet-5-5")):
            self.service.model_overrides[k] = {"model": model, "effort": "high", "ts": 1.0}
        self.assertEqual(self.request(self.handler, "/api/sessions?pids=100")[0], 200)
        self.assertEqual(self.request(self.handler, "/api/session?pid=100")[0], 200)
        self.assertEqual(set(self.service.model_overrides), {arch, impl}, "the gallery tile and FLEET detail polls")
        _, _, doc = self.request(self.handler, "/api/sessions?pids=500")
        self.assertEqual(doc["sessions"][0]["meta"]["model"], "claude-sonnet-5-5")

    def test_the_whole_fleet_still_drops_a_session_that_left(self):
        arch, impl = self.key(100), self.key(500)
        self.service.model_overrides.update({arch: {"model": "a"}, impl: {"model": "b"}})
        del self.machine.procs[500]                     # impl exits
        self.fleet.last["at"] = 0                       # the next look rescans
        self.service.fleet_now()
        self.assertEqual(set(self.service.model_overrides), {arch})
