"""The two honest cases of the attacks job, ten times over in one job, at one commit.

    python repeat.py keys          every key: KEY-n at the mended head and at the head before, in turn
    python repeat.py ref KEY       the commit of the key
    python repeat.py run REPO      the two tests ten times in REPO, each time in a process of its own

Each time both tests run once: the case after one that made many mappings, and one turn of the reading from
outside (every turn of it is the same judging). The last line, REPEAT and a line of JSON, gives each test's
outcome every time and, for a failure, the end of what pytest said. The job fails where any time failed."""
import json
import subprocess
import sys
from pathlib import Path

REFS = {"mended": "627d352966f20a4f6f52101ac3d366bb77b99cce", "before": "c6cbd91ff8b9c09fb2db05074dfd97b3e6deeebc"}
TESTS = ("tests/test_cli_isolation.py::test_the_case_after_one_that_made_many_mappings_passes_and_says_what_its_wait_saw",
         "tests/test_cli_isolation.py::test_the_reading_from_outside_shows_what_a_program_holds_and_nothing_the_kernel_can_give_back[1]")
TIMES = 10
JOBS = 20


def main(argv):
    if argv[0] == "keys":
        keys = [f"{name}-{n}" for n in range(1, JOBS + 1) for name in ("before", "mended")]
        print("keys=" + json.dumps(keys))
        return 0
    if argv[0] == "ref":
        print("ref=" + REFS[argv[1].rsplit("-", 1)[0]])
        return 0
    repo = Path(argv[1]).resolve()
    times = []
    for n in range(TIMES):
        seen = {}
        for node in TESTS:
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node], cwd=repo / "bench",
                                  capture_output=True, text=True)
            name = "mappings" if "mappings" in node else "reading"
            seen[name] = "passed" if done.returncode == 0 else "failed"
            if done.returncode != 0:
                seen[name + "_said"] = (done.stdout + done.stderr)[-4000:]
                print(f"--- time {n + 1}, {name}: failed\n" + (done.stdout + done.stderr)[-12000:])
        times.append(seen)
        print(f"time {n + 1}: " + ", ".join(f"{k} {v}" for k, v in seen.items() if not k.endswith("_said")), flush=True)
    record = {"times": [{k: v for k, v in t.items() if not k.endswith("_said")} for t in times],
              "failed": {name: sum(t[name] == "failed" for t in times) for name in ("mappings", "reading")}}
    print("REPEAT " + json.dumps(record))
    return 1 if any(record["failed"].values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
