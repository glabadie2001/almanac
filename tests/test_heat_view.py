"""The heatmap's zoom window (app.js, between `// heat-view:begin` and `// heat-view:end`), run under node.
Skipped when node is not installed."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")
RANGE = "{start: 1000, bucket_s: 10, n: 100}"          # 1000 .. 2000
DOC = ("{range: " + RANGE + ", overlap: {peak: 2, max_working: Array.from({length: 100}, (_, i) => i)}, "
       "agents: [{work_s: 7, work: Array(100).fill(1), wait: Array(100).fill(0), events: Array(100).fill(2), "
       "output: Array(100).fill(3)}], marks: [{ts: 1005}, {ts: 1500}], messages: [{ts: 1499}, {ts: 1999}]}")


def run_js(expr):
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// heat-view:begin"):src.index("// heat-view:end")]
    run = subprocess.run([NODE, "-e", part + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, encoding="utf-8", timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class HeatViewTests(unittest.TestCase):
    def test_zoom_keeps_the_point_under_the_pointer_and_stays_inside_the_run(self):
        self.assertEqual(run_js(f"heatWindow({RANGE}, null, 0.5, 0.5)"), {"t0": 1250, "t1": 1750})
        self.assertEqual(run_js(f"heatWindow({RANGE}, null, 0.5, 1)"), {"t0": 1500, "t1": 2000})
        self.assertEqual(run_js(f"heatWindow({RANGE}, {{t0: 1250, t1: 1750}}, 1, 0.5, 0.5)"), {"t0": 1500, "t1": 2000})
        self.assertEqual(run_js(f"heatWindow({RANGE}, {{t0: 1500, t1: 2000}}, 1, 0.5, 0.5)"), {"t0": 1500, "t1": 2000},
                         "a pan stops at the end of the run")
        self.assertEqual(run_js(f"heatWindow({RANGE}, {{t0: 1000, t1: 1040}}, 0.5)"), {"t0": 1000, "t1": 1040},
                         "never under 4 buckets")
        self.assertIsNone(run_js(f"heatWindow({RANGE}, {{t0: 1250, t1: 1750}}, 2)"), "zoomed all the way out")
        self.assertIsNone(run_js(f"heatWindow({RANGE}, null, 1, 0.5, 0.5)"), "nothing to pan when all is shown")

    def test_the_view_cuts_the_buckets_and_keeps_the_totals(self):
        whole, cut = run_js(f"[heatView({DOC}, null), heatView({DOC}, {{t0: 1500, t1: 1600}})]")
        self.assertEqual(whole["range"]["n"], 100)
        self.assertEqual((cut["range"]["start"], cut["range"]["end"], cut["range"]["n"]), (1500, 1600, 10))
        self.assertEqual(cut["overlap"]["max_working"], list(range(50, 60)))
        self.assertEqual((len(cut["agents"][0]["work"]), cut["agents"][0]["work_s"], cut["overlap"]["peak"]), (10, 7, 2))
        self.assertEqual((cut["marks"], cut["messages"]), ([{"ts": 1500}], []))
        late = run_js(f"heatView({DOC}, {{t0: 5000, t1: 6000}})")["range"]
        self.assertEqual((late["start"], late["n"]), (1960, 4), "a window past the run is pulled back inside it")


if __name__ == "__main__":
    unittest.main()
