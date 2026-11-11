"""Stage 0, identity at the door: an agent token carries the session it was spawned as
(`agent.<name>.<session>.<sig>`), and a two-part token minted before that keeps working as handle-only, because
agents alive across a restart still hold one."""
from support import MemorySocket, TOKEN
from test_server_fixes import MeshRig

from types import SimpleNamespace

from almanac import web

SESSION = "8da9c0de-1111-2222-3333-444455556666"


class TokenTests(MeshRig):
    def test_a_three_part_token_names_the_agent_and_its_session(self):
        bearer = web.agent_token(TOKEN, "impl", SESSION)
        self.assertEqual(bearer.count("."), 3)
        self.assertEqual(web.agent_identity(bearer, TOKEN), ("impl", SESSION))
        self.assertEqual(web.agent_of(bearer, TOKEN), "impl")

    def test_an_old_two_part_token_is_handle_only(self):
        bearer = web.agent_token(TOKEN, "impl")
        self.assertEqual(web.agent_identity(bearer, TOKEN), ("impl", None))
        self.assertEqual(self.request(self.handler, "/api/mesh", token=bearer)[0], 200)

    def test_the_signature_covers_the_session(self):
        bearer = web.agent_token(TOKEN, "impl", SESSION)
        other = bearer.replace(SESSION, "0" * 36)
        self.assertIsNone(web.agent_identity(other, TOKEN), "a session cannot be swapped under the signature")
        self.assertIsNone(web.agent_identity(bearer.replace("impl", "arch", 1), TOKEN))
        # the two-part signature does not validate a three-part token, nor the other way round
        name_sig = web.agent_token(TOKEN, "impl").rsplit(".", 1)[1]
        self.assertIsNone(web.agent_identity(f"agent.impl.{SESSION}.{name_sig}", TOKEN))
        self.assertIsNone(web.agent_identity("agent.impl..x", TOKEN))
        self.assertIsNone(web.agent_identity("agent.a.b.c.d", TOKEN))
        self.assertEqual(self.request(self.handler, "/api/mesh", token=other)[0], 401)

    def test_the_door_exposes_the_callers_session(self):
        seen = []

        class Spy(self.handler):
            def _dispatch(self):
                seen.append((self.agent, self.agent_session))
                return super()._dispatch()

        new, old = web.agent_token(TOKEN, "impl", SESSION), web.agent_token(TOKEN, "arch")
        wire = "".join(f"GET /api/mesh HTTP/1.1\r\nHost: 127.0.0.1:8812\r\nAuthorization: Bearer {t}\r\n\r\n"
                       for t in (new, old))
        wire += "GET /ident HTTP/1.1\r\nHost: 127.0.0.1:8812\r\nConnection: close\r\n\r\n"
        Spy(MemorySocket(wire.encode()), ("127.0.0.1", 9), SimpleNamespace(server_port=8812, server_name="localhost"))
        self.assertEqual(seen, [("impl", SESSION), ("arch", None), (None, None)],
                         "each request on a keep-alive connection gets its own caller")

    def test_a_new_token_still_only_opens_the_mesh(self):
        bearer = web.agent_token(TOKEN, "impl", SESSION)
        self.assertEqual(self.request(self.handler, "/api/fleet", token=bearer)[0], 403)
        self.assertEqual(self.request(self.handler, "/api/mesh", token=web.agent_token(TOKEN, "operator", SESSION))[0], 403)
