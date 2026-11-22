"""Run and score the submission without importing sample labels into the selector."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from score import evaluate_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("sample"))
    parser.add_argument("--output", type=Path, default=Path("output.jsonl"))
    options = parser.parse_args()
    process = subprocess.run([sys.executable, "selector.py", str(options.data)], capture_output=True, check=True)
    options.output.write_bytes(process.stdout)
    tests = json.loads((options.data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((options.data / "answers.json").read_text("utf-8"))["commits"]
    result = evaluate_run(process.stdout, tests, answers)
    print(json.dumps(result, indent=2))
    if not result["ok"] or result["missed_commits"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
