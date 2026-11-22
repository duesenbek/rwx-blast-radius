"""Score three Docker runs under the contest's runtime limits."""

import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess

from score import evaluate_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="blast-radius-entry")
    parser.add_argument("--data", type=Path, default=Path("sample"))
    options = parser.parse_args()
    data = options.data.resolve()
    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
    wrapper = (
        "import resource,runpy,sys;"
        "runpy.run_path('/app/selector.py',run_name='__main__');"
        "r=resource.getrusage(resource.RUSAGE_SELF);"
        "print(r.ru_utime+r.ru_stime,file=sys.stderr)"
    )
    totals = []
    for attempt in range(3):
        command = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--env", "HOME=/tmp",
            "--volume", f"{data / 'commits'}:/data/commits:ro",
            "--volume", f"{data / 'tests.json'}:/data/tests.json:ro",
            options.image, "python", "-c", wrapper,
        ]
        result = subprocess.run(command, capture_output=True, check=True, timeout=180)
        scored = evaluate_run(result.stdout, tests, answers)
        if not scored["ok"] or scored["missed_commits"]:
            raise RuntimeError(scored)
        cpu_seconds = float(result.stderr.strip().splitlines()[-1])
        total = scored["simulated_milliseconds"] / 1000 + cpu_seconds
        totals.append(total)
        print(json.dumps({"run": attempt + 1, "simulated_seconds": scored["simulated_milliseconds"] / 1000,
                          "cpu_seconds": cpu_seconds, "total_seconds": total,
                          "misses": 0, "stdout_bytes": len(result.stdout)}), flush=True)
    median = statistics.median(totals)
    print(f"Median score: {math.floor(median * 10 + 0.5) / 10:.1f} s")


if __name__ == "__main__":
    main()
