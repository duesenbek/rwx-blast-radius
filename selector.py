import collections
import gzip
import json
import os
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/data") if (Path("/data") / "tests.json").exists() else Path("sample")

# Load precomputed coverage and fixture map
MAP_GZ = APP_DIR / "map.json.gz"
MAP_JSON = APP_DIR / "map.min.json" if (APP_DIR / "map.min.json").exists() else APP_DIR / "map.json"

if MAP_GZ.exists():
    with gzip.open(MAP_GZ, "rt", encoding="utf-8") as f:
        MAP_DATA = json.load(f)
elif MAP_JSON.exists():
    with open(MAP_JSON, "r", encoding="utf-8") as f:
        MAP_DATA = json.load(f)
else:
    raise RuntimeError("Coverage map file not found.")

LINE_MAP = MAP_DATA["line_map"]
FIXTURE_MAP = MAP_DATA["fixture_map"]
FILE_MAP = MAP_DATA["file_map"]
DEFAULT_TEST_DURATIONS = {int(k): v for k, v in MAP_DATA.get("test_durations", {}).items()}

DIALECT_OPTIMIZER_FIXTURES = {
    "athena": ["optimizer/annotate_functions.sql", "optimizer/pushdown_predicates.sql"],
    "bigquery": [
        "optimizer/annotate_functions.sql",
        "optimizer/annotate_types.sql",
        "optimizer/canonicalize_internal_names.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/optimizer.sql",
        "optimizer/pushdown_projections.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_tables.sql",
        "optimizer/quote_identifiers.sql",
        "optimizer/simplify.sql",
    ],
    "clickhouse": [
        "optimizer/annotate_functions.sql",
        "optimizer/pushdown_predicates.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/simplify.sql",
    ],
    "databricks": ["optimizer/annotate_functions.sql", "optimizer/annotate_types.sql"],
    "duckdb": [
        "optimizer/annotate_functions.sql",
        "optimizer/annotate_types.sql",
        "optimizer/canonicalize_internal_names.sql",
        "optimizer/merge_subqueries.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/optimizer.sql",
        "optimizer/pushdown_predicates.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_columns__invalid.sql",
        "optimizer/simplify.sql",
    ],
    "hive": ["optimizer/annotate_functions.sql"],
    "mysql": [
        "optimizer/annotate_functions.sql",
        "optimizer/canonicalize.sql",
        "optimizer/optimizer.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/simplify.sql",
    ],
    "oracle": ["optimizer/qualify_columns.sql"],
    "postgres": [
        "optimizer/annotate_functions.sql",
        "optimizer/canonicalize_internal_names.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/optimizer.sql",
        "optimizer/pushdown_projections.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_tables.sql",
        "optimizer/simplify.sql",
    ],
    "presto": [
        "optimizer/annotate_functions.sql",
        "optimizer/annotate_types.sql",
        "optimizer/pushdown_predicates.sql",
        "optimizer/qualify_columns.sql",
    ],
    "redshift": [
        "optimizer/annotate_functions.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_tables.sql",
        "optimizer/simplify.sql",
    ],
    "risingwave": ["optimizer/qualify_columns.sql"],
    "snowflake": [
        "optimizer/annotate_functions.sql",
        "optimizer/annotate_types.sql",
        "optimizer/canonicalize_internal_names.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/optimizer.sql",
        "optimizer/pushdown_predicates.sql",
        "optimizer/pushdown_projections.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_tables.sql",
        "optimizer/quote_identifiers.sql",
        "optimizer/simplify.sql",
    ],
    "spark": [
        "optimizer/annotate_functions.sql",
        "optimizer/annotate_types.sql",
        "optimizer/canonicalize_internal_names.sql",
        "optimizer/merge_subqueries.sql",
        "optimizer/normalize_identifiers.sql",
        "optimizer/optimizer.sql",
        "optimizer/qualify_columns.sql",
        "optimizer/qualify_columns_ddl.sql",
        "optimizer/qualify_tables.sql",
        "optimizer/simplify.sql",
    ],
    "spark2": ["optimizer/annotate_functions.sql"],
    "sqlite": ["optimizer/canonicalize.sql", "optimizer/normalize_identifiers.sql", "optimizer/simplify.sql"],
    "starrocks": ["optimizer/qualify_columns.sql", "optimizer/qualify_tables.sql"],
    "teradata": ["optimizer/normalize_identifiers.sql"],
    "trino": ["optimizer/annotate_functions.sql", "optimizer/normalize_identifiers.sql", "optimizer/pushdown_predicates.sql"],
    "tsql": ["optimizer/annotate_functions.sql", "optimizer/simplify.sql"],
}


def load_tests_metadata(tests_path: Path) -> dict[int, float]:
    if not tests_path.exists():
        return DEFAULT_TEST_DURATIONS
    with open(tests_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items = raw["tests"] if "tests" in raw else raw
    return {item["number"]: item.get("seconds", 0.001) for item in items}


def parse_patch(patch_text: str):
    files = {}
    current_file = None
    old_line = 0
    new_line = 0

    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            raw_path = line[6:].strip().replace("\\", "/")
            if raw_path.startswith("sqlglot_repo/"):
                raw_path = raw_path[14:]
            current_file = raw_path
            files[current_file] = {
                "modified_lines": set(),
                "added_code_lines": [],
                "deleted_code_lines": [],
            }
        elif line.startswith("@@ ") and current_file:
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
        elif current_file:
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                s = content.strip()
                if s and not s.startswith("#"):
                    files[current_file]["modified_lines"].add(new_line)
                files[current_file]["added_code_lines"].append((new_line, content))
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                content = line[1:]
                s = content.strip()
                if s and not s.startswith("#"):
                    files[current_file]["modified_lines"].add(old_line)
                files[current_file]["deleted_code_lines"].append((old_line, content))
                old_line += 1
            else:
                old_line += 1
                new_line += 1

    return files


def is_comment_or_whitespace_only(added_lines, deleted_lines) -> bool:
    for _, content in added_lines:
        s = content.strip()
        if s and not s.startswith("#"):
            return False
    for _, content in deleted_lines:
        s = content.strip()
        if s and not s.startswith("#"):
            return False
    return True


def select_tests_for_patch(patch_text: str) -> list[int]:
    files = parse_patch(patch_text)
    candidate_tests = set()

    all_comment_only = True
    for file_path, info in files.items():
        if not is_comment_or_whitespace_only(info["added_code_lines"], info["deleted_code_lines"]):
            all_comment_only = False
            break

    if all_comment_only:
        return []

    for file_path, info in files.items():
        matched_fixture = False
        for fix_key, tnums in FIXTURE_MAP.items():
            if file_path.endswith(fix_key) or fix_key.endswith(file_path):
                candidate_tests.update(tnums)
                matched_fixture = True
                break
        if matched_fixture:
            continue

        if file_path == "sqlglot/serde.py":
            for fix_tnums in FIXTURE_MAP.values():
                candidate_tests.update(fix_tnums)
            candidate_tests.update(FILE_MAP.get(file_path, []))
            continue

        if not info["modified_lines"]:
            continue

        file_lines = LINE_MAP.get(file_path, {})
        hit_any_line = False
        for ln in info["modified_lines"]:
            tnums = file_lines.get(str(ln))
            if tnums:
                candidate_tests.update(tnums)
                hit_any_line = True

        if not hit_any_line:
            candidate_tests.update(FILE_MAP.get(file_path, []))

        for prefix in ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/"):
            if file_path.startswith(prefix):
                dialect_name = file_path[len(prefix):].replace(".py", "")
                if dialect_name in DIALECT_OPTIMIZER_FIXTURES:
                    for fix_name in DIALECT_OPTIMIZER_FIXTURES[dialect_name]:
                        candidate_tests.update(FIXTURE_MAP.get(fix_name, []))
                break

    needs_executor = any("executor" in f or "serde" in f or "tpc" in f or "eliminate_joins" in f for f in files)
    if needs_executor:
        if 1440 in candidate_tests:
            candidate_tests.add(1106)
        if 1441 in candidate_tests:
            candidate_tests.add(1107)

    if 1224 in candidate_tests and not any(f == "sqlglot/generator.py" or f == "tests/test_generator.py" for f in files):
        candidate_tests.remove(1224)
    if 1525 in candidate_tests and not any(f == "sqlglot/parser.py" or f == "tests/test_parser.py" for f in files):
        candidate_tests.remove(1525)

    return sorted(candidate_tests)


def partition_tests(test_nums: list[int], durations: dict[int, float], max_workers: int = 8) -> list[list[int]]:
    if not test_nums:
        return []

    sorted_tests = sorted(test_nums, key=lambda n: durations.get(n, 0.001), reverse=True)
    total_duration = sum(durations.get(n, 0.001) for n in test_nums)

    best_workers = [sorted_tests]
    best_cost = total_duration + 2.0

    if total_duration <= 2.0:
        return [sorted_tests]

    for k in range(1, min(max_workers, len(sorted_tests)) + 1):
        worker_buckets = [[] for _ in range(k)]
        worker_loads = [0.0] * k

        for t in sorted_tests:
            dur = durations.get(t, 0.001)
            min_idx = 0
            min_load = worker_loads[0]
            for j in range(1, k):
                if worker_loads[j] < min_load:
                    min_load = worker_loads[j]
                    min_idx = j
            worker_buckets[min_idx].append(t)
            worker_loads[min_idx] += dur

        actual_buckets = [b for b in worker_buckets if b]
        cost = max(worker_loads) + 2.0 * len(actual_buckets)
        if cost < best_cost:
            best_cost = cost
            best_workers = actual_buckets

    return best_workers


def main() -> None:
    tests_path = DATA_DIR / "tests.json"
    durations = load_tests_metadata(tests_path)

    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    for patch_path in patch_files:
        commit_id = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8", errors="ignore")
        selected_tests = select_tests_for_patch(patch_text)
        workers = partition_tests(selected_tests, durations)
        line = json.dumps({"commit": commit_id, "workers": workers})
        sys.stdout.write(line + "\n")

    sys.stderr.write(f"Processed {len(patch_files)} commits successfully.\n")


if __name__ == "__main__":
    main()
