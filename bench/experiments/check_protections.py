"""Break each protection of the judging in turn and see that a test fails.

For every entry of protections.toml this makes a copy of the bench folder, puts the entry's broken
variant into the copy, and runs the tests the entry names there. A variant under which they all
pass means that nothing pins that protection: the check fails and says which. A protection counts
as pinned at the first of its tests that fails: the tests are stopped there, and the others the
entry names are not run for it. Before that, the tests named must all pass with every protection
in place.

An entry marked as a second line of defence names other protections that already stop the attack.
Its tests are attacks, and with its variant in place they must still pass: the attack still gets
nothing. A second line whose tests fail without it is no second line, and the check says so.

    python experiments/check_protections.py            # every protection
    python experiments/check_protections.py "memory"   # only those whose description holds the word
    python experiments/check_protections.py --second-lines   # every second line against the judge's tests

A second line is checked against more than the tests its entry names: with "--second-lines", every
second line is taken out in turn and all the tests of the judge and of the vetting tool that need no
image are run, in eight processes at once. A second line under which one of them fails is no second
line: that test catches it, and its entry must name it.

The tests that judge in a container need Docker and the Python language image. Where they are missing
those tests are skipped, which the check reports as a protection it could not check, and fails.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
HANGS = 900   # seconds after which the tests of one variant are ended; a variant must not hang the check
SKIP = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv", ".harbor-venv", "vet-out", "*.pyc")


def protections() -> list[dict]:
    return tomllib.loads((BENCH / "experiments/protections.toml").read_text(encoding="utf-8"))["protection"]


def broken_copy(protection: dict, where: Path) -> Path:
    """A copy of the bench folder with this protection's variant in it."""
    copy = where / "bench"
    shutil.copytree(BENCH, copy, ignore=SKIP)
    path = copy / protection["file"]
    text = path.read_text(encoding="utf-8")
    for change in protection["change"]:
        # one entry is one place: a text that occurs twice would take two places out together
        if text.count(change["find"]) != 1:
            raise SystemExit(f"{protection['what']}: the text to break must occur exactly once in "
                             f"{protection['file']}, and it occurs {text.count(change['find'])} times")
        text = text.replace(change["find"], change["put"])
    path.write_text(text, encoding="utf-8")
    return copy


def check(protection: dict) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dodam-protection-") as where:
        copy = broken_copy(protection, Path(where))
        try:
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-rs",
                                   *protection["tests"]], cwd=copy, capture_output=True, text=True, timeout=HANGS)
        except subprocess.TimeoutExpired:
            return {"what": protection["what"], "verdict": "the tests did not end", "seconds": HANGS,
                    "tests": protection["tests"], "pytest": ""}
    last = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr.strip()[-200:]
    second = bool(protection.get("second_line"))
    if done.returncode not in (0, 1):
        verdict = "the check itself failed"
    elif " skipped" in last and " passed" not in last and " failed" not in last:
        verdict = "could not be checked"
    elif second:
        verdict = "second line" if done.returncode == 0 else "NO SECOND LINE: a test fails without it"
    else:
        verdict = "pinned" if done.returncode == 1 else "NOT PINNED"
    if verdict not in ("pinned", "second line"):
        # what is needed to read the cause from the log: what the tests said
        print(done.stdout[-12000:], file=sys.stderr, flush=True)
    return {"what": protection["what"], "verdict": verdict, "seconds": round(time.monotonic() - started, 1),
            "tests": protection["tests"], "pytest": last}


# The tests of the judge and of the vetting tool that need no image, and how many processes run them at once.
JUDGE_TESTS = ["tests/test_judge.py", "tests/test_cli_judge.py", "tests/test_cli_test_code.py", "tests/test_vet.py"]
NO_IMAGE = "not test_vet_in_the_language_image"
PARTS = 8
# tests that need the judge to read many times within a fraction of a second: beside seven other processes on a
# machine of four processors they fail whatever is taken out, so they run on their own after the others
ALONE = ("test_a_figure_is_made_of_readings_that_belong_together",)


def judge_tests(cwd: Path) -> list[str] | None:
    """The node of every test of the judge and of the vetting tool that needs no image, in the folder `cwd`;
    None when they cannot all be listed."""
    listed = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "-k",
                             NO_IMAGE, *JUDGE_TESTS], cwd=cwd, capture_output=True, text=True)
    nodes = [line for line in listed.stdout.splitlines() if "::" in line]
    return nodes if listed.returncode == 0 and nodes else None


def against_judge_tests(protection: dict) -> dict:
    """One second line taken out, and every test of the judge and of the vetting tool that needs no image run
    against it, in PARTS processes at once and then the tests named in ALONE in one; the tests that fail."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dodam-second-line-") as where:
        copy = broken_copy(protection, Path(where))
        nodes = judge_tests(copy)
        if nodes is None:
            # a variant after which the tests cannot be listed shows nothing, so it holds nothing
            return {"what": protection["what"], "verdict": "the tests could not be listed",
                    "seconds": round(time.monotonic() - started, 1), "tests": 0, "failed": []}
        alone = [node for node in nodes if any(name in node for name in ALONE)]
        rest = [node for node in nodes if node not in alone]
        said, runs = [], []
        for group in ([rest[i::PARTS] for i in range(PARTS)], [alone]):
            started_group = [subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *part],
                                              cwd=copy, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                             for part in group if part]
            runs += started_group
            try:
                said += [run.communicate(timeout=HANGS)[0] for run in started_group]
            except subprocess.TimeoutExpired:
                for run in started_group:
                    run.kill()
                return {"what": protection["what"], "verdict": "the tests did not end", "seconds": HANGS, "failed": []}
    failed = [line.split(" ", 1)[1].split(" - ")[0] for out in said for line in out.splitlines()
              if line.startswith(("FAILED ", "ERROR "))]
    verdict = "second line" if runs and not failed and all(run.returncode == 0 for run in runs) else "NO SECOND LINE"
    if verdict != "second line":
        print("\n".join(out[-4000:] for out in said), file=sys.stderr, flush=True)
    return {"what": protection["what"], "verdict": verdict, "seconds": round(time.monotonic() - started, 1),
            "tests": len(nodes), "failed": failed}


def second_lines() -> int:
    """Every second line against every test of the judge and of the vetting tool that needs no image."""
    chosen = [p for p in protections() if p.get("second_line")]
    whole = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", NO_IMAGE,
                            *JUDGE_TESTS], cwd=BENCH, capture_output=True, text=True)
    if whole.returncode != 0:
        print(whole.stdout[-20000:], file=sys.stderr, flush=True)
        print(json.dumps({"second_lines": len(chosen), "held": 0,
                          "not_held": [{"what": "the tests do not pass with every protection in place"}]}, indent=1))
        return 1
    results = []
    for protection in chosen:
        result = against_judge_tests(protection)
        results.append(result)
        print(f"{result['verdict']:22}  {result['seconds']:6.1f}s  {result['what']}", file=sys.stderr, flush=True)
    bad = [r for r in results if r["verdict"] != "second line"]
    print(json.dumps({"second_lines": len(results), "held": len(results) - len(bad), "not_held": bad}, indent=1))
    return 1 if bad or not results else 0


def main(argv: list[str]) -> int:
    if argv == ["--second-lines"]:
        return second_lines()
    chosen = [p for p in protections() if not argv or any(word.lower() in p["what"].lower() for word in argv)]
    # a test that fails as the code is would seem to pin anything: first they must all pass unbroken
    named = sorted({test for p in chosen for test in p["tests"]})
    whole = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *named], cwd=BENCH,
                           capture_output=True, text=True)
    if whole.returncode != 0:
        # what failed is what the tests say, so it is shown as they say it
        print(whole.stdout[-20000:], file=sys.stderr, flush=True)
        last = whole.stdout.strip().splitlines()[-1] if whole.stdout.strip() else whole.stderr.strip()[-200:]
        print(json.dumps({"protections": len(chosen), "pinned": 0,
                          "not_pinned": [{"what": "the tests do not pass with every protection in place", "pytest": last}]},
                         indent=1))
        return 1
    results = []
    for protection in chosen:
        result = check(protection)
        results.append(result)
        print(f"{result['verdict']:22}  {result['seconds']:6.1f}s  {result['what']}", file=sys.stderr, flush=True)
    bad = [r for r in results if r["verdict"] not in ("pinned", "second line")]
    print(json.dumps({"protections": len(results), "pinned": sum(r["verdict"] == "pinned" for r in results),
                      "second_line": sum(r["verdict"] == "second line" for r in results), "not_pinned": bad}, indent=1))
    return 1 if bad or not results else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
