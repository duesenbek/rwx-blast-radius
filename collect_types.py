"""Observe operand types in baseline identity/equality comparisons.

No edited source or sample answers are read. Only comparisons with identity-based
equality for every observed operand pair are retained, without any test numbers.
"""

import ast
import builtins
import concurrent.futures
import enum
import gzip
import importlib.machinery
import json
import os
from pathlib import Path
import sys

from collect_map import InlinePool, PicklePool


def comparison_key(scope, node):
    return json.dumps([scope, ast.dump(node.left), ast.dump(node.comparators[0])], separators=(",", ":"))


def main():
    repo = Path("sqlglot_repo").resolve()
    output = Path("identity_types.json.gz").resolve()
    safe = {}
    seen = set()

    def compare(left, right, op, filename, key):
        a, b = type(left), type(right)
        signature = (filename, key, a, b)
        if signature not in seen:
            seen.add(signature)
            default = all(t is type(None) or
                          (t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__)
                          for t in (a, b))
            singleton = a is b and a in (type(None), bool)
            same_enum = a is b and issubclass(a, enum.Enum) and not issubclass(a, enum.Flag)
            # Enum classes overriding equality themselves do not qualify.
            if same_enum:
                same_enum = (a.__eq__ in (object.__eq__, str.__eq__, int.__eq__)
                             and a.__ne__ in (object.__ne__, str.__ne__, int.__ne__))
            site = (filename, key)
            safe[site] = safe.get(site, True) and (default or singleton or same_enum)
        if op == "Is":
            return left is right
        if op == "IsNot":
            return left is not right
        if op == "Eq":
            return left == right
        return left != right

    builtins._blast_radius_compare = compare

    class Instrument(ast.NodeTransformer):
        def __init__(self, filename):
            self.filename = filename
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
            key = comparison_key(self.scope, node) if len(node.ops) == 1 else None
            self.generic_visit(node)
            if key is not None and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                return ast.copy_location(ast.Call(
                    func=ast.Name(id="_blast_radius_compare", ctx=ast.Load()),
                    args=[node.left, node.comparators[0], ast.Constant(type(node.ops[0]).__name__),
                          ast.Constant(self.filename), ast.Constant(key)], keywords=[]
                ), node)
            return node

    original_get_code = importlib.machinery.SourceFileLoader.get_code

    def get_code(loader, fullname):
        path = Path(loader.path).resolve()
        if path.is_relative_to(repo / "sqlglot"):
            filename = path.relative_to(repo).as_posix()
            tree = Instrument(filename).visit(ast.parse(path.read_text("utf-8")))
            return compile(ast.fix_missing_locations(tree), str(path), "exec")
        return original_get_code(loader, fullname)

    importlib.machinery.SourceFileLoader.get_code = get_code
    concurrent.futures.ThreadPoolExecutor = InlinePool
    concurrent.futures.ProcessPoolExecutor = PicklePool
    sys.path.insert(0, str(repo))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(repo)
    import pytest
    exit_code = pytest.main(["-q", "--tb=short", "tests"])
    result = {}
    for (filename, key), is_safe in sorted(safe.items()):
        if is_safe:
            result.setdefault(filename, []).append(key)
    with gzip.open(output, "wt", encoding="utf-8") as handle:
        json.dump({"sites": result, "exit_code": int(exit_code)}, handle, separators=(",", ":"))


if __name__ == "__main__":
    main()
