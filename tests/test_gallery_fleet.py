"""The gallery's fleet rows: a spawned agent's worktree reaches /api/fleet so its tile can badge it."""
import json

from support import TOKEN, Fixture

from almanac.door import Service, make_handler
from almanac.store import Store


class GalleryFleetTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "almanac.db")
        self.addCleanup(self.store.close)
        from test_fleet import FakeMachine
        from almanac.fleet import Fleet
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "100.json").write_text(json.dumps({"pid": 100, "sessionId": "s", "status": "idle", "name": "one"}), encoding="utf-8")
        machine = FakeMachine()
        self.service = Service(self.store, self.projects, 300,
                               fleet=Fleet(home, table=machine.table, facts=machine.facts, lines=machine.lines))
        self.handler = make_handler(self.service, TOKEN)

    def spawn(self, worktree_path, **kw):
        args = dict(agent_name="one", session_id="s", squad_id="sq-1", role_id="implementer", provider="claude",
                    model=None, effort=None, cwd=str(worktree_path or self.root), worktree_path=worktree_path, pid=100)
        args.update(kw)
        self.store.record_spawn(**args)

    def row(self, pid=100):
        status, _, doc = self.request(self.handler, "/api/fleet")
        self.assertEqual(status, 200)
        return next(p for p in doc["processes"] if p["pid"] == pid)

    def test_worktree_path_and_its_repo_reach_the_fleet_row(self):
        repo = self.root / "repo"
        wt = self.root / "state" / "worktrees" / "sq-1" / "one"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {(repo / '.git' / 'worktrees' / 'one').as_posix()}\n", encoding="utf-8")
        self.spawn(str(wt))
        p = self.row()
        self.assertEqual(p["worktree_path"], str(wt))
        self.assertEqual(p["worktree_of"], str(repo))
        self.assertEqual(p["squad_id"], "sq-1")
        self.assertEqual(self.row()["worktree_of"], str(repo), "a second poll keeps it")

    def test_worktree_without_a_readable_git_file_still_carries_the_path(self):
        wt = self.root / "gone-worktree"
        self.spawn(str(wt))
        p = self.row()
        self.assertEqual(p["worktree_path"], str(wt))
        self.assertIsNone(p["worktree_of"])

    def test_shared_folder_spawn_has_no_worktree(self):
        self.spawn(None)
        p = self.row()
        self.assertIsNone(p.get("worktree_path"))
        self.assertNotIn("worktree_of", p)

    def test_unspawned_session_is_untouched(self):
        p = self.row()
        self.assertIsNone(p.get("worktree_path"))
        self.assertNotIn("worktree_of", p)
