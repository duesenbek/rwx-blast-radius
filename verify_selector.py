"""Regression checks for diff parsing, dependency selection and worker costs."""

import difflib
import ast
import itertools
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

import selector


def diff(filename, before, after, context=3):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/" + filename, tofile="b/" + filename, n=context,
    ))


class SelectionChecks(unittest.TestCase):
    def test_multiple_hunks_keep_original_coordinates(self):
        before = "".join(f"line {n}\n" for n in range(20))
        after = before.replace("line 2\n", "extra\nline 2\n").replace("line 17\n", "changed\n")
        info = selector.parse_patch(diff("module.py", before, after, context=1))["module.py"]
        actual, changed, inserted = selector.apply_hunks(before, info["hunks"])
        self.assertEqual(actual, after)
        self.assertEqual(changed, {18})
        self.assertEqual(inserted, {3})

    def test_deleted_file_uses_old_path(self):
        text = "diff --git a/module.py b/module.py\n--- a/module.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
        info = selector.parse_patch(text)
        self.assertTrue(info["module.py"]["deleted"])

    def test_replacements_are_not_insertions(self):
        before = "def f():\n    return 1\n"
        info = selector.parse_patch(diff("module.py", before, before.replace("1", "2")))["module.py"]
        _, changed, inserted = selector.apply_hunks(before, info["hunks"])
        self.assertEqual(changed, {2})
        self.assertEqual(inserted, set())

    def test_comment_in_multiline_string_is_executable_data(self):
        before = 'def f():\n    return """\n# old\n"""\n'
        filename = "sqlglot/synthetic.py"
        text = diff(filename, before, before.replace("# old", "# new"))
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.LINE_MAP, {filename: {"2": [7]}}
        ):
            self.assertEqual(selector.select_tests_for_patch(text), [7])

    def test_sql_fixture_metadata_comment_is_not_skipped(self):
        filename = "tests/fixtures/synthetic.sql"
        with patch.dict(selector.FIXTURE_MAP, {filename: [7]}):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, "# dialect: spark\nSELECT 1;\n", "# dialect: mysql\nSELECT 1;\n")
            ), [7])

    def test_python_comment_only_change_is_empty(self):
        filename = "sqlglot/synthetic_comment.py"
        before = "def f():\n    return 1\n"
        with patch.dict(selector.SOURCES, {filename: before}):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, before, "# comment\n" + before)
            ), [])

    def test_encoding_cookie_can_break_a_comment_only_change(self):
        filename = "sqlglot/synthetic_encoding.py"
        before = '# coding: utf-8\nvalue = "\u00e9"\n'
        with patch.dict(selector.SOURCES, {filename: before}):
            self.assertEqual(set(selector.select_tests_for_patch(
                diff(filename, before, before.replace("utf-8", "ascii"))
            )), selector.ALL_TESTS)

    def test_identity_normalization_requires_observed_site(self):
        filename = "sqlglot/synthetic_identity.py"
        before = "def f(value):\n    return value is None\n"
        tree = ast.parse(before)
        comparison = tree.body[0].body[0].value
        key = json.dumps([["f"], ast.dump(comparison.left), ast.dump(comparison.comparators[0])],
                         separators=(",", ":"))
        text = diff(filename, before, before.replace(" is ", " == "))
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.LINE_MAP, {filename: {"2": [7]}}
        ):
            self.assertEqual(selector.select_tests_for_patch(text), [7])
            with patch.dict(selector.IDENTITY_SITES, {filename: {key}}):
                self.assertEqual(selector.select_tests_for_patch(text), [])

    def test_identity_normalization_does_not_hide_other_changes(self):
        filename = "sqlglot/synthetic_other.py"
        before = "def f(value):\n    return value is None\n"
        tree = ast.parse(before)
        comparison = tree.body[0].body[0].value
        key = json.dumps([["f"], ast.dump(comparison.left), ast.dump(comparison.comparators[0])],
                         separators=(",", ":"))
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.LINE_MAP, {filename: {"2": [7]}}
        ), patch.dict(selector.IDENTITY_SITES, {filename: {key}}):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, before, before.replace(" is ", " != "))
            ), [7])

    def test_syntax_error_selects_tests_even_when_map_is_empty(self):
        filename = "sqlglot/synthetic_syntax.py"
        before = "def f():\n    return 1\n"
        with patch.dict(selector.SOURCES, {filename: before}):
            self.assertEqual(set(selector.select_tests_for_patch(
                diff(filename, before, "def f(:\n    return 1\n")
            )), selector.ALL_TESTS)

    def test_class_attribute_reads_use_config_dependencies(self):
        filename = "sqlglot/synthetic_class.py"
        before = "class Dialect:\n    WEEK_OFFSET = -1\n"
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.CONFIG_MAP, {filename: {"2": [4, 7]}}
        ):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, before, before.replace("-1", "-2"))
            ), [4, 7])

    def test_partitions_are_valid_and_never_cost_more_than_one_worker(self):
        rng = random.Random(42)
        for size in range(1, 50):
            durations = {n: rng.randrange(0, 30000) / 1000 for n in range(size)}
            workers = selector.partition_tests(list(durations), durations)
            self.assertEqual(sorted(itertools.chain.from_iterable(workers)), list(durations))
            self.assertLessEqual(len(workers), 8)
            cost = max(sum(round(durations[n] * 1000) for n in w) for w in workers) + 2000 * len(workers)
            self.assertLessEqual(cost, sum(round(d * 1000) for d in durations.values()) + 2000)

    def test_sample_has_no_misses(self):
        answers = json.loads(Path("sample/answers.json").read_text("utf-8"))["commits"]
        for filename in Path("sample/commits").glob("*.patch"):
            with self.subTest(commit=filename.stem):
                actual = set(selector.select_tests_for_patch(filename.read_text("utf-8")))
                self.assertTrue(set(answers[filename.stem]["failing"]) <= actual)


if __name__ == "__main__":
    unittest.main()
