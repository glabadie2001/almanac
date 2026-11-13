"""The PLAN decide bar's pure part (app.js, between `// plan-decide:begin` and `// plan-decide:end`), run under node.
Skipped when node is not installed."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")


def run_js(expr):
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// plan-decide:begin"):src.index("// plan-decide:end")]
    run = subprocess.run([NODE, "-e", part + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class PlanDecideTests(unittest.TestCase):
    def test_accept(self):
        self.assertEqual(run_js("decideBody('accepted', '')"), {"status": "accepted", "body": ""})
        self.assertEqual(run_js("decideBody('accepted', 'good')"), {"status": "accepted", "body": "good"})

    def test_send_back(self):
        self.assertEqual(run_js("decideBody('in progress', ' fix X ')"), {"status": "in progress", "body": "fix X"})

    def test_send_back_needs_comment(self):
        for c in ("''", "'  '", "undefined"):
            self.assertEqual(run_js(f"decideBody('in progress', {c})"), {"error": "say what to change"})


if __name__ == "__main__":
    unittest.main()
