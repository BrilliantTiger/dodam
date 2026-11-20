"""Measure how long the judge takes to walk and to remove a tree that a program's side left, by layout and size.

The judge walks or removes what the program's side left in /app, where a dependency folder goes, in
the memory-backed folders and in a served program's data folder. It gives up after 500,000 entries,
and all its walking and removing in one step shares one time limit. This makes trees of four layouts
on the container's own disk, at sizes up to that bound, and times the judge's own walk and removal
of each, as root in one container of the Python image capped as a judged one is: no network, 1 GB of
memory with no swap beyond it, 64 processes and 2 processors.

    python experiments/removal_time.py > removal.json
    python experiments/removal_time.py --quick        # every layout once, small: whether the table comes out
    python experiments/removal_time.py --judge OTHER_JUDGE.py --layouts "empty folders in one folder"

It prints one JSON object: for each layout and size, the entries, the seconds the walk took, the
seconds the removal took, and how much memory each held above what the process held before it
(each runs in a process of its own). The walk visits each entry and does nothing with it. The script
ends with an error when a row does not come out: a tree that is not removed whole, or a container
that does not run.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
JUDGE = HERE.parents[1] / "dodambench" / "judge.py"
IMAGE = "dodam-bench/python:dev"
CAPS = ["--network", "none", "--memory", "1g", "--memory-swap", "1g", "--pids-limit", "64", "--cpus", "2"]
LAYOUTS = ("files in one folder", "empty folders in one folder", "half files and half empty folders in one folder",
           "chains of 1,000 folders inside each other")
CHAIN = 1_000   # the container's disk refuses folders nested much deeper than a few thousand
SIZES = (10_000, 40_000, 160_000, 500_000)
QUICK = (1_000,)


def make_tree(top: str, layout: str, entries: int) -> None:
    """A folder holding `entries` entries in the given layout. Folders inside each other are made
    through open folders, a chain at a time."""
    import os
    os.mkdir(top)
    first = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    here = os.dup(first)
    try:
        for n in range(entries):
            if layout == "files in one folder" or (layout.startswith("half") and n % 2):
                os.close(os.open(str(n), os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=here))
            elif layout.startswith("chains"):
                if n % CHAIN == 0:
                    os.close(here)
                    here = os.dup(first)
                name = str(n // CHAIN) if n % CHAIN == 0 else "d"
                os.mkdir(name, dir_fd=here)
                below = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
                os.close(here)
                here = below
            else:
                os.mkdir(str(n), dir_fd=here)
    finally:
        os.close(here)
        os.close(first)


def resident_kb() -> int:
    """What this process holds in memory now, in KB; 0 where the system does not say."""
    try:
        lines = Path("/proc/self/status").read_text().splitlines()
    except OSError:
        return 0
    return next((int(line.split()[1]) for line in lines if line.startswith("VmRSS:")), 0)


def timed(work) -> tuple[float, float]:
    """Run work() in a process of its own: the seconds it took and the MB it held above its start."""
    import os
    import time
    before = resident_kb()
    began = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            work()
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status, usage = os.wait4(pid, 0)
    took = time.monotonic() - began
    if os.waitstatus_to_exitcode(status) != 0:
        raise SystemExit(f"the work ended with an error (status {status})")
    return round(took, 2), round(max(usage.ru_maxrss - before, 0) / 1024, 1)


def inside(judge_path: str, layouts: list[str], sizes: list[int], where: str) -> None:
    """What runs in the container: make each tree, walk it, remove it, check that nothing is left.
    Each row is printed as a line of JSON when it is done."""
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("judge", judge_path)
    judge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(judge)
    rows = 0
    for layout in layouts:
        for entries in sizes:
            rows += 1
            top = os.path.join(where, f"tree-{rows}")
            make_tree(top, layout, entries)
            walk_seconds, walk_mb = timed(lambda: judge.walk_tree(top, lambda folder, name, info: None))
            remove_seconds, remove_mb = timed(lambda: judge.remove_tree(top))
            if os.path.lexists(top):
                raise SystemExit(f"{layout}, {entries} entries: the tree was not removed whole")
            print(json.dumps({"layout": layout, "entries": entries, "walk_seconds": walk_seconds,
                              "walk_mb": walk_mb, "remove_seconds": remove_seconds, "remove_mb": remove_mb}),
                  flush=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--quick", action="store_true", help="every layout once, with 1,000 entries")
    p.add_argument("--judge", type=Path, default=JUDGE, help="the judge whose walk and removal are timed")
    p.add_argument("--layouts", default=";".join(LAYOUTS), help="the layouts, separated by semicolons")
    p.add_argument("--sizes", default="", help="the sizes, separated by commas")
    p.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    layouts = [name for name in LAYOUTS if name in a.layouts.split(";")]
    sizes = [int(n) for n in a.sizes.split(",") if n] or list(QUICK if a.quick else SIZES)
    if a.inside:
        inside(str(a.judge), layouts, sizes, tempfile.mkdtemp(prefix="trees-"))
        return
    name = f"dodam-removal-{secrets.token_hex(6)}"
    work = Path(tempfile.mkdtemp(prefix="dodam-removal-"))
    try:
        shutil.copy(a.judge, work / "judge.py")
        shutil.copy(HERE, work / "removal_time.py")
        subprocess.run(["docker", "create", "--name", name, *CAPS, "--user", "0", IMAGE, "/opt/judge/bin/python",
                        "/measure/removal_time.py", "--inside", "--judge", "/measure/judge.py",
                        "--layouts", ";".join(layouts), "--sizes", ",".join(map(str, sizes))],
                       check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{work}/.", f"{name}:/measure"], check=True, capture_output=True)
        done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, text=True, timeout=3000)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    rows = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    print(json.dumps({"image": IMAGE, "caps": " ".join(CAPS), "rows": rows}, indent=1))
    if done.returncode != 0 or len(rows) != len(layouts) * len(sizes):
        raise SystemExit(f"a row did not come out (exit code {done.returncode}): {done.stderr[-2000:]}")


if __name__ == "__main__":
    main()
