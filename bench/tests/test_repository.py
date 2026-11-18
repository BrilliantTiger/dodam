"""Checks on what the repository tracks: no compiled files and no paths from someone's machine.

A compiled Python file embeds the full path of its source, and a home-directory path carries an
account name. Neither belongs in a public repository.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEP = "/"
# Home-directory paths, written so that this file does not contain one itself.
HOME_PATH = re.compile((SEP + "(?:Users|home)" + SEP + r"[A-Za-z0-9._-]+" + SEP).encode())
# Names that are not a person: accounts that exist on every build machine or container.
GENERIC = {b"runner", b"user", b"app", b"agent", b"nonroot", b"node", b"ubuntu", b"dodam", b"you", b"me",
           b"somebody-local"}   # the last is the made-up name in the pilot record's tests


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def tracked() -> list[str]:
    result = git("ls-files", "-z")
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [name for name in result.stdout.split("\0") if name]


def test_no_compiled_file_is_tracked():
    assert [n for n in tracked() if n.endswith((".pyc", ".pyo")) or "__pycache__" in n.split("/")] == []


@pytest.mark.parametrize("path", [
    "__pycache__/a.cpython-313.pyc", "tools/site/__pycache__/build.cpython-312.pyc", "tools/new/__pycache__/x.pyc",
    "bench/dodambench/__pycache__/task.cpython-313.pyc", "anywhere/else/module.pyc", "site/__pycache__/x.pyc",
])
def test_compiled_files_are_ignored_everywhere(path):
    tracked()
    assert git("check-ignore", "-q", "--no-index", path).returncode == 0, f"{path} is not ignored"


def test_no_tracked_file_holds_a_home_directory_path():
    found = []
    for name in tracked():
        path = ROOT / name
        if not path.is_file():
            continue
        for match in HOME_PATH.finditer(path.read_bytes()):
            if match.group(0).split(SEP.encode())[2] not in GENERIC:
                found.append(name)
                break
    assert found == []
