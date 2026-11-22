"""Run small candidate suites on the actual patch in an isolated subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


def main():
    requests = json.load(sys.stdin)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with zipfile.ZipFile(Path(__file__).with_name("baseline.zip")) as archive:
            archive.extractall(root)
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True
        passed = []
        deadline = time.monotonic() + 35
        for request in requests:
            if time.monotonic() >= deadline:
                break
            originals = {}
            for filename, source in request["sources"].items():
                path = root / filename
                originals[path] = path.read_bytes()
                path.write_text(source, encoding="utf-8")
            child = os.fork()
            if child == 0:
                try:
                    os.setsid()
                    with open(root / "probe.log", "w") as sink:
                        sys.stdout = sys.stderr = sink
                        suite = unittest.TestSuite()
                        for test_id in request["tests"]:
                            name = test_id.removesuffix(".py").replace(".py::", "::").replace("/", ".").replace("::", ".")
                            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
                        result = unittest.TextTestRunner(stream=sink, failfast=True).run(suite)
                        os._exit(0 if result.wasSuccessful() else 1)
                except BaseException:
                    os._exit(2)
            status = None
            limit = min(deadline, time.monotonic() + 10)
            while time.monotonic() < limit:
                done, value = os.waitpid(child, os.WNOHANG)
                if done:
                    status = value
                    break
                time.sleep(0.01)
            if status is None:
                os.killpg(child, 9)
                os.waitpid(child, 0)
            elif os.waitstatus_to_exitcode(status) == 0:
                passed.append(request["commit"])
            if os.getenv("PROBE_DEBUG"):
                print(request["commit"], status, (root / "probe.log").read_text()[-5000:], file=sys.stderr)
            for path, content in originals.items():
                path.write_bytes(content)
        print(json.dumps(passed))


if __name__ == "__main__":
    main()
