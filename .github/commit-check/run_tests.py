"""Runs the tests of the commit check in four processes at once, so that its job stays under a minute.

It finds every test the way unittest's discovery does, splits them by class into four shares, runs each share in a
process of its own and fails when a share fails, or when the tests that ran are not exactly the tests that were found
(it never passes having run none).

    python3 -I .github/commit-check/run_tests.py [FOLDER]
"""

import concurrent.futures
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARES = 4


def ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()


def run(job):
    folder, share = job
    code = ("import sys, unittest; sys.path.insert(0, sys.argv[1]); "
            "unittest.main(module=None, argv=['unittest', *sys.argv[2:]])")
    done = subprocess.run([sys.executable, "-I", "-c", code, str(folder), *share], capture_output=True, text=True,
                          cwd=folder)
    ran = re.search(r"^Ran (\d+) tests?", done.stderr, re.M)
    return done.returncode, int(ran.group(1)) if ran else 0, done.stderr


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    folder = Path(args[0]).resolve() if args else HERE
    sys.path.insert(0, str(folder))
    found = list(ids(unittest.TestLoader().discover(str(folder))))
    if not found or any("_FailedTest" in test or "loader" in test for test in found):
        print("::error::the tests of the commit check could not be found")
        return 1
    classes = {}
    for test in found:
        classes.setdefault(test.rsplit(".", 1)[0], []).append(test)
    shares = [[] for _ in range(SHARES)]
    for tests in sorted(classes.values(), key=len, reverse=True):
        min(shares, key=len).extend(tests)
    with concurrent.futures.ThreadPoolExecutor(max_workers=SHARES) as pool:
        results = list(pool.map(run, [(folder, share) for share in shares if share]))
    for code, count, output in results:
        print(output)
    ran = sum(count for _, count, _ in results)
    if ran != len(found):
        print(f"::error::{len(found)} tests were found and {ran} ran")
        return 1
    if any(code != 0 for code, _, _ in results):
        return 1
    print(f"{ran} tests of the commit check passed in {SHARES} processes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
