"""Regression checks for diff parsing, dependency selection and worker costs."""

import difflib
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

    def test_syntax_error_selects_tests_even_when_map_is_empty(self):
        filename = "sqlglot/synthetic_syntax.py"
        before = "def f():\n    return 1\n"
        with patch.dict(selector.SOURCES, {filename: before}):
            self.assertEqual(set(selector.select_tests_for_patch(
                diff(filename, before, "def f(:\n    return 1\n")
            )), selector.ALL_TESTS)

    def test_config_assignment_uses_all_observed_file_dependencies(self):
        filename = "sqlglot/synthetic_class.py"
        before = "class Dialect:\n    WEEK_OFFSET = -1\n"
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.CONFIG_MAP, {filename: {"2": [4]}}
        ), patch.dict(selector.FILE_MAP, {filename: [4, 7]}):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, before, before.replace("-1", "-2"))
            ), [4, 7])

    def test_import_surface_change_includes_collection_importers(self):
        filename = "sqlglot/shared_import.py"
        before = "from math import sin\n"
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.FILE_MAP, {filename: [7]}
        ), patch.dict(selector.IMPORT_MAP, {filename: [8]}):
            self.assertEqual(selector.select_tests_for_patch(
                diff(filename, before, before.replace("sin", "cos"))
            ), [7, 8])

    def test_unread_statement_keeps_function_callers(self):
        filename = "sqlglot/branch_example.py"
        before = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        with patch.dict(selector.SOURCES, {filename: before}), patch.dict(
            selector.LINE_MAP, {filename: {"2": [7], "4": [7]}}
        ):
            self.assertEqual(selector.select_tests_for_patch(diff(filename, before, before.replace("return 1", "return 3"))), [7])

    def test_unknown_fixture_gets_full_suite(self):
        filename = "tests/fixtures/unmapped.json"
        self.assertEqual(set(selector.select_tests_for_patch(diff(filename, "{}\n", "[]\n"))), selector.ALL_TESTS)

    def test_metadata_only_rename_in_multi_file_diff_gets_full_suite(self):
        text = ("diff --git a/sqlglot/old.py b/sqlglot/new.py\n"
                "similarity index 100%\nrename from sqlglot/old.py\nrename to sqlglot/new.py\n"
                "diff --git a/README.md b/README.md\n")
        text += diff("README.md", "old\n", "new\n")
        self.assertEqual(set(selector.select_tests_for_patch(text)), selector.ALL_TESTS)

    def test_shared_setup_fixture_includes_entire_executor_class(self):
        filename = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
        patch_text = diff(filename, "SELECT 1;\n", "# malformed metadata\nSELECT 1;\n")
        executor = {int(n) for n, test_id in selector.MAP_DATA["test_ids"].items()
                    if test_id.startswith("tests/test_executor.py::TestExecutor::")}
        self.assertGreater(len(executor), 1)
        self.assertLessEqual(executor, set(selector.select_tests_for_patch(patch_text)))

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
