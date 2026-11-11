"""The markdown subset the UI renders (app.js, between `// md:begin` and `// md:end`), run under node: what agents
write, hostile input, and input that would hang a backtracking parser. Skipped when node is not installed."""
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "almanac" / "static" / "app.js"
NODE = shutil.which("node")


def parse(*texts):
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// md:begin"):src.index("// md:end")]
    script = part + ("\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                     "\nprocess.stdout.write(JSON.stringify(input.map((t) => mdParse(t))));")
    run = subprocess.run([NODE, "-e", script], input=json.dumps(texts), capture_output=True, encoding="utf-8",
                         timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    out = json.loads(run.stdout)
    return out if len(texts) > 1 else out[0]


def timed(*texts):
    """Milliseconds node spends parsing each text, measured inside node (its start-up is not counted)."""
    src = APP.read_text(encoding="utf-8")
    part = src[src.index("// md:begin"):src.index("// md:end")]
    script = part + ("\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                     "\nprocess.stdout.write(JSON.stringify(input.map((t) => { const t0 = performance.now(); mdParse(t);"
                     " return performance.now() - t0; })));")
    run = subprocess.run([NODE, "-e", script], input=json.dumps(texts), capture_output=True, encoding="utf-8",
                         timeout=60)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


def walk(node):
    """Every dict in a parsed tree."""
    if isinstance(node, list):
        for n in node:
            yield from walk(n)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)


def text_of(tree):
    return "".join(n.get("v", "") + (n.get("text", "") if n.get("t") == "code" else "") for n in walk(tree))


def kinds(tree):
    return [n["t"] for n in walk(tree) if "t" in n]


@unittest.skipUnless(NODE, "node is not installed")
class MarkdownTests(unittest.TestCase):
    def test_blocks_agents_write(self):
        fence, heads, table, nested, ordered, quote, rule = parse(
            "```python\nx = 1\n  y = 2\n```\nafter",
            "# One\n#### Four ##\n##### five is text",
            "| Part | Owner | State |\n|---|---|---|\n| S1-view | impl-plan | `0dab3b3` |",
            "- a\n  - b\n    - c\n- d",
            "1. first\n2. second\n\n3. third",
            "> quoted\n> > twice",
            "above\n\n---\n\nbelow",
        )
        self.assertEqual(fence[0], {"t": "code", "lang": "python", "text": "x = 1\n  y = 2"})
        self.assertEqual(fence[1]["t"], "p")
        self.assertEqual([(b["t"], b.get("level")) for b in heads], [("h", 1), ("h", 4), ("p", None)])
        self.assertEqual(text_of(heads[1]), "Four")
        self.assertEqual(table["t"] if isinstance(table, dict) else table[0]["t"], "table")
        self.assertEqual([text_of(c) for c in table[0]["head"]], ["Part", "Owner", "State"])
        self.assertEqual(table[0]["rows"][0][2], [{"t": "code", "v": "0dab3b3"}])
        top = nested[0]
        self.assertEqual([text_of(i["c"]) for i in top["items"]], ["a", "d"])
        self.assertEqual(text_of(top["items"][0]["sub"][0]["items"][0]["c"]), "b")
        self.assertEqual(text_of(top["items"][0]["sub"][0]["items"][0]["sub"][0]["items"][0]["c"]), "c")
        self.assertEqual(len(ordered), 1, "a blank line between items keeps one list")
        self.assertEqual([i["marker"] for i in ordered[0]["items"]], ["1.", "2.", "3."])
        self.assertTrue(ordered[0]["ordered"])
        self.assertEqual(quote[0]["t"], "quote")
        self.assertEqual(quote[0]["c"][1]["t"], "quote")
        self.assertEqual([b["t"] for b in rule], ["p", "hr", "p"])

    def test_inline_agents_write(self):
        label, paths, breaks, link = parse(
            "**Files:** `almanac/static/app.js` and *one* more",
            "touch my_file_name.py and snake_case_names here",
            "line one\nline two",
            "see [the spec](https://example.com/a?b=1) now",
        )
        self.assertEqual(kinds(label[0]["c"]), ["b", "text", "text", "code", "text", "i", "text", "text"])
        self.assertEqual(kinds(paths), ["p", "text"], "underscores inside words stay literal")
        self.assertEqual(kinds(breaks[0]["c"]), ["text", "br", "text"])
        a = [n for n in walk(link) if n.get("t") == "a"][0]
        self.assertEqual(a["href"], "https://example.com/a?b=1")

    def test_hostile_input_is_text(self):
        script, img, js, data, image, vb = parse(
            "<script>alert(1)</script>",
            '<img src=x onerror="alert(1)">',
            "[click](javascript:alert(1))",
            "[x](data:text/html,<script>alert(1)</script>)",
            "![pic](https://example.com/p.png) and ![bad](javascript:alert(1))",
            "[v](vbscript:msgbox) [f](file:///etc/passwd) [rel](/api/kill)",
        )
        self.assertEqual(text_of(script), "<script>alert(1)</script>")
        self.assertEqual(text_of(img), '<img src=x onerror="alert(1)">')
        for tree, raw in ((js, "[click](javascript:alert(1))"), (vb, "[v](vbscript:msgbox) [f](file:///etc/passwd) [rel](/api/kill)")):
            self.assertNotIn("a", kinds(tree))
            self.assertEqual(text_of(tree), raw, "refused links are shown as written, never dropped")
        self.assertNotIn("a", kinds(data))
        links = [n for n in walk(image) if n.get("t") == "a"]
        self.assertEqual([a["href"] for a in links], ["https://example.com/p.png"], "an image is a link; a bad one is text")
        self.assertIn("![bad](javascript:alert(1))", text_of(image))
        self.assertTrue(all(set(n) <= {"t", "c", "v", "href", "text", "lang", "level", "ordered", "items", "marker",
                                       "sub", "head", "rows"} for n in walk([script, img, js, data, image, vb])))

    def test_broken_input_is_kept(self):
        fence, ragged, unclosed = parse(
            "```\nno closing fence\n# not a heading",
            "| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |",
            "**bold never closes and `code never closes and [link never",
        )
        prose = "See [spec](https://example.com/doc and remember (important) to run tests"
        swallowed = parse(prose)
        self.assertNotIn("a", kinds(swallowed), "a target with a space is not a link")
        self.assertEqual(text_of(swallowed), prose, "nothing up to the later ')' is dropped")
        self.assertEqual(fence, [{"t": "code", "lang": "", "text": "no closing fence\n# not a heading"}])
        self.assertEqual([len(r) for r in ragged[0]["rows"]], [2, 3], "short rows padded, long rows keep their cells")
        self.assertEqual(text_of(ragged[0]["rows"][1][2]), "3")
        self.assertEqual(text_of(unclosed), "**bold never closes and `code never closes and [link never")

    def test_pathological_input_does_not_hang(self):
        start = time.monotonic()
        rule, stars, quotes, brackets, ticks, unders = parse("*" * 5000, "a " + "*" * 5000, ">" * 2000 + " deep",
                                                             "[" * 5000, "a " + "`" * 3000 + "x", "_a" * 3000)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(kinds(rule), ["hr"], "a line of stars alone is a rule")
        self.assertEqual((kinds(stars), text_of(stars)), (["p", "text"], "a " + "*" * 5000), "a run of stars is text")
        self.assertIn("deep", text_of(quotes))
        self.assertLessEqual(sum(1 for n in walk(quotes) if n.get("t") == "quote"), 8, "nesting stops at the limit")
        self.assertEqual(text_of(brackets), "[" * 5000)
        self.assertEqual(text_of(ticks), "a " + "`" * 3000 + "x")
        self.assertEqual(text_of(unders), "_a" * 3000)

    def test_bracket_runs_are_linear(self):
        texts = ("[" * 20000 + "]", "[" * 5000 + "](not-a-url)", "[" * 100000 + "]")
        ms = timed(*texts)
        self.assertLess(max(ms), 1000, f"parse times {ms} ms")   # a hang guard: about 5 ms at 100,000 brackets
        for text, tree in zip(texts, parse(*texts)):
            self.assertNotIn("a", kinds(tree))
            self.assertEqual(text_of(tree), text, "every bracket stays as written")


if __name__ == "__main__":
    unittest.main()
