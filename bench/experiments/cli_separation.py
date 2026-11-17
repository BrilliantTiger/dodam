"""Numbers behind the design of command-line judging: who the program runs as, how a case's limits
are enforced, and how a case is ended.

Each candidate is tried against the same attacks, written down before any candidate was built:
the program tries to read the tests, to write the result, to become the build's user, to leave files
and shared memory for a later case, to leave a process behind, to fill the disk, to flood its
output, to fork without end, to use up memory, and to hang.

Run from bench/ on a machine with Docker and the language images:

    python experiments/cli_separation.py > separation.json

Everything runs in fresh containers of the images, without network, as vetting does. The part that
runs inside a container is this same file, started with `inside` as its first argument.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
LIMITS = {"timeout_sec": 5, "memory_mb": 256, "output_kb": 256, "processes": 16, "disk_mb": 32}
SAFETY_PROCESSES = 600

ATTACK = r'''
import ctypes, json, os, subprocess, sys, time
what = sys.argv[1]
if what == "plain":
    print("plain")
elif what == "snoop":
    found = {}
    def attempt(name, action):
        try:
            found[name] = bool(action())
        except BaseException:
            found[name] = False
    def mark(place):
        with open(os.path.join(place, "marker.txt"), "w") as f:
            f.write("x")
        return True
    attempt("read the tests", lambda: "expected-answer" in open("/tests/answers.txt").read())
    attempt("write the result", lambda: open("/logs/verifier/reward.json", "w").write('{"reward": 1.0}'))
    for place in ("/app", "/tmp", "/var/tmp", "/dev/shm", "."):
        attempt("mark " + place, lambda: mark(place))
    attempt("make shared memory", lambda: ctypes.CDLL(None).shmget(0xD0DA, 4096, 0o1000 | 0o666) >= 0)
    attempt("become the build's user", lambda: subprocess.run(["/app/as-agent", "-p", "-c", "id -u"], capture_output=True, text=True).stdout.strip() == "1000" and os.getuid() != 1000)
    print(json.dumps(found))
elif what == "fork":
    while True:
        try:
            os.fork()
        except OSError:
            pass
elif what == "threads":
    import threading
    while True:
        try:
            threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
        except RuntimeError:
            time.sleep(0.01)
elif what == "memory":
    blocks = []
    while True:
        blocks.append(bytearray(16 * 1024 * 1024))
        for i in range(0, len(blocks[-1]), 4096):
            blocks[-1][i] = 1
elif what == "memory-spread":
    for _ in range(8):
        if os.fork() == 0:
            block = bytearray(64 * 1024 * 1024)
            for i in range(0, len(block), 4096):
                block[i] = 1
            time.sleep(60)
            os._exit(0)
    time.sleep(60)
elif what == "big-file":
    with open("big.bin", "wb") as f:
        while True:
            f.write(b"x" * (1024 * 1024))
elif what == "many-files":
    i = 0
    while True:
        i += 1
        with open("part-%d.bin" % i, "wb") as f:
            f.write(b"x" * (1024 * 1024))
elif what == "flood":
    while True:
        sys.stdout.write("x" * 65536)
elif what == "hang":
    import signal
    for number in range(1, 32):
        try:
            signal.signal(number, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    while True:
        time.sleep(1)
elif what == "daemon":
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            for fd in (0, 1, 2):
                os.close(fd)
            time.sleep(600)
        os._exit(0)
    os.wait()
'''
LIMIT_ATTACKS = {"fork": "fork without end", "threads": "threads without end", "memory": "use up memory in one process",
                 "memory-spread": "use up memory spread over eight processes", "big-file": "one huge file",
                 "many-files": "many files", "flood": "flood of output", "hang": "hang, ignoring every signal"}


# ---------------------------------------------------------------- inside a container

def inside(mode: str) -> None:
    sys.path.insert(0, "/probe/lib")
    import resource
    import dodam_cli
    import dodam_judge as judge

    def setup() -> None:
        os.makedirs("/tests", exist_ok=True)
        os.makedirs("/logs/verifier", exist_ok=True)
        Path("/tests/answers.txt").write_text("expected-answer\n")
        Path("/logs/verifier/reward.json").write_text('{"reward": 0.0}')
        Path("/app/main.py").write_text(ATTACK)
        shutil.copy("/bin/sh", "/app/as-agent")
        subprocess.run(["chown", "-R", "agent:agent", "/app"], check=True)
        os.chmod("/app/as-agent", 0o6755)

    def cli_run(limits: dict):
        env = {"INTERFACE": "cli", "RUN_CMD": "python3 {app}/main.py",
               **{key: str(limits[name]) for name, key in judge.CLI_LIMITS.items()}}
        run = judge.CliRun(Path("/app"), Path("/tests"), Path("/logs/verifier"), env, "agent", {})
        os.environ["DODAM_CLI_CONFIG"] = str(run.settings_file)
        dodam_cli._config = None
        return run

    def user_tasks(uid: int) -> int:
        return dodam_cli._running(uid)[1]

    if mode.startswith("who-"):
        setup()
        if mode == "who-build-user":      # as the web judge runs a server today
            judge.close_to_others(Path("/tests"), Path("/logs/verifier"))
            out = subprocess.run(["su", "-s", "/bin/sh", "agent", "-c", "cd /tmp && python3 /app/main.py snoop"],
                                 capture_output=True, text=True).stdout
        elif mode == "who-own-user":      # a user of its own, and nothing else
            judge.close_to_others(Path("/tests"), Path("/logs/verifier"))
            out = subprocess.run(["python3", "/app/main.py", "snoop"], capture_output=True, text=True, cwd="/tmp",
                                 user=54321, group=54321, extra_groups=[]).stdout
        else:                             # a user of its own, everything closed, no new privileges
            cli_run(LIMITS)
            with dodam_cli.case() as c:
                out = c.run(["snoop"]).out
        tried = json.loads(out)
        # what a later case would find is looked at from outside, after the case has ended
        shared = [line for line in Path("/proc/sysvipc/shm").read_text().splitlines()[1:] if line.strip()]
        succeeded = {
            "read the tests": tried["read the tests"],
            "write the result": tried["write the result"] or json.loads(Path("/logs/verifier/reward.json").read_text()) != {"reward": 0.0},
            "become the build's user": tried["become the build's user"],
            "leave a changed program for a later case": Path("/app/marker.txt").exists(),
            "leave a file in a shared temporary folder for a later case":
                any(Path(place, "marker.txt").exists() for place in ("/tmp", "/var/tmp", "/dev/shm")),
            "leave shared memory for a later case": bool(shared),
        }
        print(json.dumps({"mode": mode, "succeeded": succeeded, "stopped": sum(not v for v in succeeded.values()),
                          "of": len(succeeded)}))
        return

    if mode.startswith("limits-"):
        setup()
        run = cli_run(LIMITS)
        uid = run.uid
        mb = 1024 * 1024

        def kernel_only(args: list[str]) -> None:
            def before() -> None:
                os.setsid()
                resource.setrlimit(resource.RLIMIT_AS, (LIMITS["memory_mb"] * mb,) * 2)
                resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS["disk_mb"] * mb,) * 2)
                resource.setrlimit(resource.RLIMIT_CPU, (LIMITS["timeout_sec"] + 5,) * 2)
                resource.setrlimit(resource.RLIMIT_NPROC, (LIMITS["processes"] + 1,) * 2)
                dodam_cli._become(uid, uid)
            proc = subprocess.Popen(["python3", "/app/main.py", *args], cwd=work, preexec_fn=before,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                proc.communicate(timeout=LIMITS["timeout_sec"])
            except subprocess.TimeoutExpired:
                pass
            dodam_cli._kill_user(uid, uid)
            proc.kill()
            proc.communicate()

        def watching_only(args: list[str]) -> None:
            def before() -> None:
                os.setsid()
                dodam_cli._become(uid, uid)
            proc = subprocess.Popen(["python3", "/app/main.py", *args], cwd=work, preexec_fn=before,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.set_blocking(proc.stdout.fileno(), False)
            seen, deadline = 0, time.monotonic() + LIMITS["timeout_sec"]
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
                try:
                    seen += len(os.read(proc.stdout.fileno(), 1 << 20))
                except BlockingIOError:
                    pass
                pids, tasks = dodam_cli._running(uid)
                if (tasks > LIMITS["processes"] or dodam_cli._resident_bytes(pids) > LIMITS["memory_mb"] * mb
                        or seen > LIMITS["output_kb"] * 1024 or dodam_cli._tree_bytes(Path(work))[0] > LIMITS["disk_mb"] * mb):
                    break
            dodam_cli._kill_user(uid, uid)
            proc.kill()
            proc.wait()

        def chosen(args: list[str]) -> None:
            c.run(args)

        runner = {"limits-kernel-only": kernel_only, "limits-watching-only": watching_only, "limits-chosen": chosen}[mode]
        results = {}
        only = os.environ.get("ONLY_ATTACK")
        for attack, name in LIMIT_ATTACKS.items():
            if only and attack != only:
                continue
            c = dodam_cli.Case()
            work = str(c.work)
            peak = {"tasks": 0, "resident_mb": 0, "disk_mb": 0}
            stop = False

            def sample() -> None:
                while not stop:
                    pids, tasks = dodam_cli._running(uid)
                    peak["tasks"] = max(peak["tasks"], tasks)
                    peak["resident_mb"] = max(peak["resident_mb"], dodam_cli._resident_bytes(pids) // mb)
                    peak["disk_mb"] = max(peak["disk_mb"], dodam_cli._tree_bytes(c.root)[0] // mb)
                    time.sleep(0.002)
            import threading
            thread = threading.Thread(target=sample, daemon=True)
            thread.start()
            started = time.monotonic()
            dodam_cli._spent.clear()
            try:
                runner([attack])
                fault = None
            except Exception as exc:  # the judge itself failed
                fault = f"{type(exc).__name__}: {exc}"
            seconds = round(time.monotonic() - started, 2)
            stop = True
            thread.join()
            peak["disk_mb"] = max(peak["disk_mb"], dodam_cli._tree_bytes(c.root)[0] // mb)  # what it managed to write
            dodam_cli._kill_user(uid, uid)
            ended_early = seconds < LIMITS["timeout_sec"] - 0.5
            within = (peak["tasks"] <= LIMITS["processes"] + 1 and peak["resident_mb"] <= LIMITS["memory_mb"] * 1.25
                      and peak["disk_mb"] <= LIMITS["disk_mb"] * 1.25)
            results[name] = {"seconds": seconds, "largest_seen": dict(peak), "judge_fault": fault,
                             "stopped": fault is None and within and seconds < LIMITS["timeout_sec"] + 3,
                             "ended_before_the_time_limit": ended_early}
            shutil.rmtree(c.root, ignore_errors=True)
        print(json.dumps({"mode": mode, "attacks": results}))
        return

    if mode.startswith("looks-"):
        # how often the judge measures memory and files: what gets past it between two looks, and what a case costs
        setup()
        every = float(mode.split("-")[1]) / 1000
        dodam_cli.WATCH = every
        dodam_cli.TICK = min(dodam_cli.TICK, every)
        run = cli_run(LIMITS)
        uid, mb = run.uid, 1024 * 1024
        import threading
        results = {}
        for attack in ("memory", "memory-spread", "many-files"):
            worst = []
            for _ in range(5):
                c = dodam_cli.Case()
                peak = {"resident_mb": 0, "disk_mb": 0}
                stop = False

                def sample() -> None:
                    while not stop:
                        peak["resident_mb"] = max(peak["resident_mb"], dodam_cli._resident_bytes(dodam_cli._alive(uid)) // mb)
                        time.sleep(0.001)
                thread = threading.Thread(target=sample, daemon=True)
                thread.start()
                dodam_cli._spent.clear()
                c.run([attack])
                stop = True
                thread.join()
                peak["disk_mb"] = dodam_cli._tree_bytes(c.root)[0] // mb
                worst.append(peak["disk_mb"] if attack == "many-files" else peak["resident_mb"])
                shutil.rmtree(c.root, ignore_errors=True)
            results[LIMIT_ATTACKS[attack]] = {"limit_mb": LIMITS["disk_mb"] if attack == "many-files" else LIMITS["memory_mb"],
                                              "largest_seen_mb_in_5_tries": max(worst)}
        Path("/app/main.py").write_text("print('plain')\n")
        times = []
        cpu_before = time.process_time()
        wall_before = time.monotonic()
        for _ in range(200):
            started = time.perf_counter()
            dodam_cli._spent.clear()
            with dodam_cli.case(files={"in.txt": "x"}) as c:
                c.run([])
            times.append(time.perf_counter() - started)
        Path("/app/main.py").write_text("import time\ntime.sleep(2)\n")
        cpu_before = time.process_time()
        dodam_cli._spent.clear()
        with dodam_cli.case() as c:
            c.run([])
        print(json.dumps({"mode": mode, "looks_every_ms": every * 1000, "past_the_limit": results,
                          "median_ms_of_a_plain_case": round(statistics.median(times) * 1000, 2),
                          "judge_processor_share_while_a_program_runs_percent":
                              round((time.process_time() - cpu_before) / 2 * 100, 1)}))
        return

    if mode.startswith("end-"):
        setup()
        run = cli_run({**LIMITS, "processes": 64})
        uid = run.uid
        results = {}
        for attack in ("daemon", "fork"):
            c = dodam_cli.Case()

            def before() -> None:
                os.setsid()
                resource.setrlimit(resource.RLIMIT_NPROC, (65, 65))
                dodam_cli._become(uid, uid)
            proc = subprocess.Popen(["python3", "/app/main.py", attack], cwd=c.work, preexec_fn=before,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            started = time.monotonic()
            if mode == "end-group":
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
            elif mode == "end-walk":       # the web judge's way: three rounds over the process table
                for _ in range(3):
                    pids = judge.user_pids(uid)
                    if not pids:
                        break
                    for pid in pids:
                        try:
                            os.kill(pid, 9)
                        except ProcessLookupError:
                            pass
                    time.sleep(0.2)
            else:
                dodam_cli._kill_user(uid, uid)
            seconds = round(time.monotonic() - started, 3)
            time.sleep(0.3)
            results[attack] = {"seconds": seconds, "survivors": len(dodam_cli._alive(uid))}
            dodam_cli._kill_user(uid, uid)
            proc.wait()
            shutil.rmtree(c.root, ignore_errors=True)
        print(json.dumps({"mode": mode, "after": results}))
        return

    if mode == "time":
        setup()
        Path("/app/main.py").write_text("print('plain')\n")
        runs = 200

        def timed(action) -> float:
            times = []
            for _ in range(runs):
                started = time.perf_counter()
                action()
                times.append(time.perf_counter() - started)
            return round(statistics.median(times) * 1000, 2)

        plain = timed(lambda: subprocess.run(["python3", "/app/main.py"], capture_output=True))
        su = timed(lambda: subprocess.run(["su", "-s", "/bin/sh", "agent", "-c", "python3 /app/main.py"], capture_output=True))
        cli_run(LIMITS)

        def one_case() -> None:
            dodam_cli._spent.clear()
            with dodam_cli.case(files={"in.txt": "x"}) as c:
                c.run([])
        chosen = timed(one_case)
        print(json.dumps({"mode": mode, "runs": runs, "median_ms": {"the program alone, as root": plain,
                          "through su as the build's user": su, "a whole case as chosen": chosen}}))
        return

    if mode == "address-space":
        # does each language's own runtime start under a limit on address space, the kernel's only per-process memory limit?
        env = dict(line.split("=", 1) for line in Path("/probe/lang.env").read_text().splitlines())
        shutil.copytree("/probe/solution", "/app", dirs_exist_ok=True)
        if env["DEPS_DIR"] and env["DEPS_LINKS"]:
            judge.restore_deps(Path("/app"), Path(env["DEPS_DIR"]), env["DEPS_LINKS"].split(","))
        subprocess.run(["chown", "-R", "agent:agent", "/app"], check=True)
        if env["BUILD_CMD"]:
            subprocess.run(["su", "-s", "/bin/sh", "agent", "-c", "cd /app && " + env["BUILD_CMD"]], check=True, capture_output=True)
        command = [part.replace("{app}", "/app") for part in env["RUN_CMD"].split()]
        work = tempfile.mkdtemp()
        Path(work, "in.csv").write_text("a,b\n1,2\n")
        os.chmod(work, 0o777)
        found = {}
        for megabytes in (512, 1024, 4096, None):
            def before() -> None:
                if megabytes:
                    resource.setrlimit(resource.RLIMIT_AS, (megabytes * 1024 * 1024,) * 2)
            done = subprocess.run(command + ["in.csv", "-"], cwd=work, preexec_fn=before, capture_output=True, text=True,
                                  user=54321, group=54321, extra_groups=[])
            try:
                found[str(megabytes or "no limit")] = done.returncode == 0 and json.loads(done.stdout) == [{"a": "1", "b": "2"}]
            except ValueError:
                found[str(megabytes or "no limit")] = False
        peak_threads = 0
        proc = subprocess.Popen(command + ["-", "-"], cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                user=54321, group=54321, extra_groups=[])
        for _ in range(100):
            time.sleep(0.01)
            peak_threads = max(peak_threads, dodam_cli._running(54321)[1])
        proc.communicate(b"a,b\n1,2\n")
        print(json.dumps({"mode": mode, "runs_under_address_space_limit_mb": found, "threads_while_waiting_for_input": peak_threads}))
        return
    raise SystemExit(f"unknown mode {mode}")


# ---------------------------------------------------------------- on the host

def in_container(image: str, mode: str, extra: dict[str, Path] | None = None, memory_mb: int = 2048,
                 attack: str = "") -> dict:
    name = f"dodam-probe-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe"
        (probe / "lib").mkdir(parents=True)
        shutil.copy(Path(__file__), probe / "probe.py")
        shutil.copy(BENCH / "dodambench/judge.py", probe / "lib/dodam_judge.py")
        shutil.copy(BENCH / "dodambench/dodam_cli.py", probe / "lib/dodam_cli.py")
        for target, source in (extra or {}).items():
            if source.is_dir():
                shutil.copytree(source, probe / target)
            else:
                shutil.copy(source, probe / target)
        try:
            # The cap on processes is this experiment's own safety net: a candidate that cannot stop a
            # fork bomb must not take the whole machine down with it. The verifier gives the judge no such cap.
            subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0",
                            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m", "--cpus", "2",
                            "--pids-limit", str(SAFETY_PROCESSES), "-e", f"ONLY_ATTACK={attack}", image,
                            "/opt/judge/bin/python", "/probe/probe.py", "inside", mode], check=True, capture_output=True)
            subprocess.run(["docker", "cp", f"{probe}/.", f"{name}:/probe"], check=True, capture_output=True)
            done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {"mode": mode, "error": "the container did not finish in 300 seconds"}
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        why = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
        return {"mode": mode, "error": f"the judge's own process ended with status {done.returncode} "
                                       f"(137 means it was killed, as when the container runs out of memory) {why[0][:200]}"}
    return json.loads(lines[-1])


def main() -> None:
    sys.path.insert(0, str(BENCH))
    from dodambench.convert import to_harbor
    from dodambench.languages import LANGUAGES
    from dodambench.task import load_task

    python = "dodam-bench/python:dev"
    report: dict = {"limits_of_a_case": LIMITS}
    report["who_the_program_runs_as"] = [in_container(python, mode) for mode in
                                         ("who-build-user", "who-own-user", "who-closed")]
    report["how_limits_are_enforced"] = []
    for mode in ("limits-kernel-only", "limits-watching-only", "limits-chosen"):
        attacks = {}
        for attack, name in LIMIT_ATTACKS.items():  # a container for each attack, so one failure spoils no other
            found = in_container(python, mode, attack=attack)
            attacks[name] = found.get("attacks", {}).get(name, {"stopped": False, "error": found.get("error")})
        report["how_limits_are_enforced"].append({"mode": mode, "attacks": attacks})
    report["how_a_case_is_ended"] = [in_container(python, mode) for mode in ("end-group", "end-walk", "end-chosen")]
    report["time_per_case"] = in_container(python, "time")
    report["how_often_the_judge_looks"] = [in_container(python, f"looks-{ms}") for ms in (10, 5, 2, 1)]
    task = load_task(BENCH / "tasks/cli-csv-json")
    report["each_language"] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGUAGES:
            harbor = to_harbor(task, lang, Path(tmp))
            extra = {"solution": harbor / "steps/build/solution/variants/secure", "lang.env": harbor / "tests/lang.env"}
            report["each_language"][lang] = in_container(LANGUAGES[lang].image, "address-space", extra, memory_mb=4096)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    if sys.argv[1:2] == ["inside"]:
        inside(sys.argv[2])
    else:
        main()
