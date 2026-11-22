"""Record which pinned SQLGlot modules each test file imports at collection."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
import subprocess
import sys


IMPORT_TEST_FILE = """
import importlib
import json
from pathlib import Path
import sys

importlib.import_module(sys.argv[1])
root = Path.cwd()
files = set()
for name, module in tuple(sys.modules.items()):
    path = getattr(module, '__file__', None)
    if (name == 'sqlglot' or name.startswith('sqlglot.')) and path:
        files.add(Path(path).resolve().relative_to(root).as_posix())
print(json.dumps(sorted(files)))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("sqlglot_repo"))
    parser.add_argument("--tests", type=Path, default=Path("sample/tests.json"))
    parser.add_argument("--map", type=Path, default=Path("dependencies.json.gz"))
    options = parser.parse_args()
    metadata = json.loads(options.tests.read_text("utf-8"))["tests"]
    test_files = {}
    for test in metadata:
        test_files.setdefault(test["file"], []).append(test["number"])

    def collect(filename):
        module = filename.removesuffix(".py").replace("/", ".")
        run = subprocess.run(
            [sys.executable, "-c", IMPORT_TEST_FILE, module], cwd=options.repo,
            text=True, capture_output=True, check=True, timeout=30,
        )
        return filename, json.loads(run.stdout.splitlines()[-1])

    imports = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for filename, paths in pool.map(collect, sorted(test_files)):
            for path in paths:
                imports.setdefault(path, set()).update(test_files[filename])

    with gzip.open(options.map, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    data["import_map"] = {path: sorted(numbers) for path, numbers in sorted(imports.items())}
    encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
    options.map.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    print(f"Recorded imports for {len(test_files)} test files and {len(imports)} SQLGlot modules.")


if __name__ == "__main__":
    main()
