"""A role's `add_dirs`: extra workspace folders for a Gemini agent (agy `--add-dir`). Set by the operator on the
role, refused unless absolute and existing, passed to Gemini spawns only (minus any deleted since), never nameable
by an agent."""
import os
from pathlib import Path

from support import runner_probe
from test_elastic import TASK, ElasticRig

from almanac import spawner


class AddDirsTests(ElasticRig):
    def role(self, **extra):
        doc = dict(self.store.role("reviewer-gemini"), **extra)
        return self.request(self.handler, "/api/roles", method="POST", body=doc)

    def test_the_role_round_trips_and_absent_keeps_it(self):
        a, b = self.root / "a", self.root / "b"
        a.mkdir()
        b.mkdir()
        status, _, res = self.role(add_dirs=f"{a}\n\n{b}\n{b}{os.sep}\n{b}{os.sep}..{os.sep}b")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["role"]["add_dirs"], [str(a.resolve()), str(b.resolve())])
        role = dict(self.store.role("reviewer-gemini"))
        role.pop("add_dirs")
        self.assertEqual(self.store.upsert_role(role)["add_dirs"], [str(a.resolve()), str(b.resolve())], "left out keeps it")
        self.assertEqual(self.store.upsert_role(dict(role, add_dirs=[]))["add_dirs"], [])
        self.assertEqual(self.store.role("implementer-claude")["add_dirs"], [], "a new column starts empty")

    def test_the_door_refuses_a_relative_or_missing_folder_and_names_it(self):
        missing = self.root / "nope"
        status, _, res = self.role(add_dirs=["relative/dir", str(missing)])
        self.assertEqual(status, 400, res)
        self.assertIn("relative/dir", res["error"])
        self.assertIn(str(missing), res["error"])
        self.assertEqual(self.store.role("reviewer-gemini")["add_dirs"], [], "nothing saved")

    def test_a_gemini_spawn_carries_the_folders_after_the_bundle(self):
        extra = self.root / "docs"
        extra.mkdir()
        self.assertEqual(self.role(add_dirs=[str(extra)])[0], 200)
        self.service.probe = runner_probe(self.root, claude=True, gemini=True)
        self.service._runners = None
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST",
                                      body={"squad_id": "sq-1", "role_id": "reviewer-gemini", "task": TASK})
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertEqual(dirs[1:], [str(extra.resolve())], "the role's folders follow the mesh bundle")
        self.assertNotEqual(dirs[0], str(extra.resolve()))

    def test_quotes_are_stripped_and_more_than_twenty_are_refused(self):
        a = self.root / "a"
        a.mkdir()
        status, _, res = self.role(add_dirs=f'"{a}"')
        self.assertEqual((status, res["role"]["add_dirs"]), (200, [str(a.resolve())]), res)
        status, _, res = self.role(add_dirs=[str(a)] * 21)
        self.assertEqual(status, 400, res)
        self.assertIn("at most 20 folders, not 21", res["error"])

    def test_a_folder_deleted_since_is_left_out_of_argv_and_charter(self):
        keep, gone = self.root / "keep", self.root / "gone"
        keep.mkdir()
        gone.mkdir()
        self.assertEqual(self.role(add_dirs=[str(keep), str(gone)])[0], 200)
        gone.rmdir()
        self.service.probe = runner_probe(self.root, claude=True, gemini=True)
        self.service._runners = None
        status, _, res = self.request(self.handler, "/api/squad/member", method="POST",
                                      body={"squad_id": "sq-1", "role_id": "reviewer-gemini", "task": TASK})
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"][1:], [str(keep.resolve())])
        charter = Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
        self.assertIn(str(keep.resolve()), charter)
        self.assertNotIn(str(gone.resolve()), charter)

    def test_the_charter_names_the_folders_for_gemini_only(self):
        kw = dict(agent_name="r", role_title="R", role_prompt="p", almanac_url="u", add_dirs=["C:\\docs"])
        self.assertIn("these folders are also part of your workspace: `C:\\docs`.", spawner.build_mesh_charter(provider="gemini", **kw))
        self.assertNotIn("C:\\docs", spawner.build_mesh_charter(provider="claude", **kw))

    def test_an_agent_cannot_name_folders(self):
        self.store.update_squad("sq-1", spawn_role="reviewer-gemini")
        self.service.probe = runner_probe(self.root, claude=True, gemini=True)
        self.service._runners = None
        status, _, res = self.spawn(add_dirs=[str(self.root)])
        self.assertEqual(status, 200, res)
        argv = self.argv[-1]
        self.assertEqual(argv.count("--add-dir"), 1, "only the mesh bundle: the body's add_dirs is ignored")
        self.assertNotIn(str(self.root.resolve()), [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"])
