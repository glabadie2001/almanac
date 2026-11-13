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

    def test_a_nested_plan_and_its_slices_are_left_out(self):
        got = run_js("planOrder([{ref:'T-8',parent:'T-7',data:{}},{ref:'T-20',parent:'T-7',data:{plan:{}}},"
                     "{ref:'T-21',parent:'T-20',data:{}}], 'T-7').map((x) => [x.row.ref, x.depth])")
        self.assertEqual(got, [["T-8", 1]])

    def test_no_slices_and_a_cycle_end(self):
        self.assertEqual(run_js("planOrder([], 'T-7')"), [])
        self.assertEqual(run_js("planOrder([{ref:'T-1',parent:'T-2'},{ref:'T-2',parent:'T-1'}], 'T-1').length"), 1)


class PlanNoteTests(unittest.TestCase):
    """door.plan_note: the kickoff line only for a task under a plan the operator accepted; it rides in the head."""

    def rows(self, status="accepted", ts=5.0):
        plan = {"id": 1, "kind": "task", "parent_id": None, "status": status, "data": {"plan": {"accepted_ts": ts} if ts else {}}}
        return {1: plan, 2: {"id": 2, "kind": "task", "parent_id": 1, "status": "queued", "data": {}},
                3: {"id": 3, "kind": "task", "parent_id": 2, "status": "queued", "data": {}},
                4: {"id": 4, "kind": "task", "parent_id": None, "status": "queued", "data": {}}}

    def note(self, rows, i):
        from types import SimpleNamespace
        from almanac import door
        return door.plan_note(SimpleNamespace(store=SimpleNamespace(object_row=rows.get)), rows[i])

    def test_line_for_a_slice_under_an_accepted_plan_only(self):
        rows = self.rows()
        self.assertEqual(self.note(rows, 2), "Plan T-1, accepted by the operator: build to this task's Sketch.")
        self.assertEqual(self.note(rows, 3), self.note(rows, 2))
        self.assertEqual(self.note(rows, 1), "")           # the plan itself
        self.assertEqual(self.note(rows, 4), "")           # no plan
        self.assertEqual(self.note(self.rows("in review"), 2), "")
        self.assertEqual(self.note(self.rows("in progress"), 2), "")
        self.assertEqual(self.note(self.rows(ts=None), 2), "")

    def test_kickoff_head_carries_it_when_room_is_tight(self):
        from almanac import door
        row = dict(id=2, kind="task", title="t", body="b", files=[], status="queued", parent_id=1)
        out = door.task_kickoff(row, [], {"name": "s", "brief": ""}, 10, "TAIL", plan="PLANLINE")
        self.assertIn("PLANLINE", out)
        self.assertNotIn("PLANLINE", door.task_kickoff(row, [], {"name": "s", "brief": ""}, 10, "TAIL"))


if __name__ == "__main__":
    unittest.main()
