"""Break each protection of the judging in turn and see that a test fails.

For every entry of protections.toml this makes a copy of the bench folder, puts the entry's broken
variant into the copy, and runs the tests the entry names there. A variant under which they all
pass means that nothing pins that protection: the check fails and says which. Before that, the
tests named must all pass with every protection in place.

An entry marked as a second line of defence names other protections that already stop the attack.
Its tests are attacks, and with its variant in place they must still pass: the attack still gets
nothing. A second line whose tests fail without it is no second line, and the check says so.

    python experiments/check_protections.py            # every protection
    python experiments/check_protections.py "memory"   # only those whose description holds the word

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
        if change["find"] not in text:
            raise SystemExit(f"{protection['what']}: the text to break is not in {protection['file']} any more")
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
    return {"what": protection["what"], "verdict": verdict, "seconds": round(time.monotonic() - started, 1),
            "tests": protection["tests"], "pytest": last}


def main(argv: list[str]) -> int:
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
