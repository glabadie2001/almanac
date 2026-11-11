"""`#squad` reached nobody (2026-11-11): the role form never sends `channels`, so every save from it stored `[]`,
and `#squad` was delivered only to subscribers. A role save keeps what the doc leaves out; `#squad` reaches the
whole squad like `@squad`; other channels keep their subscriber rule."""
from support import TOKEN
from test_server_fixes import MeshRig

from almanac import web


class RoleSaveKeepsTests(MeshRig):
    FORM = ("id", "title", "provider", "model", "effort", "workspace_mode", "system_prompt", "may_page_operator",
            "may_spawn", "allowed_commands", "add_dirs")      # the form before it posted channels; it never posts
                                                              # permission_mode

    def test_a_save_without_channels_or_permission_mode_keeps_them(self):
        s = self.store
        s.upsert_role(dict(s.role("implementer-claude"), permission_mode="bypass"))
        form = {k: v for k, v in s.role("implementer-claude").items() if k in self.FORM}
        saved = s.upsert_role(dict(form, title="Builder"))
        self.assertEqual(saved["title"], "Builder")
        self.assertEqual(saved["channels"], ["#squad", "#impl"])
        self.assertEqual(saved["permission_mode"], "bypass")
        self.assertEqual(s.upsert_role(dict(form, channels=[]))["channels"], [], "an explicit empty list still clears")

    def test_a_channel_typed_without_its_hash_is_stored_with_it(self):
        form = dict(self.store.role("implementer-claude"), channels="squad, #review, review")
        self.assertEqual(self.store.upsert_role(form)["channels"], ["#squad", "#review"])

    def test_a_new_role_still_gets_the_defaults(self):
        r = self.store.upsert_role(dict(id="scribe", system_prompt="Write things down."))
        self.assertEqual((r["channels"], r["permission_mode"], r["provider"]), ([], "auto", "claude"))


class SquadChannelTests(MeshRig):
    PEERS = MeshRig.PEERS + (("mute", 600, "sq-1", []),)       # in sq-1, subscribed to nothing

    def test_squad_reaches_every_member_of_the_senders_squad(self):
        status, _, res = self.send(web.agent_token(TOKEN, "arch"), to="#squad", text="milestone")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.heard(), {"impl", "mute"})

    def test_a_peer_scoped_stream_shows_a_teammates_squad_post(self):
        self.send(web.agent_token(TOKEN, "arch"), to="#squad", text="milestone")
        self.send(web.agent_token(TOKEN, "arch"), to="@squad", text="also")
        self.assertEqual({m["body"] for m in self.store.messages(peer="impl", squad_id="sq-1")}, {"milestone", "also"})

    def test_other_channels_still_reach_only_subscribers(self):
        status, _, res = self.send(web.agent_token(TOKEN, "arch"), to="#impl", text="interface changed")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.heard(), {"impl"})
