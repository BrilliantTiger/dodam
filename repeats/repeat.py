"""At the head d808ba7: the two honest cases of the attacks job ten times over in each of 20 jobs (keys
"two-n"), and the whole attacks job once in each of 45 jobs (keys "whole-n").

    python repeat.py keys          every key
    python repeat.py ref KEY       the commit of the key
    python repeat.py run KEY REPO  the key's tests in REPO

The last line, REPEAT and a line of JSON, gives every time's outcome and, for a failure, the failed tests.
The job fails where anything failed."""
import json
import re
import subprocess
import sys
from pathlib import Path

HEAD = "d808ba7d500a15653dedd4e5f5cb8c16d6be83ce"
TWO = ("tests/test_cli_isolation.py::test_the_case_after_one_that_made_many_mappings_passes_and_says_what_its_wait_saw",
       "tests/test_cli_isolation.py::test_the_reading_from_outside_shows_what_a_program_holds_and_nothing_the_kernel_can_give_back[1]")
# the command of the job attacks in ci.yml at the head
WHOLE = ["tests/test_cli_isolation.py", "tests/test_web_isolation.py", "tests/test_vet.py", "tests/test_cli_runtimes.py"]


def failed_in(said):
    return [line.split(" ", 1)[1].split(" - ")[0] for line in said.splitlines() if line.startswith(("FAILED ", "ERROR "))]


def main(argv):
    if argv[0] == "keys":
        keys = [f"two-{n}" for n in range(1, 21)] + [f"whole-{n}" for n in range(1, 46)]
        print("keys=" + json.dumps(keys))
        return 0
    if argv[0] == "ref":
        print("ref=" + HEAD)
        return 0
    key, repo = argv[1], Path(argv[2]).resolve()
    times = []
    if key.startswith("two-"):
        for n in range(10):
            seen = {}
            for node in TWO:
                done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node], cwd=repo / "bench",
                                      capture_output=True, text=True)
                name = "mappings" if "mappings" in node else "reading"
                seen[name] = "passed" if done.returncode == 0 else "failed"
                if done.returncode != 0:
                    print(f"--- time {n + 1}, {name}: failed\n" + (done.stdout + done.stderr)[-12000:])
            times.append(seen)
            print(f"time {n + 1}: " + ", ".join(f"{k} {v}" for k, v in seen.items()), flush=True)
        record = {"times": times, "failed": {name: sum(t[name] == "failed" for t in times) for name in ("mappings", "reading")}}
        bad = any(record["failed"].values())
    else:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", *WHOLE], cwd=repo / "bench",
                              capture_output=True, text=True)
        said = done.stdout + done.stderr
        summary = [l for l in said.splitlines() if re.search(r"\d+ (passed|failed)", l)][-1:]
        record = {"code": done.returncode, "failed": failed_in(said), "summary": summary}
        if done.returncode != 0:
            print(said[-30000:])
        bad = done.returncode != 0
    print("REPEAT " + json.dumps(record))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
