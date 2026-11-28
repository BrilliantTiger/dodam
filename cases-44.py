"""PR 44's eleven cases, each on a clean copy of bench: what is changed, and what the test must do then."""
import os, shutil, subprocess, sys, tempfile
TEST = "tests/test_cli_isolation.py::test_a_program_the_kernel_ends_fails_its_case_and_the_others_are_judged[switched off-5]"
ISO, JUDGE, CLI = "tests/test_cli_isolation.py", "dodambench/judge.py", "dodambench/dodam_cli.py"
STOP = "    libc.kill(judge, signal.SIGSTOP)\n"
MARK = "    libc.rename(mark_from, mark_to)\n"
CONT = "    libc.kill(judge, signal.SIGCONT)\n"
WRITE = '    write_record(order="written, then continued")\n'
ASK = '        os.rename("/logs/verifier/keep-ask.part", "/logs/verifier/keep-ask")\n'
CASES = [
    ("1 the kernel's end not counted as the case's memory", [(CLI, "    return killed_now > killed_before\n", "    return False\n")],
     "fails", "assert"),
    ("2 the kernel's count of ends read as 0", [(JUDGE, '    return memory_events(group).get("oom_kill", 0)\n', "    return 0\n")],
     "fails", "assert"),
    ("3 a judge that stands stopped by itself before the moment",
     [(ISO, ASK, "        os.kill(os.getppid(), 19)  # SIGSTOP\n" + ASK)], "fails", "state_before"),
    ("4 a judge that goes on with the same case runner", [(JUDGE, '    if stop and _runner["session"] is not None:\n', "    if False:\n")],
     "fails", "after"),
    ("5 a watcher that also stops the case runner",
     [(ISO, MARK, MARK + "    libc.kill(runner, signal.SIGSTOP)\n"), (ISO, CONT, CONT + "    libc.kill(runner, signal.SIGCONT)\n")],
     "fails", "runner_stopped"),
    ("6 a helper that stops nothing and writes the record", [(ISO, STOP, "    pass\n")], "fails", "stopped"),
    ("7 a watcher that lets the judge go at once", [(ISO, MARK, MARK + "    libc.kill(judge, signal.SIGCONT)\n")], "fails", "stopped"),
    ("8 the watcher held up 2 s before it writes its record (no fault)", [(ISO, WRITE, "    time.sleep(2)\n" + WRITE)], "passes", ""),
    ("9 the watcher held up 2 s after it continued the judge (no fault)", [(ISO, "    os.write(told_w, told)\n", "    time.sleep(2)\n    os.write(told_w, told)\n")],
     "passes", ""),
    ("10 the watcher ended while the judge stands stopped", [(ISO, MARK, MARK + "    os.kill(os.getpid(), signal.SIGKILL)\n")],
     "fails", "the watcher left none"),
    ("11 the watcher and its second line both ended", [(ISO, MARK, MARK + "    second.kill()\n    second.wait()\n    os.kill(os.getpid(), signal.SIGKILL)\n")],
     "fails", "did not end within"),
]
bench = os.path.abspath(sys.argv[1])
rows, wrong = [], 0
for name, changes, expect, words in CASES:
    with tempfile.TemporaryDirectory(prefix="dodam-case-") as where:
        copy = os.path.join(where, "bench")
        shutil.copytree(bench, copy, ignore=shutil.ignore_patterns("__pycache__", ".venv"))
        for path, find, put in changes:
            full = os.path.join(copy, path)
            text = open(full).read()
            assert text.count(find) == 1, (name, path, find)
            open(full, "w").write(text.replace(find, put))
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", TEST],
                              cwd=copy, capture_output=True, text=True)
        said = done.stdout + done.stderr
        came = "passes" if done.returncode == 0 else ("fails" if done.returncode == 1 else f"error {done.returncode}")
        right = came == expect and (not words or words in said)
        wrong += not right
        rows.append((name, expect + (f" ({words})" if words else ""), came, "yes" if right else "NO"))
        if not right:
            print(f"--- {name}\n{said[-3000:]}", flush=True)
print("| case | expected | came | as expected |")
print("|---|---|---|---|")
for row in rows:
    print("| " + " | ".join(row) + " |")
sys.exit(1 if wrong else 0)
