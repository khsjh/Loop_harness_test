"""CLI compatibility and JSON output checks in isolated temporary directories."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from modules.output_format import render_paths


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        for name in ("b.txt", "a.txt", "sub/c.txt", "한글.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", "-S", "-m", "modules.file_cli", *map(str, args)],
                              capture_output=True, text=True, encoding="utf-8", timeout=10)

    def test_legacy_text(self):
        result = self.invoke(self.root, "b.txt", "a.txt", "b.txt", "missing")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "a.txt\nb.txt\n", ""))

    def test_legacy_empty_output(self):
        result = self.invoke(self.root)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_exclusion(self):
        result = self.invoke("--exclude", "sub", self.root, "sub/c.txt", "a.txt")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "a.txt\n", ""))

    def test_invalid_path_error(self):
        result = self.invoke(self.root, "../outside")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_bad_root_error(self):
        result = self.invoke(self.root / "missing", "a.txt")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_help(self):
        result = self.invoke("--help")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("usage:", result.stdout)
        self.assertIn("--exclude", result.stdout)

    def test_legacy_formatter(self):
        paths = ["a.txt", "한글.txt"]
        self.assertEqual(render_paths(paths), "a.txt\n한글.txt\n")
        self.assertEqual(paths, ["a.txt", "한글.txt"])
        self.assertEqual(render_paths([]), "")

    def test_json_sorted_unique_unicode(self):
        result = self.invoke("--json", self.root, "한글.txt", "b.txt", "a.txt", "b.txt")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, '{"files":["a.txt","b.txt","한글.txt"],"count":3}\n')
        parsed = json.loads(result.stdout)
        self.assertIs(type(parsed["count"]), int)

    def test_json_empty(self):
        result = self.invoke("--json", self.root)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, '{"files":[],"count":0}\n', ""))

    def test_json_exclusions_and_skipped_symlink(self):
        (self.root / "alias").symlink_to(self.root / "a.txt")
        result = self.invoke("--json", "--exclude", "sub", "--exclude", "b.txt",
                             self.root, "sub/c.txt", "b.txt", "alias", "a.txt")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, '{"files":["a.txt"],"count":1}\n', ""))

    def test_json_validation_error_is_not_partial_json(self):
        result = self.invoke("--json", self.root, "a.txt", "../outside")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_json_formatter_direct_call(self):
        paths = ["a.txt", "한글.txt"]
        self.assertEqual(render_paths(paths, as_json=True), '{"files":["a.txt","한글.txt"],"count":2}\n')
        self.assertEqual(paths, ["a.txt", "한글.txt"])
        self.assertEqual(render_paths([], as_json=True), '{"files":[],"count":0}\n')
        self.assertEqual(render_paths(paths, as_json=False), "a.txt\n한글.txt\n")

    def test_unknown_option_is_usage_error(self):
        result = self.invoke("--unknown", self.root)
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("error:", result.stderr)

    def test_help_documents_json_option(self):
        result = self.invoke("--help")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
