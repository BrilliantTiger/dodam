"""Measure how far a program can pass its memory limit before the judge ends it.

The judge measures the container's memory as often as it gets to run, at most every two thousandths
of a second, and ends a program above its limit. A program that tries can be above the limit between
two measurements. This runs programs that try, each several times, under the judge as the verifier
runs it: a container of the Python image without network, capped in memory and processes.

    python experiments/memory_overshoot.py > overshoot.json
    python experiments/memory_overshoot.py --quick     # every row once: whether the table still comes out

The script ends with an error when a row does not come out; the automatic check runs the quick mode.

For every run it records, from inside the container:

- the overshoot's base: everything the container held when the run began (the kernel's own count);
- the highest value the kernel kept for the container during the run, less that base: what the
  program reached, the cases' files and the cache of files on disk included;
- how often the judge measured during the run and the longest time between two measurements;
- whether the kernel had to end a process, and how the case was judged.

Each program runs under a limit of 256 MB in a container of 1,024 MB and in one of 640 MB, and
once more in a container of 640 MB under a limit the container cannot give: there the only stop is
the line at which the judge ends a program whatever its limit, 128 MB below the cap, at 512 MB.
The last setting takes that line out as well, to show what it is for: there the kernel has to end
a process, and the table says in how many runs the case still failed and the others were judged.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))
from dodambench.convert import TEST_SH  # noqa: E402

IMAGE = "dodam-bench/python:dev"
LIMITS = {"CASE_TIMEOUT_SEC": "5", "CASE_MEMORY_MB": "256", "CASE_OUTPUT_KB": "256", "CASE_PROCESSES": "16",
          "CASE_DISK_MB": "32"}
RUNS = 20
SETTINGS = ((1024, 256, True), (640, 256, True), (640, 100000, True), (640, 100000, False))
# container and limit in MB, and whether the judge ends a program that leaves the container too little memory

HEAD = r'''
import ctypes, os, sys, threading, time
MB = 1024 * 1024
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
if sys.argv[1:] == ["plain"]:
    print("plain")
    sys.exit(0)


def touch(block):
    for i in range(0, len(block), 4096):
        block[i] = 1
'''

PROGRAMS = {
    "one process, block after block": r'''
blocks = []
while True:
    blocks.append(bytearray(16 * MB))
    touch(blocks[-1])
''',
    "eight processes, 64 MB each": r'''
for _ in range(8):
    if os.fork() == 0:
        block = bytearray(64 * MB)
        touch(block)
        time.sleep(60)
        os._exit(0)
time.sleep(60)
''',
    "two threads asking for memory the kernel fills at once": r'''
def take():
    while True:
        libc.mmap(None, 8 * MB, 3, 0x02 | 0x20 | 0x8000, -1, 0)


for _ in range(2):
    threading.Thread(target=take).start()
''',
    "fifteen processes that take memory at the same moment": r'''
go, start = os.pipe()
for _ in range(15):
    if os.fork() == 0:
        os.read(go, 1)
        block = bytearray(b"x") * (64 * MB)
        time.sleep(60)
        os._exit(0)
os.write(start, b"x" * 15)
time.sleep(60)
''',
}

HIDDEN = '''
import json, os, time
from dodam_cli import case


def number(name):
    return int(open("/sys/fs/cgroup/" + name).read())


def kills():
    return [int(l.split()[1]) for l in open("/sys/fs/cgroup/memory.events") if l.startswith("oom_kill ")][0]


def test_warm_up():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_the_program_that_tries():
    with case() as c:
        held, high, ended = number("memory.current"), number("memory.peak"), kills()
        r = c.run(["try"])
        with open("/logs/verifier/overshoot.json", "w") as f:
            json.dump({"held_before_mb": held / 2**20, "high_before_mb": high / 2**20,
                       "high_after_mb": number("memory.peak") / 2**20, "limit": r.limit,
                       "ended_by_the_kernel": kills() - ended, "seconds": r.seconds, **c.measured}, f)
        assert r.exit_code == 0


def test_afterwards():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
'''


def judged(program: str, memory_mb: int, limit_mb: int, stop: bool) -> dict:
    """One judging of the program in a fresh container; what the hidden test recorded, and the outcomes."""
    with tempfile.TemporaryDirectory(prefix="dodam-overshoot-") as work:
        tests, solution, out = Path(work) / "tests", Path(work) / "solution", Path(work) / "out"
        (tests / "lib").mkdir(parents=True)
        (tests / "functional").mkdir()
        solution.mkdir()
        out.mkdir()
        (solution / "main.py").write_text(HEAD + program, encoding="utf-8")
        for name, target in (("judge.py", "dodam_judge.py"), ("dodam_cli.py", "dodam_cli.py")):
            shutil.copy(BENCH / "dodambench" / name, tests / "lib" / target)
        if not stop:
            judge = tests / "lib/dodam_judge.py"
            text = judge.read_text(encoding="utf-8")
            assert text.count("MEMORY_RESERVE_MB = 128") == 1
            judge.write_text(text.replace("MEMORY_RESERVE_MB = 128", "MEMORY_RESERVE_MB = 0"), encoding="utf-8")
        (tests / "functional/test_overshoot.py").write_text(HIDDEN, encoding="utf-8")
        (tests / "manifest.toml").write_text(
            '[[test]]\nfile = "functional/test_overshoot.py"\nkind = "functional"\nstep = "build"\n', encoding="utf-8")
        env = {"STEPS": "build", "INTERFACE": "cli", "BUILD_CMD": "", "RUN_CMD": "python3 {app}/main.py", **LIMITS,
               "CASE_MEMORY_MB": str(limit_mb), "DEPS_DIR": "", "DEPS_LINKS": ""}
        (tests / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
        (tests / "test.sh").write_text(TEST_SH, encoding="utf-8")
        name = f"dodam-overshoot-{uuid.uuid4().hex[:12]}"
        script = ("mkdir -p /logs/verifier && cp -R /solution/. /app/ && printf 'DODAM_STEP=build\\n' > /tests/step.env "
                  "&& exec sh /tests/test.sh")
        try:
            subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0",
                            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m", "--cpus", "2",
                            "--pids-limit", "512", "--tmpfs",
                            "/dodam-cases:rw,exec,nosuid,nodev,size=32m,nr_inodes=20000,mode=0711",
                            "-e", "DODAM_APP_USER=agent", IMAGE, "sh", "-c", script], check=True, capture_output=True)
            subprocess.run(["docker", "cp", f"{tests}/.", f"{name}:/tests"], check=True, capture_output=True)
            subprocess.run(["docker", "cp", f"{solution}/.", f"{name}:/solution"], check=True, capture_output=True)
            done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, timeout=300)
            subprocess.run(["docker", "cp", f"{name}:/logs/verifier/.", str(out)], capture_output=True)
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
        seen = json.loads((out / "overshoot.json").read_text()) if (out / "overshoot.json").is_file() else {}
        cases = (json.loads((out / "details.json").read_text())["tests"] if (out / "details.json").is_file() else [])
        return {**seen, "exit_code": done.returncode, "outcomes": [c["outcome"] for c in cases]}


def main(runs_each: int = RUNS) -> None:
    table = []
    for memory_mb, limit_mb, stop in SETTINGS:
        for name, program in PROGRAMS.items():
            runs = [judged(program, memory_mb, limit_mb, stop) for _ in range(runs_each)]
            reached = sorted(r["high_after_mb"] - r["held_before_mb"] for r in runs if "high_after_mb" in r)
            row = {"container_mb": memory_mb, "limit_mb": limit_mb, "stop_128_mb_below_the_cap": stop,
                   "program": name, "runs": len(runs), "measured_runs": sum("high_after_mb" in r for r in runs),
                   "judged_as_meant": sum(r["outcomes"] == ["passed", "failed", "passed"] and r["exit_code"] == 0
                                          for r in runs),
                   "ended_for_memory": sum(r.get("limit") == "memory" for r in runs),
                   "kernel_had_to_end_a_process": sum(r.get("ended_by_the_kernel", 0) > 0 for r in runs),
                   "reached_mb": {"lowest": round(reached[0]), "median": round(statistics.median(reached)),
                                  "highest": round(reached[-1])} if reached else None,
                   "highest_in_container_mb": round(max((r.get("high_after_mb", 0) for r in runs), default=0)),
                   "longest_gap_ms": round(max((r.get("longest_gap", 0) for r in runs), default=0) * 1000, 1),
                   "fewest_measurements": min((r.get("measurements", 0) for r in runs), default=0)}
            table.append(row)
            print(f"{memory_mb:5} MB  limit {limit_mb:6}  stop {stop!s:5}  {name:56}  {row['reached_mb']}  gap {row['longest_gap_ms']} ms  "
                  f"kernel {row['kernel_had_to_end_a_process']}  as meant {row['judged_as_meant']}", file=sys.stderr, flush=True)
    # a row has come out when every run handed back what it measured and every case was judged
    missing = [f"{row['container_mb']} MB, limit {row['limit_mb']}, {row['program']}" for row in table
               if row["reached_mb"] is None or row["measured_runs"] != row["runs"] or row["judged_as_meant"] != row["runs"]]
    print(json.dumps({"runs_each": runs_each, "rows": table, "rows_that_did_not_come_out": missing}, indent=1))
    if missing:
        raise SystemExit("a row did not come out: " + "; ".join(missing))


if __name__ == "__main__":
    main(1 if sys.argv[1:2] == ["--quick"] else RUNS)
