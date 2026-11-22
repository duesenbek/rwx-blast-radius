"""Collect dependencies from the unmodified pinned checkout, never from patches.

Run: python collect_map.py --repo sqlglot_repo --tests sample/tests.json
The test pools run inline so their work retains the requesting test's context.
"""

import argparse
import ast
import builtins
import collections
import concurrent.futures
import gzip
import importlib.machinery
import io
import json
import os
import pickle
from pathlib import Path
import sys


class InlinePool:
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()

    def shutdown(self, *args, **kwargs):
        pass

    def submit(self, function, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future

    def map(self, function, *iterables, **kwargs):
        return map(function, *iterables)


class PicklePool(InlinePool):
    def submit(self, function, *args, **kwargs):
        # ProcessPoolExecutor serializes both arguments and results. Preserve those
        # dependencies while executing in the caller's coverage context.
        def roundtrip():
            copied_args, copied_kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = function(*copied_args, **copied_kwargs)
            return pickle.loads(pickle.dumps(result))

        return super().submit(roundtrip)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("sqlglot_repo"))
    parser.add_argument("--tests", type=Path, default=Path("sample/tests.json"))
    parser.add_argument("--output", type=Path, default=Path("dependencies.json.gz"))
    parser.add_argument("paths", nargs="*")
    options = parser.parse_args()
    repo = options.repo.resolve()
    output_path = options.output.resolve()
    metadata = json.loads(options.tests.read_text("utf-8"))["tests"]
    test_numbers = {item["id"]: item["number"] for item in metadata}
    sources = {}
    definitions = {}
    tracked_attributes = set()
    function_lines = collections.defaultdict(set)

    def index_nodes(nodes, module, filename, parents=()):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                index_nodes(node.body, module, filename, (*parents, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Include the definition and decorators for edits to signatures.
                start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                function_lines[filename].update(range(start, node.end_lineno + 1))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parents:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        definitions[(module, ".".join(parents), name)] = (
                            filename, node.lineno, node.end_lineno
                        )
                        tracked_attributes.add(name)

    for path in sorted((repo / "sqlglot").rglob("*.py")):
        filename = path.relative_to(repo).as_posix()
        source = path.read_text("utf-8")
        sources[filename] = source
        module = filename[:-3].replace("/", ".").removesuffix(".__init__")
        index_nodes(ast.parse(source).body, module, filename)
    for path in sorted((repo / "tests").rglob("*.py")):
        sources[path.relative_to(repo).as_posix()] = path.read_text("utf-8")

    active = [None]
    cov = None
    number_to_id = {item["number"]: item["id"] for item in metadata}
    shared_contexts = {}
    seen = set()
    attribute_map = collections.defaultdict(set)
    class_map = collections.defaultdict(set)
    fixture_map = collections.defaultdict(set)

    def observe_attribute(obj, attribute):
        result = getattr(obj, attribute)
        number = active[0]
        if number is None:
            return result
        cls = obj if isinstance(obj, type) else type(obj)
        key = (cls, attribute)
        if key in seen:
            return result
        seen.add(key)
        try:
            mro = type.__getattribute__(cls, "__mro__")
        except TypeError:
            return result
        found = False
        for parent in mro:
            module = type.__getattribute__(parent, "__module__")
            if not module.startswith("sqlglot"):
                continue
            qualname = type.__getattribute__(parent, "__qualname__")
            class_map[(module, qualname)].add(number)
            if not found and attribute in type.__getattribute__(parent, "__dict__"):
                definition = definitions.get((module, qualname, attribute))
                if definition:
                    attribute_map[definition].add(number)
                found = True
        return result

    builtins._blast_radius_attribute = observe_attribute

    class InstrumentAttributes(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and node.attr in tracked_attributes:
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="_blast_radius_attribute", ctx=ast.Load()),
                        args=[node.value, ast.Constant(node.attr)],
                        keywords=[],
                    ), node
                )
            return node

    original_get_code = importlib.machinery.SourceFileLoader.get_code
    original_exec_module = importlib.machinery.SourceFileLoader.exec_module

    def get_code(loader, fullname):
        filename = Path(loader.path).resolve()
        if filename.is_relative_to(repo / "sqlglot"):
            tree = ast.parse(filename.read_text("utf-8"), filename=str(filename))
            tree = ast.fix_missing_locations(InstrumentAttributes().visit(tree))
            return compile(tree, str(filename), "exec")
        return original_get_code(loader, fullname)

    importlib.machinery.SourceFileLoader.get_code = get_code

    def exec_module(loader, module):
        saved = active[0]
        is_source = Path(loader.path).resolve().is_relative_to(repo / "sqlglot")
        if is_source:
            active[0] = None
            if cov is not None and saved is not None:
                cov.switch_context("")
        try:
            return original_exec_module(loader, module)
        finally:
            if is_source:
                active[0] = saved
                if cov is not None and saved is not None:
                    cov.switch_context(number_to_id.get(saved, saved))

    importlib.machinery.SourceFileLoader.exec_module = exec_module
    original_open = builtins.open
    original_io_open = io.open

    def record_fixture(path):
        if active[0] is None or not isinstance(path, (str, os.PathLike)):
            return
        filename = Path(path).resolve()
        if filename.is_relative_to(repo / "tests" / "fixtures"):
            fixture_map[filename.relative_to(repo).as_posix()].add(active[0])

    def observed_open(path, *args, **kwargs):
        record_fixture(path)
        return original_open(path, *args, **kwargs)

    def observed_io_open(path, *args, **kwargs):
        record_fixture(path)
        return original_io_open(path, *args, **kwargs)

    builtins.open = observed_open
    io.open = observed_io_open
    concurrent.futures.ThreadPoolExecutor = InlinePool
    concurrent.futures.ProcessPoolExecutor = PicklePool
    sys.path.insert(0, str(repo))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    import coverage
    import pytest

    cov = coverage.Coverage(
        data_file=None, config_file=False, source=[str(repo / "sqlglot")]
    )

    class Contexts:
        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if fixturedef.scope == "function":
                yield
                return
            prefix = request.node.nodeid
            context = f"shared:{fixturedef.scope}:{prefix}"
            if context not in shared_contexts:
                shared_contexts[context] = {
                    n for test_id, n in test_numbers.items()
                    if not prefix or test_id.startswith((prefix + "::", prefix + "/"))
                }
            saved = active[0]
            active[0] = context
            seen.clear()
            cov.switch_context(context)
            try:
                yield
            finally:
                active[0] = saved
                seen.clear()
                cov.switch_context(number_to_id.get(saved, saved) if saved is not None else "")

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self, item):
            active[0] = test_numbers.get(item.nodeid)
            seen.clear()
            cov.switch_context(item.nodeid)

        @pytest.hookimpl(trylast=True)
        def pytest_runtest_teardown(self, item):
            active[0] = None
            cov.switch_context("")

    os.chdir(repo)
    cov.start()
    exit_code = pytest.main(["-q", "--tb=short", *(options.paths or ["tests"])], plugins=[Contexts()])
    cov.stop()
    builtins.open = original_open
    io.open = original_io_open
    data = cov.get_data()

    def expand(contexts):
        numbers = set()
        for context in contexts:
            if isinstance(context, int):
                numbers.add(context)
            elif context in test_numbers:
                numbers.add(test_numbers[context])
            else:
                numbers.update(shared_contexts.get(context, ()))
        return sorted(numbers)

    line_map = collections.defaultdict(dict)
    for filename in sorted(data.measured_files()):
        path = Path(filename)
        if not path.is_relative_to(repo):
            continue
        relative = path.relative_to(repo).as_posix()
        for line, contexts in data.contexts_by_lineno(filename).items():
            if line not in function_lines[relative]:
                continue
            numbers = expand(contexts)
            if numbers:
                line_map[relative][str(line)] = numbers

    config_map = collections.defaultdict(dict)
    for (module, qualname, name), definition in definitions.items():
        filename, start, end = definition
        # Direct runtime reads are precise; class use covers cached metaclass settings.
        numbers = attribute_map.get(definition) or class_map.get((module, qualname), set())
        for line in range(start, end + 1):
            config_map[filename][str(line)] = expand(numbers)

    file_map = {}
    for filename in sources:
        numbers = set()
        for mapping in (line_map, config_map):
            for tests in mapping.get(filename, {}).values():
                numbers.update(tests)
        numbers.update(t["number"] for t in metadata if t["file"] == filename)
        if numbers:
            file_map[filename] = sorted(numbers)
    output = {
        "version": 2,
        "base_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "line_map": dict(line_map),
        "config_map": dict(config_map),
        "file_map": file_map,
        "fixture_map": {f: expand(ns) for f, ns in fixture_map.items()},
        "test_ids": {str(t["number"]): t["id"] for t in metadata},
        "sources": sources,
        "collection_exit_code": int(exit_code),
    }
    with gzip.open(output_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(output, handle, separators=(",", ":"))
    print(f"Saved {options.output}: {len(line_map)} files, {len(fixture_map)} fixtures", file=sys.stderr)


if __name__ == "__main__":
    main()
