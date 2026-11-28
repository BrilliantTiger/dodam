"""PR 44's thirteen cases, each on a clean copy of bench: what is changed, and what the test must do then."""
import os, shutil, subprocess, sys, tempfile
TEST = "tests/test_cli_isolation.py::test_a_program_the_kernel_ends_fails_its_case_and_the_others_are_judged[switched off-5]"
ISO, JUDGE, CLI = "tests/test_cli_isolation.py", "dodambench/judge.py", "dodambench/dodam_cli.py"
STOP = "    libc.kill(judge, signal.SIGSTOP)\n"
MARK = "    libc.rename(mark_from, mark_to)\n"
CONT = "    libc.kill(judge, signal.SIGCONT)\n"
WRITE = '    write_record(order="written, then continued")\n'
ASK = '        os.rename("/logs/verifier/keep-ask.part", "/logs/verifier/keep-ask")\n'
BEFORE = '        stopped_now("before")\n'
# where the cases run: at d8a6b24 the two new cases pass (what the finding says); at the mended head they must fail
WHERE = os.environ.get("CASES_AT", "mended")
MENDED_WORDS = {"12": "MENDED_WORDS_12", "13": "MENDED_WORDS_13"}
AT = {k: (("passes", "") if WHERE == "d8a6b24" else ("fails", MENDED_WORDS[k])) for k in ("12", "13")}
CASES = [
    ("1 the kernel's end not counted as the case's memory", [(CLI, "    return killed_now > killed_before\n", "    return False\n")],
     "fails", ('attack["limit"] == "memory"', 'ends[0]["kernel_kills_before"], lost(result, where / "out")')),
    ("2 the kernel's count of ends read as 0", [(JUDGE, '    return memory_events(group).get("oom_kill", 0)\n', "    return 0\n")],
     "fails", ('attack["limit"] == "memory"', 'ends[0]["kernel_kills_before"], lost(result, where / "out")')),
    ("3 a judge that stands stopped by itself before the moment",
     [(ISO, ASK, "        os.kill(os.getppid(), 19)  # SIGSTOP\n" + ASK)], "fails", 'kept.get("state_before") not in (None, "T", "t", "gone")'),
    ("4 a judge that goes on with the same case runner", [(JUDGE, '    if stop and _runner["session"] is not None:\n', "    if False:\n")],
     "fails", 'after["runner"] != begun.get("runner")'),
    ("5 a watcher that also stops the case runner",
     [(ISO, MARK, MARK + "    libc.kill(runner, signal.SIGSTOP)\n"), (ISO, CONT, CONT + "    libc.kill(runner, signal.SIGCONT)\n")],
     # caught by the watcher's looks, or by the test's own bound when the case runner is never let go
     "fails", ('kept.get("judge_ran") == 0 and kept.get("runner_stopped") == 0', "the judging did not end within")),
    # 6 fails because the hidden test's reading is never made: the mark comes after the hidden test's 10 s
    ("6 a helper that stops nothing and writes the record", [(ISO, STOP, "    pass\n")], "fails",
     'begun.get("judge") is not None and begun.get("stopped") == [begun["judge"]]'),
    ("7 a watcher that lets the judge go at once", [(ISO, MARK, MARK + "    libc.kill(judge, signal.SIGCONT)\n")], "fails",
     'begun.get("judge") is not None and begun.get("stopped") == [begun["judge"]]'),
    ("8 the watcher held up 2 s before it writes its record (no fault)", [(ISO, WRITE, "    time.sleep(2)\n" + WRITE)], "passes", ""),
    ("9 the watcher held up 2 s after it continued the judge (no fault)", [(ISO, "    os.write(told_w, told)\n", "    time.sleep(2)\n    os.write(told_w, told)\n")],
     "passes", ""),
    ("10 the watcher ended while the judge stands stopped", [(ISO, MARK, MARK + "    os.kill(os.getpid(), signal.SIGKILL)\n")],
     "fails", 'assert (out / "held.json").is_file(), "the watcher left none"'),
    ("11 the watcher and its second line both ended", [(ISO, MARK, MARK + "    second.kill()\n    second.wait()\n    os.kill(os.getpid(), signal.SIGKILL)\n")],
     "fails", "the judging did not end within"),
    # written before the mend, from the second look's first blocking finding and the coordinating session's ruling:
    # each passes at d8a6b24 and must fail at the mended head in the words the mend gives it (MENDED_WORDS)
    ("12 a judge that comes to stand stopped by itself between the watcher's reading and its stop",
     # the watcher is held up 2 s right before its stop, after every reading it makes; the hidden test stops the
     # judge 1 s after the second line's ready word, inside those 2 s with about 1 s to spare on either side
     [(ISO, STOP, "    time.sleep(2)\n" + STOP),
      (ISO, ASK, ASK + "        for _ in range(2000):\n"
                       "            if os.path.exists(\"/logs/verifier/keep-second-ready\"):\n"
                       "                break\n"
                       "            time.sleep(0.005)\n"
                       "        time.sleep(1)\n"
                       "        os.kill(os.getppid(), 19)  # SIGSTOP\n")],
     *AT["12"]),
    ("13 a stop sent to the judge from outside while the watcher holds it",
     # once the mark of the hold is there, the judge stands stopped by the watcher; the hidden test stops it again
     [(ISO, BEFORE, "        os.kill(os.getppid(), 19)  # SIGSTOP\n" + BEFORE)],
     *AT["13"]),
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
        named = (words,) if isinstance(words, str) else words
        matched = next((line.strip() for line in said.splitlines() if words and any(w in line for w in named)), None)
        right = came == expect and (not words or matched is not None)
        print(f"{name}: came {came}; the line that matched: {matched}", flush=True)
        wrong += not right
        rows.append((name, expect + (f" ({' or '.join((words,) if isinstance(words, str) else words)})" if words else ""), came, "yes" if right else "NO"))
        if not right:
            print(f"--- {name}\n{said[-3000:]}", flush=True)
print("| case | expected | came | as expected |")
print("|---|---|---|---|")
for row in rows:
    print("| " + " | ".join(row) + " |")
sys.exit(1 if wrong else 0)
