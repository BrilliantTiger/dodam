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
    if (BENCH.parent / "docs").is_dir():
        # the tests of the converter read the document of command-line tasks beside the bench folder
        shutil.copytree(BENCH.parent / "docs", where / "docs", ignore=SKIP)
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


# The tests of the judge and of the vetting tool that need no image, those of the formats and of the converters,
# which reach the judge's sum of time and what a task may say, and how many processes run them at once.
JUDGE_TESTS = ["tests/test_judge.py", "tests/test_cli_judge.py", "tests/test_cli_test_code.py", "tests/test_vet.py",
               "tests/test_cli_format.py", "tests/test_task_format.py", "tests/test_convert.py", "tests/test_cli_convert.py"]
NO_IMAGE = "not test_vet_in_the_language_image"
PARTS = 8
# tests that need the judge to read many times within a fraction of a second: they have room in themselves,
# and run on their own after the others all the same, so that no verdict rests on how busy the machine is
ALONE = ("test_a_figure_is_made_of_readings_that_belong_together",)


def run_judge_tests(cwd: Path) -> dict:
    """Every test of JUDGE_TESTS that needs no image, run in the folder `cwd`: in PARTS processes at once,
    then the tests named in ALONE in one. Whether they could be listed, how each run ended, and the
    tests that failed."""
    listed = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "-k",
                             NO_IMAGE, *JUDGE_TESTS], cwd=cwd, capture_output=True, text=True)
    nodes = [line for line in listed.stdout.splitlines() if "::" in line]
    if listed.returncode != 0 or not nodes:
        return {"listed": False, "codes": [], "failed": [], "said": [listed.stdout[-4000:] + listed.stderr[-2000:]], "tests": 0}
    alone = [node for node in nodes if any(name in node for name in ALONE)]
    rest = [node for node in nodes if node not in alone]
    said, codes = [], []
    for group in ([rest[i::PARTS] for i in range(PARTS)], [alone]):
        runs = [subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *part], cwd=cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for part in group if part]
        try:
            said += [run.communicate(timeout=HANGS)[0] for run in runs]
        except subprocess.TimeoutExpired:
            for run in runs:
                run.kill()
            return {"listed": True, "codes": [None], "failed": [], "said": ["the tests did not end"], "tests": len(nodes)}
        codes += [run.returncode for run in runs]
    failed = [line.split(" ", 1)[1].split(" - ")[0] for out in said for line in out.splitlines()
              if line.startswith(("FAILED ", "ERROR "))]
    return {"listed": True, "codes": codes, "failed": failed, "said": said, "tests": len(nodes)}


def verdict_of(run: dict) -> str:
    """A second line holds only where its tests were listed, every run of them started and ended passed. A
    run that ended otherwise than passed or failed, or no run at all, shows nothing, and holds nothing."""
    if not run["listed"]:
        return "the tests could not be listed"
    if not run["codes"] or any(code not in (0, 1) for code in run["codes"]):
        return "the tests did not run through"
    if run["failed"] or any(code != 0 for code in run["codes"]):
        return "NO SECOND LINE"
    return "second line"


def against_judge_tests(protection: dict) -> dict:
    """One second line taken out, and the tests of JUDGE_TESTS that need no image run against it."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dodam-second-line-") as where:
        run = run_judge_tests(broken_copy(protection, Path(where)))
    verdict = verdict_of(run)
    if verdict != "second line":
        print("\n".join(out[-4000:] for out in run["said"]), file=sys.stderr, flush=True)
    return {"what": protection["what"], "verdict": verdict, "seconds": round(time.monotonic() - started, 1),
            "tests": run["tests"], "failed": run["failed"]}


def second_lines() -> int:
    """Every second line against the tests of JUDGE_TESTS that need no image. They are first run the same
    way with nothing taken out: where one fails there, no second line is judged by them."""
    chosen = [p for p in protections() if p.get("second_line")]
    with tempfile.TemporaryDirectory(prefix="dodam-second-line-") as where:
        whole = run_judge_tests(broken_copy({"what": "nothing taken out", "file": "experiments/protections.toml",
                                             "change": []}, Path(where)))
    if verdict_of(whole) != "second line":
        print("\n".join(out[-4000:] for out in whole["said"]), file=sys.stderr, flush=True)
        print(json.dumps({"second_lines": len(chosen), "held": 0,
                          "not_held": [{"what": "the tests do not pass with nothing taken out",
                                        "verdict": verdict_of(whole), "failed": whole["failed"]}]}, indent=1))
        return 1
    print(f"{'nothing taken out':22}  {whole['tests']} tests pass", file=sys.stderr, flush=True)
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
