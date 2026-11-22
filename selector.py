"""Select tests using dependencies measured on the pinned, unmodified source."""

import ast
import copy
import gzip
import json
from pathlib import Path
import re
import sys

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/data") if Path("/data/tests.json").exists() else APP_DIR / "sample"
with gzip.open(APP_DIR / "dependencies.json.gz", "rt", encoding="utf-8") as handle:
    MAP_DATA = json.load(handle)
LINE_MAP = MAP_DATA["line_map"]
CONFIG_MAP = MAP_DATA["config_map"]
FILE_MAP = MAP_DATA["file_map"]
FIXTURE_MAP = MAP_DATA["fixture_map"]
SOURCES = MAP_DATA["sources"]
TREES = {}
BASE_DUMPS = {}
CANONICAL_DUMPS = {}
IDENTITY_SITES = {f: set(keys) for f, keys in MAP_DATA.get("identity_sites", {}).items()}
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def normalized_path(path):
    path = path.strip().split("\t", 1)[0].replace("\\", "/")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.removeprefix("sqlglot_repo/")


def parse_patch(patch_text):
    files = {}
    info = None
    old_path = None
    hunk = None
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            info = hunk = None
            old_path = None
        elif line.startswith("--- ") and hunk is None:
            old_path = normalized_path(line[4:])
        elif line.startswith("+++ ") and hunk is None:
            path = normalized_path(line[4:])
            path = old_path if path == "/dev/null" else path
            info = files.setdefault(path, {"hunks": [], "deleted": line[4:] == "/dev/null"})
        elif line.startswith("@@ ") and info is not None:
            match = HUNK.match(line)
            if match:
                a, b, c, d = match.groups()
                hunk = {"old_start": int(a), "old_count": int(b or 1),
                        "new_start": int(c), "new_count": int(d or 1), "lines": [],
                        "old_seen": 0, "new_seen": 0}
                info["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk["lines"].append(line)
            hunk["old_seen"] += line[0] in (" ", "-")
            hunk["new_seen"] += line[0] in (" ", "+")
            if hunk["old_seen"] == hunk["old_count"] and hunk["new_seen"] == hunk["new_count"]:
                hunk = None
    return files


def apply_hunks(source, hunks):
    original = source.splitlines(keepends=True)
    result = []
    cursor = 0
    changed = set()
    inserted = set()
    for hunk in hunks:
        start = hunk["old_start"] - 1 if hunk["old_count"] else hunk["old_start"]
        if start < cursor or start > len(original):
            raise ValueError("Overlapping or out-of-range hunk")
        result.extend(original[cursor:start])
        cursor = start
        replacement = False
        removed_count = added_count = 0
        for line in hunk["lines"]:
            marker, content = line[0], line[1:]
            if marker in (" ", "-"):
                if cursor >= len(original) or original[cursor].rstrip("\r\n") != content:
                    raise ValueError("Patch does not match the pinned source")
                if marker == "-":
                    changed.add(cursor + 1)
                    replacement = True
                    removed_count += 1
                else:
                    result.append(original[cursor])
                    replacement = False
                    removed_count = added_count = 0
                cursor += 1
            elif marker == "+":
                # Coverage uses OLD coordinates, even after insertions in prior hunks.
                added_count += 1
                if not replacement or added_count > removed_count:
                    inserted.add(max(1, cursor + 1))
                result.append(content + "\n")
        if cursor != start + hunk["old_count"]:
            raise ValueError("Incorrect hunk size")
    result.extend(original[cursor:])
    return "".join(result), changed, inserted


def tree_for(filename):
    if filename not in TREES:
        TREES[filename] = ast.parse(SOURCES[filename], filename=filename)
    return TREES[filename]


def numbers_for_lines(filename, lines):
    numbers = set()
    for mapping in (LINE_MAP.get(filename, {}), CONFIG_MAP.get(filename, {})):
        for line in lines:
            numbers.update(mapping.get(str(line), ()))
    return numbers


def canonical_tree(tree, filename):
    sites = IDENTITY_SITES.get(filename, set())
    if not sites:
        return ast.dump(tree)

    class NormalizeIdentity(ast.NodeTransformer):
        def __init__(self):
            self.scope = []

        def visit_scoped(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
            return node

        visit_ClassDef = visit_scoped
        visit_FunctionDef = visit_scoped
        visit_AsyncFunctionDef = visit_scoped

        def visit_Compare(self, node):
            if len(node.ops) == 1:
                key = json.dumps([self.scope, ast.dump(node.left), ast.dump(node.comparators[0])],
                                 separators=(",", ":"))
                if key in sites:
                    if isinstance(node.ops[0], ast.Is):
                        node.ops[0] = ast.Eq()
                    elif isinstance(node.ops[0], ast.IsNot):
                        node.ops[0] = ast.NotEq()
            return self.generic_visit(node)

    return ast.dump(NormalizeIdentity().visit(copy.deepcopy(tree)))


def select_python(filename, info, allow_equivalence=True):
    if filename not in SOURCES or info["deleted"]:
        return set(FILE_MAP.get(filename, ())) or set(ALL_TESTS)
    try:
        modified, changed, inserted = apply_hunks(SOURCES[filename], info["hunks"])
        old_tree = tree_for(filename)
        new_tree = ast.parse(modified, filename=filename)
        compile(modified.encode("utf-8"), filename, "exec")
    except (SyntaxError, ValueError, TypeError):
        if filename.startswith("tests/test_") or filename.startswith("tests/dialects/test_"):
            return set(FILE_MAP.get(filename, ALL_TESTS))
        return set(ALL_TESTS)
    if filename not in BASE_DUMPS:
        BASE_DUMPS[filename] = ast.dump(old_tree)
    old_dump = BASE_DUMPS[filename]
    new_dump = ast.dump(new_tree)
    if old_dump == new_dump:
        return set()
    if allow_equivalence and filename in IDENTITY_SITES:
        # Most edits change more than identity operators. Avoid walking and copying
        # those large ASTs again when identity normalization cannot equate them.
        def operator_shape(dump):
            return dump.replace("Is()", "Eq()").replace("IsNot()", "NotEq()")

        if operator_shape(old_dump) == operator_shape(new_dump):
            if filename not in CANONICAL_DUMPS:
                CANONICAL_DUMPS[filename] = canonical_tree(old_tree, filename)
            if CANONICAL_DUMPS[filename] == canonical_tree(new_tree, filename):
                return set()
    if filename.startswith("tests/"):
        return set(FILE_MAP.get(filename, ()))

    numbers = set()
    statements = [node for node in ast.walk(old_tree) if isinstance(node, ast.stmt)]
    functions = [node for node in statements if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    configs = CONFIG_MAP.get(filename, {})
    for line in sorted(changed | inserted):
        if line not in inserted and str(line) in configs:
            numbers.update(configs[str(line)])
            continue
        containing = [n for n in statements if n.lineno <= line <= n.end_lineno]
        if not containing:
            numbers.update(FILE_MAP.get(filename, ()))
            continue
        node = min(containing, key=lambda n: n.end_lineno - n.lineno)
        parent_functions = [n for n in functions if n.lineno <= line <= n.end_lineno]
        if not parent_functions or isinstance(node, (ast.Import, ast.ImportFrom)):
            numbers.update(FILE_MAP.get(filename, ()))
            continue
        function = min(parent_functions, key=lambda n: n.end_lineno - n.lineno)
        if line in inserted or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            affected_lines = range(function.lineno, function.end_lineno + 1)
        else:
            # A changed continuation can become reachable after short circuiting.
            end = node.end_lineno
            if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                end = node.body[0].lineno - 1
            affected_lines = range(node.lineno, max(node.lineno, end) + 1)
        numbers.update(numbers_for_lines(filename, affected_lines))
    return numbers


ALL_TESTS = {int(number) for number in MAP_DATA["test_ids"]}


def select_tests_for_patch(patch_text):
    numbers = set()
    files = parse_patch(patch_text)
    for filename, info in files.items():
        if filename in FIXTURE_MAP:
            numbers.update(FIXTURE_MAP[filename])
        elif filename.endswith(".py"):
            numbers.update(select_python(filename, info, allow_equivalence=len(files) == 1))
        elif filename.startswith("tests/fixtures/"):
            numbers.update(FILE_MAP.get(filename, ()))
    return sorted(numbers)


def partition_tests(test_nums, durations, max_workers=8):
    if not test_nums:
        return []
    milliseconds = {n: round(durations.get(n, 0.001) * 1000) for n in test_nums}
    ordered = sorted(set(test_nums), key=lambda n: (-milliseconds[n], n))
    best = [ordered]
    best_cost = sum(milliseconds.values()) + 2000
    for count in range(2, min(max_workers, len(ordered)) + 1):
        buckets = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            worker = min(range(count), key=loads.__getitem__)
            buckets[worker].append(number)
            loads[worker] += milliseconds[number]
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best, best_cost = buckets, cost
        if max(milliseconds.values()) + 2000 * (count + 1) >= best_cost:
            break
    return best


def main(data_dir=DATA_DIR):
    metadata = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    durations = {test["number"]: test["seconds"] for test in metadata}
    runtime_numbers = {test["id"]: test["number"] for test in metadata}
    number_map = {int(n): runtime_numbers[tid] for n, tid in MAP_DATA["test_ids"].items()
                  if tid in runtime_numbers}
    for patch in sorted((data_dir / "commits").glob("*.patch")):
        selected = select_tests_for_patch(patch.read_text("utf-8"))
        selected = [number_map[n] for n in selected if n in number_map]
        workers = partition_tests(selected, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR)
