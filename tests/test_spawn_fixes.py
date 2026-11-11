"""Spawner fixes: mesh reachable from a worktree, worktrees per squad from a snapshot of uncommitted work,
Gemini agents allowed their own mesh tools, and each agent holding its own token."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from support import ROOT, TOKEN, Fixture, runner_probe

from almanac import gemini_host, spawner, store as store_mod, web
from almanac.door import Service, make_handler
from almanac.fleet import Fleet
from almanac.store import Store


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class MeshFromWorktreeTests(Fixture):
    def test_dash_P_keeps_a_worktrees_own_almanac_package_from_shadowing_the_mesh(self):
        shadow = self.root / "worktree"
        (shadow / "almanac").mkdir(parents=True)
        (shadow / "almanac" / "__init__.py").write_text("", encoding="utf-8")   # an older checkout, no mcp_mesh.py
        env = dict(os.environ, PYTHONPATH=str(ROOT))

        def run(*flags):
            return subprocess.run([sys.executable, *flags, "-m", "almanac.mcp_mesh", "--help"], cwd=shadow, env=env,
                                  capture_output=True, text=True).returncode

        self.assertNotEqual(run(), 0, "without -P the worktree's package wins")
        self.assertEqual(run("-P"), 0)

    def test_every_mesh_launch_uses_dash_P_and_names_the_agent(self):
        cfg = spawner.write_claude_mcp_config(self.root / "mcp.json", agent_name="a1", almanac_url="u", token="t", squad_id="sq")
        args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["almanac-mesh"]["args"]
        self.assertEqual(args[:3], ["-P", "-m", "almanac.mcp_mesh"])
        bundle = spawner.stage_agy_mesh_bundle(self.root / "b", agent_name="g1", almanac_url="u", token="t", squad_id="sq")
        server = json.loads((bundle / ".agents/plugins/almanac-mesh/mcp_config.json").read_text(encoding="utf-8"))
        args = server["mcpServers"]["almanac-mesh"]["args"]
        self.assertEqual(args[:3], ["-P", "-m", "almanac.mcp_mesh"])
        self.assertEqual((args[args.index("--self") + 1], args[args.index("--squad") + 1]), ("g1", "sq"))
        charter = spawner.build_mesh_charter(agent_name="a1", provider="claude", role_title="r", role_prompt="p", almanac_url="u")
        self.assertIn("-P -m almanac.mcp_mesh", charter)


class CharterTokenTests(Fixture):
    def test_the_cli_line_never_points_an_agent_at_the_master_token(self):
        state = self.root / "state"
        charter = spawner.build_mesh_charter(agent_name="a1", provider="claude", role_title="r", role_prompt="p",
                                             almanac_url="http://127.0.0.1:8812", state_dir=state)
        self.assertNotIn("--state", charter)
        self.assertNotIn(str(state), charter)
        self.assertNotIn(str(state.resolve()), charter)
        cli_line = charter.split("Shell CLI Fallback", 1)[1].split("\n", 1)[0]
        self.assertNotIn("token", cli_line.replace("your own token", ""), "no token file is named")
        self.assertIn("-P -m almanac.mcp_mesh --self a1 --cli <roster|context|contacts|send|wait|spawn|objects|object|file|log|restart>", charter)


class WorktreeTests(Fixture):
    def setUp(self):
        super().setUp()
        # the temp folder sits inside Almanac's own checkout: stop git there, or a "plain" folder is that repo
        ceiling = mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.root.resolve()))
        ceiling.start()
        self.addCleanup(ceiling.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text("secret.log\n", encoding="utf-8")
        (self.repo / "a.py").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.state = self.root / "state"

    def test_clean_checkout_branches_from_head(self):
        wt = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), self.head)

    def test_worktree_carries_uncommitted_work_without_touching_the_checkout(self):
        (self.repo / "a.py").write_text("two\n", encoding="utf-8")           # tracked, modified
        (self.repo / "new.py").write_text("new\n", encoding="utf-8")         # untracked
        (self.repo / "secret.log").write_text("ignored\n", encoding="utf-8") # ignored
        (self.repo / "staged.py").write_text("s\n", encoding="utf-8")
        git(self.repo, "add", "staged.py")                                    # the operator's own staging
        before = (git(self.repo, "status", "--porcelain"), git(self.repo, "diff", "--cached", "--name-only"),
                  git(self.repo, "stash", "list"), (self.repo / ".git" / "index").read_bytes())

        wt = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")

        after = (git(self.repo, "status", "--porcelain"), git(self.repo, "diff", "--cached", "--name-only"),
                 git(self.repo, "stash", "list"), (self.repo / ".git" / "index").read_bytes())
        self.assertEqual(before, after, "the checkout's status, index and stash are unchanged")
        self.assertEqual((wt / "a.py").read_text(encoding="utf-8"), "two\n")
        self.assertTrue((wt / "new.py").exists() and (wt / "staged.py").exists())
        self.assertFalse((wt / "secret.log").exists())
        self.assertEqual(git(wt, "status", "--porcelain"), "", "the snapshot is committed: the agent's diff is its own")
        self.assertEqual(git(wt, "rev-parse", "HEAD^"), self.head)
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"), "almanac/sq-1/impl")

    def test_worktrees_are_per_squad_and_reused_within_one(self):
        a = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")
        (a / "work.py").write_text("squad one's work\n", encoding="utf-8")
        b = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-2")
        self.assertNotEqual(a, b)
        self.assertFalse((b / "work.py").exists())
        self.assertEqual(spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1"), a)
        solo = spawner.ensure_worktree(self.repo, self.state, "impl")
        self.assertEqual(solo, self.state.resolve() / "worktrees" / "impl")
        self.assertEqual(git(solo, "rev-parse", "--abbrev-ref", "HEAD"), "almanac/impl")

    def test_snapshot_leaves_out_env_files_and_almanacs_own_state(self):
        for rel in (".env", ".env.local", "sub/.env", ".almanac/token", ".almanac/almanac.db", "keep.txt", "sub/app.env.md"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x", encoding="utf-8")
        wt = spawner.ensure_worktree(self.repo, self.repo / ".almanac", "impl", "sq-1")
        self.assertEqual(set(git(wt, "ls-files").splitlines()), {".gitignore", "a.py", "keep.txt", "sub/app.env.md"})

    def test_an_ignored_state_folder_inside_the_repo(self):
        # this repo's own shape: .almanac/ inside the checkout and in .gitignore; naming it, even as an exclude,
        # made `git add` refuse ("The following paths are ignored")
        (self.repo / ".gitignore").write_text("secret.log\n.almanac/\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "ignore the state folder")
        for rel in (".almanac/token", ".almanac/almanac.db"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x", encoding="utf-8")
        (self.repo / "wip.py").write_text("uncommitted\n", encoding="utf-8")
        wt = spawner.ensure_worktree(self.repo, self.repo / ".almanac", "impl", "sq-1")
        self.assertTrue((wt / "wip.py").exists(), "uncommitted work is in the worktree")
        self.assertEqual(set(git(wt, "ls-files").splitlines()), {".gitignore", "a.py", "wip.py"})
        self.assertIn(str(wt.resolve()).replace("\\", "/").lower(), git(self.repo, "worktree", "list").replace("\\", "/").lower())

    def test_a_repo_that_ignores_env_files(self):
        (self.repo / ".gitignore").write_text(".env\n.env.*\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "ignore env")
        for rel in (".env", ".env.local", "sub/.env", "keep.txt"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x", encoding="utf-8")
        wt = spawner.ensure_worktree(self.repo, self.state, "impl", "sq-1")
        self.assertEqual(set(git(wt, "ls-files").splitlines()), {".gitignore", "a.py", "keep.txt"})

    def test_a_git_error_leads_with_its_cause(self):
        raw = ("warning: in the working copy of 'a.py', LF will be replaced by CRLF the next time Git touches it\n" * 6
               + "The following paths are ignored by one of your .gitignore files:\n.almanac\n"
               + "hint: Use -f if you really want to add them.\n")
        cause = spawner._git_cause(raw)
        self.assertTrue(cause.startswith("The following paths are ignored"), cause)
        self.assertIn(".almanac", cause)
        self.assertNotIn("CRLF", cause)
        self.assertLessEqual(len(spawner._git_cause("fatal: " + "x" * 1000)), 241)

    def test_a_repo_with_no_commit_works_in_the_folder(self):
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        self.assertEqual(spawner.ensure_worktree(empty, self.state, "impl", "sq-1"), empty)

    def test_not_a_repo_falls_back_to_the_folder(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(spawner.ensure_worktree(plain, self.state, "impl", "sq-1"), plain)


class GeminiMeshGrantTests(Fixture):
    def test_agy_is_granted_only_the_mesh_tools_and_never_skips_permissions(self):
        allow = json.loads(gemini_host.MESH_GRANTS)["permissions"]["allow"]
        self.assertEqual(allow, [f"mcp(almanac-mesh_almanac-mesh/{t})" for t in
                                 ("mesh_roster", "mesh_context_stream", "mesh_contact_stream", "mesh_send", "mesh_spawn", "mesh_wait",
                              "mesh_objects", "mesh_object", "mesh_file", "mesh_log", "mesh_restart")])
        host = gemini_host.GeminiHost(name="g", cwd=str(self.root), transcript_path=self.root / "t.jsonl",
                                      sessions_dir=self.root / "s", agy_bin="agy", permission_mode="auto",
                                      in_memory_uds=True, quiet=True)
        with mock.patch.object(gemini_host.subprocess, "Popen") as popen:
            host._ensure_stream_proc()
        argv = popen.call_args[0][0]
        self.assertEqual(argv[argv.index("--settings") + 1], gemini_host.MESH_GRANTS)
        self.assertNotIn(" ", gemini_host.MESH_GRANTS, "compact JSON")
        self.assertNotIn("--dangerously-skip-permissions", argv)


class SpawnTokenTests(Fixture):
    def test_each_agent_gets_its_own_token_bundle_and_channels(self):
        state = self.root / "state"
        store = Store(state / "almanac.db")
        self.addCleanup(store.close)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        fleet = Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {})
        service = Service(store, self.projects, 300, fleet=fleet, state=state, claude="claude.exe", gemini="agy.exe",
                          probe=runner_probe(self.root))
        repo = self.root / "ws"
        repo.mkdir()
        launched = []

        def fake_popen(argv, **kwargs):
            launched.append((list(argv), kwargs))
            return mock.MagicMock(pid=7000 + len(launched), **{"poll.return_value": None})

        with mock.patch.object(spawner.subprocess, "Popen", side_effect=fake_popen):
            status, _, res = self.request(make_handler(service, TOKEN), "/api/squads", method="POST", body={
                "name": "sq", "cwd": str(repo), "brief": "b", "members": [
                    {"role_id": "architect-claude", "agent_name": "lead", "workspace_mode": "shared"},
                    {"role_id": "reviewer-gemini", "agent_name": "rev1", "workspace_mode": "shared"},
                    {"role_id": "reviewer-gemini", "agent_name": "rev2", "workspace_mode": "shared"}]})
        self.assertEqual((status, res["launched"]), (200, 3))
        self.assertTrue(service.wake.is_set(), "a launch wakes the transcript loop")
        claude_argv, claude_kw = launched[0]
        mcp = json.loads(Path(claude_argv[claude_argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["almanac-mesh"]["env"]["ALMANAC_TOKEN"], web.agent_token(TOKEN, "lead", claude_argv[claude_argv.index("--session-id") + 1]))
        self.assertEqual(claude_kw["env"]["ALMANAC_TOKEN"], web.agent_token(TOKEN, "lead", claude_argv[claude_argv.index("--session-id") + 1]))
        self.assertNotIn(TOKEN, json.dumps(mcp) + " ".join(claude_argv))
        brief = claude_argv[claude_argv.index("--append-system-prompt") + 1]
        self.assertIn(f"  - Role: {store_mod.REVIEWER}", brief, "every agent's brief says who does what")
        self.assertIn("must NOT edit", store_mod.ARCHITECT)
        bundles = []
        for name, (argv, kw) in zip(("rev1", "rev2"), launched[1:]):
            bundle = Path(argv[argv.index("--add-dir") + 1])
            bundles.append(bundle)
            cfg = json.loads((bundle / ".agents/plugins/almanac-mesh/mcp_config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["mcpServers"]["almanac-mesh"]["env"]["ALMANAC_TOKEN"], web.agent_token(TOKEN, name, argv[argv.index("--session-id") + 1]))
            self.assertEqual(kw["env"]["ALMANAC_TOKEN"], web.agent_token(TOKEN, name, argv[argv.index("--session-id") + 1]))
        self.assertNotEqual(bundles[0], bundles[1], "Gemini peers no longer share one bundle")
        channels = {s["agent_name"]: s["channels"] for s in store.spawned()}
        self.assertEqual(channels["lead"], ["#squad", "#architecture"])
        self.assertEqual(channels["rev1"], ["#squad", "#review"])


class LaunchFailureTests(Fixture):
    """A member that cannot start (here: its worktree) is reported; the members already running are not orphaned."""

    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.store = Store(self.state / "almanac.db")
        self.addCleanup(self.store.close)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        fleet = Fleet(home, table=lambda: [], facts=lambda _: None, lines=lambda _: {})
        self.service = Service(self.store, self.projects, 300, fleet=fleet, state=self.state, claude="claude.exe",
                               gemini="agy.exe", probe=runner_probe(self.root))
        self.handler = make_handler(self.service, TOKEN)
        self.repo = self.root / "ws"
        self.repo.mkdir()
        self.launched = []

        def popen(argv, **kwargs):
            self.launched.append(list(argv))
            return mock.MagicMock(pid=7000 + len(self.launched), **{"poll.return_value": None})

        def worktree(repo, state, name, squad=None):
            if name == "impl":
                raise RuntimeError("worktree for impl: git add failed: The following paths are ignored by one of your .gitignore files: .almanac")
            return repo

        for target, fake in ((spawner.subprocess, "Popen"), (spawner, "ensure_worktree")):
            patcher = mock.patch.object(target, fake, side_effect=popen if fake == "Popen" else worktree)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_other_members_launch_and_the_failure_is_reported_and_logged(self):
        status, _, res = self.request(self.handler, "/api/squads", method="POST", body={
            "name": "sq", "cwd": str(self.repo), "brief": "b", "members": [
                {"role_id": "architect-claude", "agent_name": "lead", "workspace_mode": "shared"},
                {"role_id": "implementer-claude", "agent_name": "impl", "workspace_mode": "worktree"},
                {"role_id": "reviewer-claude", "agent_name": "rev", "workspace_mode": "shared"}]})
        self.assertEqual((status, res["launched"], res["failed"]), (200, 2, 1))
        failed = [a for a in res["squad"]["agents"] if not a["ok"]]
        self.assertEqual([a["agent_name"] for a in failed], ["impl"])
        self.assertIn("The following paths are ignored", failed[0]["error"])
        self.assertEqual(sorted(s["agent_name"] for s in self.store.spawned()), ["lead", "rev"])
        [entry] = [a for a in self.store.actions() if a["action"] == "squad"]
        self.assertIn("impl failed: worktree for impl", entry["detail"])

    def test_a_single_spawn_that_cannot_start_is_a_409_with_the_cause(self):
        status, _, res = self.request(self.handler, "/api/spawn", method="POST", body={
            "cwd": str(self.repo), "role_id": "implementer-claude", "agent_name": "impl", "workspace_mode": "worktree"})
        self.assertEqual(status, 409)
        self.assertIn("The following paths are ignored", res["error"])
        self.assertEqual(self.launched, [])
