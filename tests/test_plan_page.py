"""T-56: planOrder (app.js, between `// plan-page:begin` and `// plan-page:end`), run under node. Skipped without node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")


def run_js(expr):
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// plan-page:begin"):src.index("// plan-page:end")]
    run = subprocess.run([NODE, "-e", part + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


ROWS = "[" + ",".join(f"{{ref:'{r}',parent:{p!r}}}".replace("None", "null") for r, p in (
    ("T-1", None), ("T-7", None), ("T-8", "T-7"), ("T-9", "T-7"), ("T-10", "T-8"), ("T-11", "T-1"), ("T-12", "T-9"))) + "]"


@unittest.skipUnless(NODE, "node is not installed")
class PlanOrderTests(unittest.TestCase):
    def test_parents_before_parts_with_depth_and_only_this_plan(self):
        got = run_js(f"planOrder({ROWS}, 'T-7').map((x) => [x.row.ref, x.depth])")
        self.assertEqual(got, [["T-8", 1], ["T-10", 2], ["T-9", 1], ["T-12", 2]])

    def test_no_slices_and_a_cycle_end(self):
        self.assertEqual(run_js("planOrder([], 'T-7')"), [])
        self.assertEqual(run_js("planOrder([{ref:'T-1',parent:'T-2'},{ref:'T-2',parent:'T-1'}], 'T-1').length"), 1)


if __name__ == "__main__":
    unittest.main()
