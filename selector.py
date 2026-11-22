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
IMPORT_MAP = MAP_DATA.get("import_map", {})
FIXTURE_MAP = MAP_DATA["fixture_map"]
SOURCES = MAP_DATA["sources"]
TREES = {}
BASE_DUMPS = {}
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
    git_path = None
    for line in patch_text.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith("diff --git "):
            if git_path is not None and info is None:
                files[git_path] = {"unhandled": True}
            info = hunk = None
            old_path = None
            git_path = line[11:]
        elif line.startswith("--- ") and hunk is None:
            old_path = normalized_path(line[4:])
        elif line.startswith("+++ ") and hunk is None:
            new_path = normalized_path(line[4:])
            deleted = new_path == "/dev/null"
            path = old_path if deleted and old_path is not None else new_path
            renamed = old_path not in (None, "/dev/null", path)
            info = files.setdefault(path, {"hunks": [], "deleted": deleted, "unhandled": renamed})
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
    if git_path is not None and info is None:
        files[git_path] = {"unhandled": True}
    return files


def apply_hunks(source, hunks):
    # Git counts LF boundaries. Python splitlines also splits characters that may
    # legitimately occur inside string literals, for example a vertical tab.
    parts = source.split("\n")
    original = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        original.append(parts[-1])
    result = []
    cursor = 0
    changed = set()
    inserted = set()
    for hunk in hunks:
        if hunk["old_seen"] != hunk["old_count"] or hunk["new_seen"] != hunk["new_count"]:
            raise ValueError("Incomplete hunk")
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
                if cursor >= len(original) or original[cursor].removesuffix("\n").removesuffix("\r") != content:
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
        TREES[filename] = ast.parse(SOURCES[filename], filename=filename, type_comments=True)
    return TREES[filename]


def numbers_for_lines(filename, lines):
    numbers = set()
    for mapping in (LINE_MAP.get(filename, {}), CONFIG_MAP.get(filename, {})):
        for line in lines:
            numbers.update(mapping.get(str(line), ()))
    return numbers


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
ALL_TESTS = {int(number) for number in MAP_DATA["test_ids"]}


def import_surface(tree):
    """Keep declarations evaluated when a module loads, ignoring function bodies."""
    class Surface(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Pass()]
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            if isinstance(node.value, (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple, ast.UnaryOp)):
                # Track the dependency of the complete setting. A same-type value
                # edit preserves the surrounding declaration and evaluation shape.
                node.value = LiteralTypes().visit(node.value)
            return node

        def visit_AnnAssign(self, node):
            return self.visit_Assign(node) if node.value is not None else node

    class LiteralTypes(ast.NodeTransformer):
        def visit_Constant(self, node):
            return ast.Constant(type(node.value).__name__)

    return ast.dump(Surface().visit(copy.deepcopy(tree)))


def tests_for_test_file(filename):
    """A changed test utility can affect every file importing it."""
    direct = FILE_MAP.get(filename)
    if not direct:
        return set(ALL_TESTS)
    module = filename.removesuffix(".py").replace("/", ".")
    leaf = module.rsplit(".", 1)[-1]
    for other, text in SOURCES.items():
        if other == filename or not other.startswith("tests/") or leaf not in text:
            continue
        for node in ast.walk(tree_for(other)):
            if isinstance(node, ast.ImportFrom) and node.module == module:
                return set(ALL_TESTS)
            if isinstance(node, ast.Import) and any(alias.name == module for alias in node.names):
                return set(ALL_TESTS)
            if isinstance(node, ast.ImportFrom) and any(alias.name == leaf for alias in node.names):
                return set(ALL_TESTS)
    return set(direct)


def select_python(filename, info, broad=False):
    if info["deleted"] or filename not in SOURCES or not info["hunks"]:
        return set(ALL_TESTS)
    try:
        modified, changed, inserted = apply_hunks(SOURCES[filename], info["hunks"])
        old_tree = tree_for(filename)
        new_tree = ast.parse(modified, filename=filename, type_comments=True)
        compile(modified.encode("utf-8"), filename, "exec")
    except (SyntaxError, ValueError, TypeError):
        return tests_for_test_file(filename) if filename.startswith("tests/") else set(ALL_TESTS)
    if filename not in BASE_DUMPS:
        BASE_DUMPS[filename] = ast.dump(old_tree)
    if BASE_DUMPS[filename] == ast.dump(new_tree):
        return set()
    if filename.startswith("tests/"):
        return tests_for_test_file(filename)

    changes = changed | inserted
    statements = [node for node in ast.walk(old_tree) if isinstance(node, ast.stmt)]
    functions = [node for node in statements if isinstance(node, FUNCTIONS)]
    file_tests = set(FILE_MAP.get(filename, ())) or set(ALL_TESTS)
    numbers = set()
    for line in sorted(changes):
        enclosing = sorted(
            (f for f in functions if f.lineno < line <= f.end_lineno),
            key=lambda f: f.end_lineno - f.lineno,
        )
        if not enclosing or str(line) in CONFIG_MAP.get(filename, {}):
            importers = IMPORT_MAP.get(filename, ())
            if import_surface(old_tree) != import_surface(new_tree):
                return file_tests | set(importers) if importers else set(ALL_TESTS)
            return file_tests
        if broad:
            return file_tests
        statement = min(
            (n for n in statements if n.lineno <= line <= n.end_lineno),
            key=lambda n: n.end_lineno - n.lineno,
        )
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            return file_tests
        line_tests = set()
        if line not in inserted and not isinstance(statement, FUNCTIONS):
            # Include the predicate entry for multiline or short-circuited expressions.
            end = statement.end_lineno
            if isinstance(statement, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                end = statement.body[0].lineno - 1
            line_tests = numbers_for_lines(filename, range(statement.lineno, max(statement.lineno, end) + 1))
        if not line_tests:
            # Newly inserted or uncovered code can be reached by callers of the scope.
            for scope in enclosing:
                line_tests = numbers_for_lines(filename, range(scope.lineno, scope.end_lineno + 1))
                if line_tests:
                    break
        numbers.update(line_tests or file_tests)
    return numbers or file_tests


def select_tests_for_patch(patch_text):
    numbers = set()
    files = parse_patch(patch_text)
    if not files and patch_text.strip():
        return sorted(ALL_TESTS)
    broad = sum(f.startswith("sqlglot/") and f.endswith(".py") for f in files) > 1
    for filename, info in files.items():
        if info.get("unhandled") or info["deleted"]:
            return sorted(ALL_TESTS)
        if filename in FIXTURE_MAP:
            numbers.update(FIXTURE_MAP[filename] or ALL_TESTS)
        elif filename.endswith(".py"):
            numbers.update(select_python(filename, info, broad=broad))
        elif filename.startswith(("tests/", "sqlglot/")) or not filename.lower().endswith((".md", ".rst")):
            return sorted(ALL_TESTS)
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
    unmapped = set(runtime_numbers.values()) - set(number_map.values())
    for patch in sorted((data_dir / "commits").glob("*.patch")):
        try:
            selected = select_tests_for_patch(patch.read_text("utf-8"))
        except UnicodeError:
            selected = ALL_TESTS
        runtime_selection = {number_map[n] for n in selected if n in number_map} | unmapped
        workers = partition_tests(runtime_selection, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR)
