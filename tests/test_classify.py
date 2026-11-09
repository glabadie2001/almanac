import unittest

import support  # noqa: F401  (path + sandbox)

from almanac import classify


class FamilyTests(unittest.TestCase):
    def test_families(self):
        cases = {
            'cd "/d/x" && grep -rn foo src | head': "search",
            "rg -n -g '*.py' foo .": "search",
            "sed -n '10,40p' server.py": "range read",
            "cat README.md | head -20": "read",
            "git status --short": "git",
            "python -m unittest discover tests 2>&1 | tail -3": "tests",
            "rt --recent 30": "tests",
            "python -m covenant check 2>&1 | tail -1": "covenant",
            "npm run build 2>&1 | tail -4": "build",
            "curl -s http://127.0.0.1:8781/ident": "http",
            "door bank /api/status": "http",
            "swap a.py <<'EOF'\n<<<<\na\n====\nb\n>>>>\nEOF": "edit",
            "python - <<'EOF'\nfrom pathlib import Path\np=Path('a'); p.write_text(p.read_text().replace('a','b'))\nEOF": "edit",
            "python - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('http://127.0.0.1:1')\nEOF": "http",
            "python - <<'EOF'\nprint(1)\nEOF": "python inline",
            'python -c "print(1)"': "python inline",
            "python tools/sprites/px.py --x": "python script",
            "ls -la && wc -l *.py": "list",
            "taskkill /PID 4 /F": "process",
            "echo hi": "other",
        }
        for cmd, want in cases.items():
            self.assertEqual(classify.family(cmd), want, cmd)
        self.assertTrue(set(classify.family(c) for c in cases) <= set(classify.FAMILIES))

    def test_head_skips_cd_and_env(self):
        self.assertEqual(classify.head('cd "/d/$Coding Projects/x" && PYTHONIOENCODING=utf-8 python -m x'), "python")
        self.assertEqual(classify.head("/c/Users/G/.venv/Scripts/python.exe -m pytest"), "python")
        self.assertEqual(classify.head(""), "other")


class AdoptionTests(unittest.TestCase):
    def test_new_old_both(self):
        a = classify.adoption
        self.assertEqual(a("rg -n foo src")["search"], 2)
        self.assertEqual(a("grep -rn foo src")["search"], 1)
        self.assertEqual(a("git log | grep fix")["search"], 0, "a piped grep filters output, it is not a search")
        self.assertEqual(a("rg foo . ; grep -n bar f.py")["search"], 3)
        self.assertEqual(a("rt")["tests"], 2)
        self.assertEqual(a("python -m covenant test --dry")["tests"], 2)
        self.assertEqual(a("python -m unittest discover tests")["tests"], 1)
        self.assertEqual(a("npx vitest run 2>&1 | tail")["tests"], 1)
        self.assertEqual(a("npx vitest run src/pages/Atelier.test.jsx")["tests"], 0, "a targeted run is fine already")
        self.assertEqual(a("door bank /ident")["http"], 2)
        self.assertEqual(a("curl -s http://127.0.0.1:8781/ident")["http"], 1)
        self.assertEqual(a("curl -s https://example.com")["http"], 0)
        self.assertEqual(a("swap f.py <<'EOF'\n<<<<\na\n====\nb\n>>>>\nEOF")["edit"], 2)
        self.assertEqual(a("python - <<'EOF'\np.write_text(p.read_text().replace('a','b'))\nEOF")["edit"], 1)
        self.assertEqual(a("cat > notes.md <<'EOF'\nhi\nEOF")["edit"], 1)


if __name__ == "__main__":
    unittest.main()
