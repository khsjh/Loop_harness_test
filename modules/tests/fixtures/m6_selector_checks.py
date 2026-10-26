"""Independent filesystem cases; all writable fixtures live in private /tmp."""
import os
from pathlib import Path
import tempfile
import unittest

from modules.file_selector import select_files


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        for name in ("z.txt", "a.txt", "nested/b.txt", "skip/x.txt", "skipmore/y.txt", "한글.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside fixture")

    def test_empty(self):
        result = select_files(self.root, [])
        self.assertIs(type(result), list)
        self.assertEqual(result, [])

    def test_sorted_unique_regular_files_only(self):
        candidates = ["z.txt", "nested/b.txt", "a.txt", "nested", "missing", "a.txt", "한글.txt"]
        self.assertEqual(select_files(str(self.root), candidates),
                         ["a.txt", "nested/b.txt", "z.txt", "한글.txt"])

    def test_exclusions_use_complete_path_components(self):
        self.assertEqual(select_files(self.root,
            ["skip/x.txt", "skipmore/y.txt", "nested/b.txt", "a.txt"],
            ["skip", "nested/b.txt", "skip"]), ["a.txt", "skipmore/y.txt"])

    def test_invalid_candidate_paths(self):
        invalid = ["", ".", "..", "../outside/secret.txt", "nested/../a.txt", "/a.txt",
                   str(self.outside / "secret.txt"), "./a.txt", "nested//b.txt", "nested/",
                   "nested\\b.txt", "a\x00.txt", 1, None, Path("a.txt")]
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                select_files(self.root, [path])

    def test_invalid_excluded_paths(self):
        for path in ("", ".", "..", "../outside", "/a.txt", "./skip", "skip/", "a//b",
                     "a\\b", "a\x00b", None, 1):
            with self.subTest(path=path), self.assertRaises(ValueError):
                select_files(self.root, [], [path])

    def test_invalid_paths_are_validated_even_if_excluded_or_missing(self):
        with self.assertRaises(ValueError):
            select_files(self.root, ["skip/../a.txt"], ["skip"])
        with self.assertRaises(ValueError):
            select_files(self.root, ["missing/../a.txt"])

    def test_reject_non_iterables_and_string_containers(self):
        for value in (None, 1, True, "a.txt", b"a.txt"):
            with self.subTest(argument="candidates", value=value), self.assertRaises(ValueError):
                select_files(self.root, value)
            with self.subTest(argument="excluded", value=value), self.assertRaises(ValueError):
                select_files(self.root, [], value)

    def test_file_symlinks_inside_outside_and_broken_are_skipped(self):
        (self.root / "inside-link").symlink_to(self.root / "a.txt")
        (self.root / "outside-link").symlink_to(self.outside / "secret.txt")
        (self.root / "broken-link").symlink_to(self.base / "does-not-exist")
        self.assertEqual(select_files(self.root,
            ["inside-link", "outside-link", "broken-link", "a.txt"]), ["a.txt"])

    def test_symlink_directory_components_are_skipped(self):
        (self.root / "internal-dir").symlink_to(self.root / "nested", target_is_directory=True)
        (self.root / "external-dir").symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(select_files(self.root,
            ["internal-dir/b.txt", "external-dir/secret.txt", "nested/b.txt"]), ["nested/b.txt"])

    def test_root_is_resolved_once_and_must_be_an_existing_directory(self):
        link = self.base / "root-alias"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(select_files(link, ["a.txt"]), ["a.txt"])
        for root in (self.base / "missing-root", self.root / "a.txt", None, 1, ""):
            with self.subTest(root=root), self.assertRaises(ValueError):
                select_files(root, [])

    def test_one_pass_iterables(self):
        class Once:
            def __init__(self, values):
                self.values, self.used = values, False

            def __iter__(self):
                if self.used:
                    raise AssertionError("iterable consumed twice")
                self.used = True
                yield from self.values

        self.assertEqual(select_files(self.root, Once(["z.txt", "a.txt"]), Once(["z.txt"])), ["a.txt"])

    def test_inputs_are_not_mutated(self):
        candidates, excluded = ["z.txt", "a.txt", "a.txt"], ["z.txt"]
        before = (candidates[:], excluded[:])
        result = select_files(self.root, candidates, excluded)
        result.append("new")
        self.assertEqual((candidates, excluded), before)

    def test_non_regular_fifo_is_skipped_without_opening(self):
        os.mkfifo(self.root / "pipe")
        self.assertEqual(select_files(self.root, ["pipe", "a.txt"]), ["a.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
