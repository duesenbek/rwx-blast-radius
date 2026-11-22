# Blast Radius entry

## Current selector

The optimized Linux entry scores **94.904 simulated seconds with zero misses** on
the 30 supplied samples, down from 115.223 seconds for the previous selector. Three
local Linux/amd64 container runs under the contest limits gave a median score of
**96.9 seconds including all child-process CPU**. This is a sample result, not a hidden-board score.
Even selecting only the known failing sample tests costs 94.351 simulated seconds.

The image uses `selector.py`, `dependencies.json.gz`, `runtime_probe.py`,
`prepare_runtime.py` and `baseline.zip`; all must be committed before submitting.
The map contains baseline source, line coverage, dictionary-value reads,
class-setting dependencies, fixture reads, operand types and bounded observations
of primitive comparison operands, collected from the unmodified pinned source.
It contains no patch IDs, sample answers or precomputed failure sets. Runtime
selection never reads `answers.json` and requires no runtime network. Short dialect
suites are verified against the actual patch in independent forked processes, in
a temporary checkout under `/tmp`. Only a fully successful selected suite removes
its CI workers. Errors, missing dependencies and timeouts keep the static selection.
Tests retain their original implementations. The image installs pinned `pytz` from
PyPI and builds hash-validated bytecode before the charged run starts. SQLGlot's
source license is included in `SQLGLOT_LICENSE` and in the baseline archive.
Identity/equality normalization is limited to single-file changes with otherwise
identical syntax and operand types observed on the baseline whose equality is
identity-based. Comparison edits are evaluated against the stored primitive
operands at runtime; unknown values use the conservative selection. Dictionary
edits require unchanged keys and otherwise identical syntax. Copying a temporary
`table_` built from scalar inputs before `alias_` is treated as value-equivalent.
Other edits use dependency selection; hidden mutations may still
expose dependencies absent from baseline execution.

Local verification:

```sh
python verify_selector.py
python evaluate.py
docker build --platform linux/amd64 -t blast-radius-entry .
python benchmark.py
```

`evaluate.py` on Windows verifies the static selection (101.143 simulated seconds).
`benchmark.py` measures the complete Linux entry, including subprocess CPU, using
the contest's read-only filesystem, nobody user, 64 MB tmpfs, process and CPU limits.

To regenerate maps, obtain a clean checkout of
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa` as `sqlglot_repo`, install
`requirements.lock` and `coverage==7.17.4` in the pinned Linux/Python environment,
then run `python prepare_maps.py`. Collection executes the unmodified tests with
inline pools, preserving argument/result pickle round trips for process pools.
It requires `sample/tests.json` for test IDs, and does not read patches or labels.
Do not substitute thread pools in the source: optimizer process serialization is
a real dependency. The earlier local optimizer version is preserved in the
ignored `optimizer.local-backup.py` file.

An optimized submission for the Blast Radius track. The original challenge and
submission instructions follow.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
