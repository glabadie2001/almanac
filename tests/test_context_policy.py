"""Role `context_policy` (T-21): stored, validated, defaulted, kept by a save that omits it, carried by the roster."""
import sqlite3

from support import Fixture

from almanac import mcp_mesh
from almanac.store import Store, parse_context_policy

DEFAULTS = {"orchestrator-claude": "compact-at-300000", "architect-claude": "compact-at-300000",
            "architect-gemini": "compact-at-300000", "implementer-claude": "fresh-per-task",
            "implementer-gemini": "fresh-per-task", "reviewer-claude": "none", "reviewer-gemini": "none"}


class ContextPolicyTests(Fixture):
    def store_at(self, name="a.db"):
        s = Store(self.root / name)
        self.addCleanup(s.close)
        return s

    def test_a_new_database_seeds_the_defaults(self):
        self.assertEqual({r["id"]: r["context_policy"] for r in self.store_at().roles()}, DEFAULTS)

    def test_an_old_database_gets_the_defaults_once_and_then_keeps_the_operators_choice(self):
        path = self.root / "old.db"
        Store(path).close()
        db = sqlite3.connect(path)
        db.execute("alter table roles drop column context_policy")
        db.execute("insert into roles (id, title, provider, system_prompt) values ('custom', 'C', 'claude', 'p')")
        db.commit()
        db.close()
        s = Store(path)
        self.assertEqual({r["id"]: r["context_policy"] for r in s.roles()}, {**DEFAULTS, "custom": "none"},
                         "shipped roles by id; any other role: none")
        s.upsert_role(dict(s.role("reviewer-gemini"), context_policy="fresh-per-task"))
        s.close()
        s = self.store_at("old.db")
        self.assertEqual(s.role("reviewer-gemini")["context_policy"], "fresh-per-task", "a restart overwrites nothing")

    def test_accepted_forms_are_stored_in_integer_form(self):
        s = self.store_at()
        base = s.role("reviewer-claude")
        for given, stored in (("none", "none"), ("fresh-per-task", "fresh-per-task"), (" Fresh-Per-Task ", "fresh-per-task"),
                              ("compact-at-250000", "compact-at-250000"), ("compact-at-300k", "compact-at-300000"),
                              ("COMPACT-AT-1K", "compact-at-1000")):
            self.assertEqual(s.upsert_role(dict(base, context_policy=given))["context_policy"], stored, given)

    def test_other_forms_are_refused_naming_the_three(self):
        s = self.store_at()
        base = s.role("reviewer-claude")
        for bad in ("", "always", "fresh", "compact", "compact-at-", "compact-at-0", "compact-at-0k", "compact-at-3.5k",
                    "compact-at--5", "compact-at-300m", "compact-at-300 k", 5, "compact-at-" + chr(0x663) + chr(0x660) + chr(0x660)):     # Arabic-Indic digits
            with self.assertRaises(ValueError, msg=repr(bad)) as ctx:
                s.upsert_role(dict(base, context_policy=bad))
            for form in ("none", "fresh-per-task", "compact-at-"):
                self.assertIn(form, str(ctx.exception))
        self.assertEqual(s.role("reviewer-claude")["context_policy"], "none", "a refused save changes nothing")

    def test_a_save_that_does_not_mention_it_keeps_it(self):
        s = self.store_at()
        base = {k: v for k, v in s.role("architect-claude").items() if k != "context_policy"}
        self.assertEqual(s.upsert_role(dict(base, title="Arch"))["context_policy"], "compact-at-300000")
        self.assertEqual(s.upsert_role(dict(base, context_policy=None))["context_policy"], "compact-at-300000", "null keeps it")
        s.upsert_role(dict(base, context_policy="compact-at-500k"))
        self.assertEqual(s.upsert_role(base)["context_policy"], "compact-at-500000")
        self.assertEqual(s.upsert_role(dict(id="new-role", title="N", provider="claude", system_prompt="p"))["context_policy"],
                         "none", "a new role starts at none")

    def test_parse_context_policy(self):
        for value, parsed in (("none", ("none", None)), ("fresh-per-task", ("fresh", None)), ("", ("none", None)),
                              (None, ("none", None)), ("compact-at-300000", ("compact", 300000)),
                              ("compact-at-300k", ("compact", 300000))):
            self.assertEqual(parse_context_policy(value), parsed, value)
        with self.assertRaises(ValueError):
            parse_context_policy("sometimes")

    def test_the_roster_carries_it(self):
        doc = {"roles": self.store_at().roles(), "peers": [], "squads": [], "open": []}
        client = mcp_mesh.MeshClient(url="http://x", token="t", self_name="arch", api_fn=lambda path, **kw: doc)
        self.assertEqual({r["id"]: r["context_policy"] for r in client.call_tool("mesh_roster")["roles"]}, DEFAULTS)
