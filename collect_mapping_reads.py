"""Measure baseline reads of individual class-dictionary values."""
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
from pathlib import Path
import sys
from collect_map import InlinePool, PicklePool


def main():
    repo = Path('sqlglot_repo').resolve()
    output = Path('mapping_reads.json.gz').resolve()
    numbers = {t['id']: t['number'] for t in json.loads(Path('sample/tests.json').read_text())['tests']}
    active = [None]
    reads = {}
    unsafe = set()

    class ObservedDict(dict):
        def __init__(self, values, filename, line):
            super().__init__(values)
            self.origin = (filename, line)
            reads.setdefault(filename, {}).setdefault(str(line), {})

        def record(self, key):
            if active[0] is None:
                unsafe.add(self.origin)
            else:
                reads[self.origin[0]][str(self.origin[1])].setdefault(repr(key), set()).add(active[0])

        def __getitem__(self, key):
            self.record(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            self.record(key)
            return super().get(key, default)

        def items(self):
            for key in super().keys():
                self.record(key)
            return super().items()

        def values(self):
            for key in super().keys():
                self.record(key)
            return super().values()

        def copy(self):
            return ObservedDict(self, *self.origin)

    builtins._blast_radius_dictionary = ObservedDict

    class Instrument(ast.NodeTransformer):
        def __init__(self, filename):
            self.filename = filename
            self.in_class = False

        def visit_ClassDef(self, node):
            previous = self.in_class
            self.in_class = True
            self.generic_visit(node)
            self.in_class = previous
            return node

        def visit_FunctionDef(self, node):
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            if self.in_class and isinstance(node.value, ast.Dict):
                if all(isinstance(k, ast.Constant) for k in node.value.keys):
                    node.value = ast.copy_location(ast.Call(
                        func=ast.Name(id='_blast_radius_dictionary', ctx=ast.Load()),
                        args=[node.value, ast.Constant(self.filename), ast.Constant(node.lineno)], keywords=[]
                    ), node.value)
            return node

        visit_AnnAssign = visit_Assign

    original = importlib.machinery.SourceFileLoader.get_code

    def get_code(loader, fullname):
        path = Path(loader.path).resolve()
        if path.is_relative_to(repo / 'sqlglot'):
            tree = Instrument(path.relative_to(repo).as_posix()).visit(ast.parse(path.read_text('utf-8')))
            return compile(ast.fix_missing_locations(tree), str(path), 'exec')
        return original(loader, fullname)

    importlib.machinery.SourceFileLoader.get_code = get_code
    concurrent.futures.ThreadPoolExecutor = InlinePool
    concurrent.futures.ProcessPoolExecutor = PicklePool
    os.environ['SKIP_INTEGRATION'] = '0'
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    import pytest

    class Context:
        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self, item):
            active[0] = numbers.get(item.nodeid)

        @pytest.hookimpl(trylast=True)
        def pytest_runtest_teardown(self, item):
            active[0] = None

    code = pytest.main(['-q', '--tb=short', 'tests'], plugins=[Context()])
    result = {f: {line: {k: sorted(ns) for k, ns in keys.items()}
                  for line, keys in lines.items() if (f, int(line)) not in unsafe}
              for f, lines in reads.items()}
    with gzip.open(output, 'wt', encoding='utf-8') as handle:
        json.dump({'reads': result, 'exit_code': int(code)}, handle, separators=(',', ':'))


if __name__ == '__main__':
    main()
