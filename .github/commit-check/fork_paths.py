"""Fails a pull request from a fork at once when it touches a path that the working rules close to outside pull
requests.

It needs no secret and no approval: it reads the names of the pull request's changed paths from GitHub's API as
data. It does not hold its own list of closed paths: the desk's tool on main (tools/desk/pr_files.py) holds the
closed folders, the exception for the introduction page's text and the allowance for new tests and new benchmark
tasks, with its own tests, and this script asks it. What it prints is the tool's verdict: the names of closed
folders from that list, never a path of the pull request.
"""

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools" / "desk"))

import check  # noqa: E402  (main's copy, beside this file)
import pr_files  # noqa: E402  (main's copy of the desk's tool)

PAGE = 100
MAX_PAGES = 30  # GitHub lists at most 3,000 files of a pull request


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print("::error::the check of a fork's paths was not called as its workflow calls it")
        sys.exit(2)


def listing(path, ref):
    """The names in a folder at a commit, as the desk's tool wants them."""
    data = check.api_get(f"contents/{path}?ref={ref}")
    if not isinstance(data, list):
        return None
    if len(data) >= pr_files.MAX_GITHUB_DIRECTORY_ENTRIES:
        return [entry.get("name") for entry in data if isinstance(entry, dict)]
    return [entry.get("name") for entry in data if isinstance(entry, dict) and entry.get("type") == "dir"]


def main(argv=None):
    parser = Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--number", required=True)
    parser.add_argument("--base-sha", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]{1,9}", args.number) or not re.fullmatch(r"[0-9a-f]{40}", args.base_sha):
        print("::error::the check of a fork's paths was not called as its workflow calls it")
        return 2
    try:
        verdict = judge(args.number, args.base_sha)
    except check.Problem as problem:
        print(f"::error::{problem}")
        return 1
    except Exception:
        print("::error::the check of a fork's paths stopped on something it does not know")
        return 1
    if verdict.get("refused") is True:
        print("::error::the paths of this pull request could not be read completely, so it is not judged as touching "
              "no closed path")
        return 1
    closed = [folder for folder in verdict.get("protected_folders", []) if folder in pr_files.PROTECTED_FOLDERS]
    if closed:
        print("::error::a pull request from outside does not change these folders: " + ", ".join(closed)
              + ". Report the problem in an issue; the project's own agents mend it there.")
        return 1
    print("This pull request touches no path that the working rules close to outside pull requests.")
    return 0


def judge(number, base):
    pull = check.api_get(f"pulls/{number}")
    if not isinstance(pull, dict):
        return {"refused": True}
    entries = []
    for page in range(1, MAX_PAGES + 1):
        files = check.api_get(f"pulls/{number}/files?per_page={PAGE}&page={page}")
        if not isinstance(files, list):
            return {"refused": True}
        entries.extend(files)
        if len(files) < PAGE:
            break
    tests = check.api_get("contents/bench/tests?ref=main")
    main_tests = [entry.get("name") for entry in tests if isinstance(entry, dict)] if isinstance(tests, list) else None
    base_ref = pull.get("base", {}).get("ref") if isinstance(pull.get("base"), dict) else None
    return pr_files.classify_pr_files(entries, changed_files=pull.get("changed_files"), draft=pull.get("draft"),
                                      base_ref=base_ref, task_folders=listing("bench/tasks", base),
                                      main_task_folders=listing("bench/tasks", "main"), main_test_entries=main_tests,
                                      repo_root=ROOT)


if __name__ == "__main__":
    sys.exit(main())
