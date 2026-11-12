"""The squad launch form's per-role row defaults (app.js, between `// member-defaults:begin` and `// member-defaults:end`),
run under node. Skipped when node is not installed."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")


def defaults(*calls):
    """memberDefaults(role, used) for each (role, used) pair."""
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// member-defaults:begin"):src.index("// member-defaults:end")]
    script = part + ("\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                     "\nprocess.stdout.write(JSON.stringify(input.map(([r, u]) => memberDefaults(r, u))));")
    run = subprocess.run([NODE, "-e", script], input=json.dumps(calls), capture_output=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class MemberDefaultsTests(unittest.TestCase):
    def test_handle_prefixes(self):
        out = defaults(*[({"id": f"{p}-x", "provider": "claude"}, []) for p in ("architect", "implementer", "reviewer", "orchestrator")],
                       ({"id": "scribe-gemini", "provider": "gemini"}, []))
        self.assertEqual([d["handle"] for d in out],
                         ["arch-claude", "impl-claude", "rev-claude", "orch-claude", "scribe-gemini"])
        self.assertEqual(defaults(({"id": "documentation-x", "provider": "claude"}, []))[0]["handle"], "docume-claude")

    def test_taken_handle_is_numbered(self):
        role = {"id": "implementer-gemini", "provider": "gemini"}
        out = defaults((role, ["impl-gemini"]), (role, ["impl-gemini", "impl-gemini-2"]), (role, ["other"]))
        self.assertEqual([d["handle"] for d in out], ["impl-gemini-2", "impl-gemini-3", "impl-gemini"])

    def test_values_come_from_the_role(self):
        full, bare, med = defaults(
            ({"id": "a-claude", "provider": "claude", "model": "opus-5-5", "effort": "high", "workspace_mode": "worktree"}, []),
            ({"id": "a-claude", "provider": "claude"}, []),
            ({"id": "a-claude", "provider": "claude", "effort": "med"}, []))
        self.assertEqual((full["model"], full["effort"], full["workspace_mode"]), ("opus-5-5", "high", "worktree"))
        self.assertEqual((bare["model"], bare["effort"], bare["workspace_mode"]), ("", "", "shared"))
        self.assertEqual(med["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
