"""One break of the judge's code, run as the job of the second lines runs the judge's tests.

The list (breaks.jsonl, next to this file) gives each break a key, the commit whose tree it is made in, the file,
where the text to break begins and ends, that text and what takes its place. A key that ends in "none" breaks
nothing: with it every test must pass.

The tests are those the job of the second lines runs: the eight files of tests of the judge, the vetting tool,
the formats and the converters, but for the test that needs the language's image, listed once with nothing
broken, in eight processes at once, then the tests that need many readings in one process after them.

How a break is caught: a test fails (or a run of the tests ends with an error of its own), and the job fails
with it; or a test does not end: a run of the tests still going LIMIT seconds after the tests began is ended,
and in each process still going the test it had not finished is named. With nothing broken all the tests end
in well under two minutes on the machine of the automatic check. Where no test fails and every run ends, the
break is not caught and the job passes. The last line the job prints, RECORD and a line of JSON, says which.

    python run.py keys                 every key of the list, for the jobs
    python run.py ref KEY              the commit of the key's tree
    python run.py list KEY REPO        the tests, listed in REPO with nothing broken
    python run.py break KEY REPO       the break, made in REPO
    python run.py test KEY REPO        the tests, run in REPO"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE_TESTS = ["tests/test_judge.py", "tests/test_cli_judge.py", "tests/test_cli_test_code.py", "tests/test_vet.py",
               "tests/test_cli_format.py", "tests/test_task_format.py", "tests/test_convert.py", "tests/test_cli_convert.py"]
NO_IMAGE = "not test_vet_in_the_language_image"
PARTS = 8
ALONE = ("test_a_figure_is_made_of_readings_that_belong_together",)
LIMIT = 420
OUTCOMES = (" PASSED", " FAILED", " ERROR", " SKIPPED", " XFAIL", " XPASS")


def item_of(key: str) -> dict:
    for line in (HERE / "breaks.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["key"] == key:
            return item
    raise SystemExit(f"no break with the key {key}")


def listed_file(repo: Path) -> Path:
    return repo.parent / "tests-listed.txt"


def list_tests(repo: Path) -> None:
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "-k",
                           NO_IMAGE, *JUDGE_TESTS], cwd=repo / "bench", capture_output=True, text=True)
    nodes = [line for line in done.stdout.splitlines() if "::" in line]
    if done.returncode != 0 or not nodes:
        raise SystemExit("the tests could not be listed with nothing broken:\n" + done.stdout[-4000:] + done.stderr[-2000:])
    listed_file(repo).write_text("\n".join(nodes) + "\n", encoding="utf-8")
    print(f"{len(nodes)} tests")


def make_break(item: dict, repo: Path) -> None:
    if "file" not in item:
        print("nothing broken")
        return
    path = repo / item["file"]
    text = path.read_text(encoding="utf-8")
    if text[item["start"]:item["end"]] != item["old"]:
        raise SystemExit("the text to break is not where the list says")
    path.write_text(text[:item["start"]] + item["new"] + text[item["end"]:], encoding="utf-8")
    print(f"{item['file']}, line {item['line']}, {item['kind']}: {item['old']!r} becomes {item['new']!r}")


def unfinished(part: list[str], said: str) -> str | None:
    """The first test of the part with no outcome in what its process wrote. A test's line begins with its
    name, which may hold spaces, and ends with its outcome."""
    lines = said.splitlines()
    for node in part:
        if not any(line.startswith(node + " ") and any(o in line[len(node):] for o in OUTCOMES) for line in lines):
            return node
    return None


def failures(said: str, code: int) -> list[str]:
    """The tests a finished process names as failed or in error, as the job of the second lines reads them."""
    named = [line.split(" ", 1)[1].split(" - ")[0] for line in said.splitlines() if line.startswith(("FAILED ", "ERROR "))]
    return named or ([f"a run that ended with exit code {code}"] if code != 0 else [])


def run_tests(repo: Path) -> dict:
    nodes = listed_file(repo).read_text(encoding="utf-8").split()
    alone = [node for node in nodes if any(name in node for name in ALONE)]
    rest = [node for node in nodes if node not in alone]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    outs = repo.parent / "outs"
    outs.mkdir(exist_ok=True)
    deadline = time.monotonic() + LIMIT
    failed, codes = [], []
    for g, group in enumerate(([rest[i::PARTS] for i in range(PARTS)], [alone])):
        group = [part for part in group if part]
        files = [outs / f"{g}-{i}.txt" for i in range(len(group))]
        runs = [subprocess.Popen([sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider", *part], cwd=repo / "bench",
                                 env=env, stdout=open(file, "w"), stderr=subprocess.STDOUT) for part, file in zip(group, files)]
        while any(run.poll() is None for run in runs) and time.monotonic() < deadline:
            time.sleep(0.5)
        hung = []
        for part, file, run in zip(group, files, runs):
            if run.poll() is None:
                run.kill()
                run.wait()
                hung.append(unfinished(part, file.read_text(errors="replace")) or "a test of the part, all of them reported")
                continue
            said = file.read_text(errors="replace")
            codes.append(run.returncode)
            failed += failures(said, run.returncode)
            if run.returncode != 0:
                print(said[-6000:])
        if hung:
            # a test that failed in a process that ended comes first; the tests that did not end are named too
            return {"how": "a failing test" if failed else "a test that does not end", "tests": failed or hung,
                    "did_not_end": hung, "codes": codes}
    return {"how": "a failing test" if failed else None, "tests": failed, "codes": codes}


def main(argv: list[str]) -> int:
    what = argv[0]
    if what == "keys":
        keys = [json.loads(line)["key"] for line in (HERE / "breaks.jsonl").read_text(encoding="utf-8").splitlines()]
        print("keys=" + json.dumps(keys))
        return 0
    item = item_of(argv[1])
    if what == "ref":
        print("ref=" + item["ref"])
        return 0
    repo = Path(argv[2]).resolve()
    if what == "list":
        list_tests(repo)
        return 0
    if what == "break":
        make_break(item, repo)
        return 0
    started = time.monotonic()
    record = run_tests(repo)
    record = {"key": item["key"], **record, "seconds": round(time.monotonic() - started, 1)}
    line = "RECORD " + json.dumps(record)
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"`{line}`\n")
    return 1 if record["how"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
