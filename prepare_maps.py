"""Rebuild the shipped maps from a clean checkout of the pinned revision.

Install requirements.lock and coverage==7.17.4 first. This build-time operation
reads tests.json for test IDs only; it never reads patches or answers.json.
"""

import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    subprocess.run([sys.executable, "collect_map.py"], check=True)
    subprocess.run([sys.executable, "collect_types.py"], check=True)
    path = Path("dependencies.json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    with gzip.open("identity_types.json.gz", "rt", encoding="utf-8") as handle:
        identity = json.load(handle)
    if data["collection_exit_code"] or identity["exit_code"]:
        raise RuntimeError("Baseline tests failed; refuse to ship an incomplete map")
    data["identity_sites"] = identity["sites"]
    data["source_sha256"] = {
        filename: hashlib.sha256(source.encode("utf-8")).hexdigest()
        for filename, source in data["sources"].items()
    }
    # A stable gzip header makes the committed build artifact reproducible.
    encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))


if __name__ == "__main__":
    main()
