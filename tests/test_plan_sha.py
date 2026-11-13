"""The PLAN commit chip and unmerged-branches line (app.js, between `// plan-sha:begin` and `// plan-sha:end`), run under node.
Skipped when node is not installed."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")
SHA = "51d9343" + "a" * 33


def run_js(expr):
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// plan-sha:begin"):src.index("// plan-sha:end")]
    run = subprocess.run([NODE, "-e", part + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class PlanShaTests(unittest.TestCase):
    def test_chip(self):
        merged, unmerged, unknown, none = run_js(
            f"[true, false, null].map((m) => shaChip({{sha: '{SHA}', merged: m, branch: 'b', base: 'main'}})).concat([shaChip({{sha: null, merged: null}})])")[:4]
        self.assertEqual((merged["text"], merged["cls"]), ("51d9343 merged", "st-accepted"))
        self.assertEqual(unmerged["text"], "51d9343 unmerged")
        self.assertEqual(unknown["text"], "51d9343 ?")
        self.assertIn("could not tell", unknown["title"])
        self.assertIn(SHA, merged["title"])
        self.assertIn("branch b", merged["title"])
        self.assertIsNone(none)

    def test_branches_line(self):
        self.assertEqual(run_js("branchesLine([])"), "")
        self.assertEqual(run_js("branchesLine([{agent_name:'impl-x',ahead:2},{agent_name:'impl-y',ahead:1},{agent_name:'impl-z',ahead:null,error:'boom'}])"),
                         "unmerged: impl-x (2 commits), impl-y (1 commit), impl-z (git: boom)")

    def test_pending(self):
        self.assertFalse(run_js(f"planPending([{{sha:null,merged:null}},{{sha:'{SHA}',merged:true}}])"))
        self.assertTrue(run_js(f"planPending([{{sha:'{SHA}',merged:false}}])"))
        self.assertTrue(run_js(f"planPending([{{sha:'{SHA}',merged:null}}])"))


if __name__ == "__main__":
    unittest.main()
