"""Measure the candidates the judging of command-line programs was chosen from.

The attacks are those of attacks.toml, written before the candidates were compared. Every candidate
meets the same attacks in fresh containers of the Python image; every container is capped in memory
and processes, so a candidate that stops nothing cannot take the machine down. What is recorded is
what the attack got (memory, files, processes), whether its case would fail, whether the judge
survived, and how long it took.

    python experiments/cli_separation.py > separation.json
    python experiments/cli_separation.py --quick     # every table once with the smallest counts, Python only

The script ends with an error when a table does not come out; the automatic check runs the quick mode.

The parts: when the tests are closed; what a command line says; who the program runs as; what bounds
files, memory, what outlives a process, and processes; how a case is ended; how what a case left is
removed; how often the judge measures; what a plain case costs; and what each language's runtime
needs (address space, idle threads).
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
LIMITS = {"timeout_sec": 5, "memory_mb": 256, "output_kb": 256, "processes": 16, "disk_mb": 32}
CAPS = {"memory_mb": 1024, "processes": 600}
MB = 1024 * 1024
CASES = "/dodam-cases"

ATTACK = r'''
import ctypes, errno, json, os, signal, sys, time
MB = 1024 * 1024
libc = ctypes.CDLL(None, use_errno=True)
what = sys.argv[1]
got = {}


def touch(block):
    for i in range(0, len(block), 4096):
        block[i] = 1


def write_until_refused(names):
    written = 0
    try:
        for name in names:
            with open(name, "wb", buffering=0) as f:
                written += f.write(b"x" * MB)
    except OSError as error:
        got["refused"] = errno.errorcode[error.errno]
    return written // MB


if what == "plain":
    print("plain")
elif what == "memory":
    blocks = []
    while True:
        blocks.append(bytearray(16 * MB)); touch(blocks[-1])
elif what == "memory-spread":
    for _ in range(8):
        if os.fork() == 0:
            block = bytearray(64 * MB); touch(block); time.sleep(60); os._exit(0)
    time.sleep(60)
elif what == "memory-file":
    held = []
    while True:
        held.append(libc.memfd_create(b"hold", 0))
        for _ in range(4):
            os.write(held[-1], b"x" * (4 * MB))
elif what in ("shared-memory", "shared-memory-to-the-brim"):
    # filled by one short-lived process after another, 64 MB each, so that no process ever holds much
    size = (320 if what == "shared-memory" else 640) * MB
    ident = libc.shmget(0, size, 0o1666)
    got["made"] = ident >= 0
    libc.shmat.restype = ctypes.c_void_p
    for part in range(size // (64 * MB) if ident >= 0 else 0):
        if os.fork() == 0:
            at = libc.shmat(ident, None, 0)
            ctypes.memset(at + part * 64 * MB, 1, 64 * MB)
            os._exit(0)
        os.wait()
    print(json.dumps(got))
elif what == "message-queues":
    class Message(ctypes.Structure):
        _fields_ = [("kind", ctypes.c_long), ("text", ctypes.c_char * 8000)]
    message = Message(1, b"x" * 7999)
    queues = sent = 0
    while queues < 4000:
        ident = libc.msgget(0, 0o1666)
        if ident < 0:
            break
        queues += 1
        while libc.msgsnd(ident, ctypes.byref(message), 8000, 0o4000) == 0:
            sent += 1
    print(json.dumps({"made": queues > 0, "mb": sent * 8000 // MB}))
elif what == "one-huge-file":
    with open("big.bin", "wb", buffering=0) as f:
        written = 0
        try:
            while written < 400 * MB:
                written += f.write(b"x" * MB)
        except OSError as error:
            got["refused"] = errno.errorcode[error.errno]
    print(json.dumps({"mb": written // MB, **got}))
elif what == "many-files":
    print(json.dumps({"mb": write_until_refused("part-%d.bin" % i for i in range(400)), **got}))
elif what == "deleted-while-open":
    held, written = [], 0
    try:
        for i in range(8):
            f = open("gone-%d.bin" % i, "wb", buffering=0); os.unlink(f.name); held.append(f)
            for _ in range(30):
                written += f.write(b"x" * MB)
    except OSError as error:
        got["refused"] = errno.errorcode[error.errno]
    print(json.dumps({"mb": written // MB, **got}))
elif what == "deep-folders":
    for _ in range(1100):
        os.mkdir("d"); os.chdir("d")
    print(json.dumps({"mb": write_until_refused("below-%d.bin" % i for i in range(240)), **got}))
elif what == "empty-files":
    i = 0
    try:
        while i < 200000:
            open("e-%d" % i, "w").close(); i += 1
    except OSError as error:
        got["refused"] = errno.errorcode[error.errno]
    print(json.dumps({"entries": i, **got}))
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
elif what == "daemon":
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            for fd in (0, 1, 2):
                os.close(fd)
            time.sleep(600)
        os._exit(0)
    os.wait()
elif what == "read-command-lines":
    said = b" ".join(open("/proc/%s/cmdline" % p, "rb").read() for p in os.listdir("/proc") if p.isdigit())
    print(json.dumps({"names the test": b"test_secret_name" in said}))
elif what == "snoop":
    def attempt(action):
        try:
            return bool(action())
        except BaseException:
            return False
    print(json.dumps({
        "read the tests": attempt(lambda: open("/tests/hidden.py").read()),
        "write the result": attempt(lambda: open("/logs/verifier/reward.json", "w").write("1")),
        "become the build's user": attempt(lambda: __import__("subprocess").run(
            ["/app/as-agent", "-p", "-c", "id -u"], capture_output=True, text=True).stdout.strip() == "1000"),
        "change the program for a later case": attempt(lambda: open("/app/main.py", "a").write("#")),
        "leave a file in a shared temporary folder": attempt(lambda: open("/tmp/left", "w").write("x")),
        "leave shared memory": attempt(lambda: libc.shmget(0xD0DA, 4096, 0o1666) >= 0),
    }))
'''


# ------------------------------------------------------------------------------------------------
# inside a container
# ------------------------------------------------------------------------------------------------

def inside(part: str, candidate: str, attack: str, quick: bool = False) -> dict:
    sys.path.insert(0, "/probe/lib")
    import dodam_judge as judge

    app = Path("/app")
    app.mkdir(exist_ok=True)
    (app / "main.py").write_text(ATTACK)
    os.chmod(app / "main.py", 0o644)
    uid = judge.free_user_id()
    side = judge.Side(uid, uid)
    capped = os.path.ismount(CASES)
    work_root = Path(CASES if capped and candidate not in ("walk", "file-size-only") else tempfile.mkdtemp(dir="/var"))
    os.chmod(work_root, 0o711)

    def taken() -> int:
        room = os.statvfs(CASES) if capped else None
        in_cases = (room.f_blocks - room.f_bfree) * room.f_frsize if room else 0
        return judge.container_memory()["taken"] - in_cases

    def tree_bytes(top: str) -> int:
        total, stack = 0, [top]
        while stack:
            try:
                with os.scandir(stack.pop()) as entries:
                    for entry in entries:
                        info = entry.stat(follow_symlinks=False)
                        total += info.st_blocks * 512
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
            except OSError:
                continue
        return total

    def tasks(pids: list[int]) -> int:
        count = 0
        for pid in pids:
            try:
                count += int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[17])
            except (OSError, IndexError, ValueError):
                pass
        return count

    def resident(pids: list[int]) -> int:
        total = 0
        for pid in pids:
            try:
                total += int(open(f"/proc/{pid}/statm").read().split()[1])
            except (OSError, IndexError, ValueError):
                pass
        return total * os.sysconf("SC_PAGE_SIZE")

    def shared_left() -> int:
        """MB still held in System V shared memory and message queues."""
        held = 0
        for name, column in (("shm", "size"), ("msg", "cbytes")):
            lines = Path(f"/proc/sysvipc/{name}").read_text().splitlines()
            for line in lines[1:]:
                held += int(dict(zip(lines[0].split(), line.split())).get(column, 0))
        return held // MB

    def case(argv: list[str], *, kernel: dict, refuse: bool, watch, every: float = 0.002, as_uid: int = uid,
             timeout: float = LIMITS["timeout_sec"]) -> dict:
        """Run the attack once as a case, with the candidate's kernel limits and the candidate's watching."""
        root = Path(tempfile.mkdtemp(dir=work_root))
        os.chown(root, as_uid, as_uid)
        whom = judge.Side(as_uid, as_uid)
        whom.filter = judge.refusing_filter() if refuse else None
        before, disk_before = taken(), os.statvfs(root)
        peak = {"memory_mb": 0, "tasks": 0, "files_mb": 0}
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                peak["memory_mb"] = max(peak["memory_mb"], (taken() - before) // MB)
                peak["tasks"] = max(peak["tasks"], tasks(whom.alive()))
                now = os.statvfs(root)
                peak["files_mb"] = max(peak["files_mb"], (disk_before.f_bfree - now.f_bfree) * now.f_frsize // MB)
                time.sleep(0.001)
        kills_before = judge.killed_for_memory()
        started = time.monotonic()
        proc = subprocess.Popen(["python3", "/app/main.py", *argv], cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env={"HOME": str(root), "TMPDIR": str(root), "PATH": os.environ["PATH"]},
                                preexec_fn=whom.entering(kernel))
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        limit, fault = None, None
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                limit = "time"
                break
            limit = watch(whom, root, before) if watch else None
            if limit:
                break
            time.sleep(every)
        try:
            if not whom.end():
                fault = "processes of the program could not be ended"
        except BaseException as error:
            fault = f"{type(error).__name__}: {error}"
        seconds = round(time.monotonic() - started, 2)
        stop.set()
        sampler.join()
        out = proc.stdout.read(4000).decode("utf-8", "replace")
        proc.wait()
        if limit is None and judge.killed_for_memory() > kills_before:
            limit = "memory (ended by the kernel)"
        if limit is None and proc.returncode == -25:
            limit = "files (the kernel refused the size)"
        said = json.loads(out) if out.startswith("{") else {}
        try:
            judge.remove_tree(root)
        except OSError as error:
            fault = fault or f"cannot remove the case's folder: {error}"
        return {"case_fails_for": limit, "judge_fault": fault, "seconds": seconds, "program_says": said, **peak}

    disk = LIMITS["disk_mb"] * MB
    base = {"RLIMIT_CORE": 0, "RLIMIT_FSIZE": disk, "RLIMIT_NPROC": LIMITS["processes"]}

    if part == "files":
        watch = None
        if candidate == "walk":
            watch = lambda whom, root, before: "files" if tree_bytes(str(root)) > disk else None
        return case([attack], kernel=base, refuse=True, watch=watch)

    if part == "memory":
        if candidate == "processes-only":
            watch = lambda whom, root, before: "memory" if resident(whom.alive()) > LIMITS["memory_mb"] * MB else None
        else:
            watch = lambda whom, root, before: "memory" if taken() - before > LIMITS["memory_mb"] * MB else None
        return case([attack], kernel=base, refuse=False, watch=watch)

    if part == "outliving":
        watch = lambda whom, root, before: "memory" if taken() - before > LIMITS["memory_mb"] * MB else None
        result = case([attack], kernel=base, refuse=candidate == "refused", watch=watch)
        result["left_after_the_case_mb"] = shared_left()
        if candidate == "removed-afterwards":
            pid = os.fork()
            if pid == 0:
                import ctypes
                libc = ctypes.CDLL(None)
                os.setgroups([]); os.setgid(uid); os.setuid(uid)
                for name, column, call in (("shm", "shmid", libc.shmctl), ("msg", "msqid", libc.msgctl)):
                    lines = Path(f"/proc/sysvipc/{name}").read_text().splitlines()
                    for line in lines[1:]:
                        call(int(dict(zip(lines[0].split(), line.split()))[column]), 0, None)
                os._exit(0)
            os.waitpid(pid, 0)
        result["left_for_the_next_case_mb"] = shared_left()
        after = case(["plain"], kernel=base, refuse=candidate == "refused", watch=watch)
        result["the_next_case_runs"] = after["case_fails_for"] is None and after["judge_fault"] is None
        return result

    if part == "processes":
        kernel = dict(base)
        watch = lambda whom, root, before: "processes" if tasks(whom.alive()) > LIMITS["processes"] else None
        if candidate == "watching-only":
            del kernel["RLIMIT_NPROC"]
        elif candidate == "kernel-cap-one-above-and-watching":
            kernel["RLIMIT_NPROC"] = LIMITS["processes"] + 1
        else:
            watch = None
        return case([attack], kernel=kernel, refuse=True, watch=watch)

    if part == "interval":
        every = float(candidate) / 1000
        watch = lambda whom, root, before: "memory" if taken() - before > LIMITS["memory_mb"] * MB else None
        tries = 2 if quick else 20
        over = sorted(case([attack], kernel=base, refuse=True, watch=watch, every=every)["memory_mb"] for _ in range(tries))
        (app / "sleeper.py").write_text("import time\ntime.sleep(2)\n")
        shares = []
        for _ in range(1 if quick else 5):
            # the judge's processor use: the processor time this process used while a program slept
            # for two seconds and was watched, as a share of those two seconds
            used, clock = time.process_time(), time.monotonic()
            proc = subprocess.Popen(["python3", "/app/sleeper.py"], preexec_fn=side.entering(base))
            before = taken()
            while proc.poll() is None:
                watch(side, None, before)
                time.sleep(every)
            shares.append(round((time.process_time() - used) / (time.monotonic() - clock) * 100, 1))
        return {"memory_mb_reached_in_20_tries": {"smallest": over[0], "median": over[len(over) // 2], "largest": over[-1]},
                "judge_processor_percent_smallest_and_largest_of_5": [min(shares), max(shares)]}

    if part == "ending":
        proc = subprocess.Popen(["python3", "/app/main.py", attack], preexec_fn=side.entering(base),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        started = time.monotonic()
        if candidate == "signal-the-group":
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
        elif candidate == "walk-the-process-list":
            for _ in range(200):
                pids = side.alive()
                if not pids:
                    break
                for pid in pids:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass
                time.sleep(0.01)
        else:
            side.end()
        seconds = round(time.monotonic() - started, 3)
        time.sleep(0.3)
        left = len(side.alive())
        side.end()
        return {"seconds": seconds, "survivors": left}

    if part == "removing":
        top = Path(tempfile.mkdtemp(dir="/var"))
        os.chdir(top)
        if attack == "deep-folders":
            for _ in range(1100):
                os.mkdir("d"); os.chdir("d")
        else:
            for i in range(20000):
                open("e-%d" % i, "w").close()
        os.chdir("/")
        started = time.monotonic()
        try:
            shutil.rmtree(top) if candidate == "library" else judge.remove_tree(top)
            error = None
        except BaseException as exc:
            error = f"{type(exc).__name__}: {str(exc)[:80]}"
        return {"removed": not top.exists(), "error": error, "seconds": round(time.monotonic() - started, 3)}

    if part == "closing":
        tests, logs = Path("/tests"), Path("/logs/verifier")
        tests.mkdir(); logs.mkdir(parents=True)
        (tests / "hidden.py").write_text("SECRET")
        agent = judge.Side.named("agent")
        judge.give_app_to(app, "agent")
        if candidate == "before-anything-runs":
            judge.keep_private(tests)
        judge.run_build("cp -R /tests /app/stolen", app, agent, dict(os.environ), 20)
        judge.keep_private(tests)
        return {"hidden_files_the_build_took": len(list((app / "stolen").rglob("*.py"))) if (app / "stolen").exists() else 0}

    if part == "command-line":
        argv = ["python3", "-c", "import time; time.sleep(3)"]
        env = dict(os.environ)
        if candidate == "arguments":
            argv.append("/tests/security/test_secret_name.py")
        else:
            env["OPTIONS"] = "/tests/security/test_secret_name.py"
        judging = subprocess.Popen(argv, env=env)
        result = case(["read-command-lines"], kernel=base, refuse=True, watch=None)
        judging.kill()
        return result["program_says"]

    if part == "who":
        tests, logs = Path("/tests"), Path("/logs/verifier")
        tests.mkdir(); logs.mkdir(parents=True)
        (tests / "hidden.py").write_text("SECRET")
        judge.give_app_to(app, "agent")
        shutil.copy("/bin/sh", "/app/as-agent")
        os.chown("/app/as-agent", 1000, 1000)
        os.chmod("/app/as-agent", 0o6755)
        whom = uid
        if candidate == "the-builds-user":
            whom = 1000
        elif candidate == "a-user-of-its-own-and-nothing-else":
            pass
        else:
            judge.keep_private(tests); judge.keep_private(logs)
            judge.open_for_reading(app)
            judge.close_writable_places()
        result = case(["snoop"], kernel=base, refuse=candidate == "chosen", watch=None, as_uid=whom)
        got = result["program_says"]
        return {"succeeded": got, "stopped": sum(not v for v in got.values()), "of": len(got)}

    if part == "time":
        (app / "plain.py").write_text("print('plain')\n")
        settings = Path("/probe/settings.json")
        settings.write_text(json.dumps({"command": ["python3", "/app/plain.py"], "uid": uid, "gid": uid, "limits": LIMITS,
                                        "work_root": CASES, "fault_file": "/probe/faults", "env": {"PATH": os.environ["PATH"]}}))
        os.environ["DODAM_CLI_CONFIG"] = str(settings)
        import dodam_cli
        times = []
        for _ in range(5 if quick else 200):
            started = time.perf_counter()
            if candidate == "the-program-alone":
                subprocess.run(["python3", "/app/plain.py"], capture_output=True)
            else:
                dodam_cli._spent.clear()
                with dodam_cli.case(files={"in.txt": "x"}) as c:   # the judge's own case, whole
                    c.run([])
            times.append(time.perf_counter() - started)
        return {"median_ms": round(statistics.median(times) * 1000, 2)}

    if part == "language":
        import resource
        command = json.loads(os.environ["RUN_COMMAND"])
        shutil.copytree("/probe/app", "/app", dirs_exist_ok=True)
        links = [name for name in os.environ.get("DEPS_LINKS", "").split(",") if name]
        if links:
            judge.restore_deps(app, Path(os.environ["DEPS_DIR"]), links)
        built = subprocess.run(os.environ["BUILD_COMMAND"] or "true", shell=True, cwd=app, capture_output=True)
        if built.returncode != 0:
            return {"error": "the sample did not build: " + built.stderr.decode("utf-8", "replace")[-200:]}
        runs = {}
        for mb in (512, 1024, 4096, 0):
            def limit(mb=mb):
                if mb:
                    resource.setrlimit(resource.RLIMIT_AS, (mb * MB, mb * MB))
            try:
                done = subprocess.run(command, input=b"a\n1\n", capture_output=True, preexec_fn=limit, timeout=60)
                runs[str(mb) if mb else "no limit"] = done.returncode == 0
            except (OSError, subprocess.SubprocessError):
                runs[str(mb) if mb else "no limit"] = False
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        threads = tasks([proc.pid])
        proc.kill()
        return {"runs_under_address_space_limit_mb": runs, "threads_while_waiting_for_input": threads}
    raise SystemExit(f"unknown part {part}")


# ------------------------------------------------------------------------------------------------
# on the host
# ------------------------------------------------------------------------------------------------

QUICK = False   # go through every table once with the smallest counts: whether each still comes out


def in_container(part: str, candidate: str, attack: str = "", image: str = "dodam-bench/python:dev",
                 memory_mb: int = CAPS["memory_mb"], extra: dict | None = None, env: dict | None = None) -> dict:
    name = f"dodam-probe-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe"
        (probe / "lib").mkdir(parents=True)
        shutil.copy(Path(__file__), probe / "probe.py")
        shutil.copy(BENCH / "dodambench/judge.py", probe / "lib/dodam_judge.py")
        shutil.copy(BENCH / "dodambench/dodam_cli.py", probe / "lib/dodam_cli.py")
        for target, source in (extra or {}).items():
            shutil.copytree(source, probe / target)
        flags = [flag for key, value in (env or {}).items() for flag in ("-e", f"{key}={value}")]
        try:
            sys.path.insert(0, str(BENCH))
            from dodambench import frame  # on this side only: the probe inside the container has no frame
            subprocess.run(frame.container([
                "docker", "create", "--name", name, "--network", "none", "--user", "0",
                "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m", "--cpus", "2",
                "--pids-limit", str(CAPS["processes"]),
                "--tmpfs", f"{CASES}:rw,exec,nosuid,nodev,size={LIMITS['disk_mb']}m,nr_inodes=20000,mode=0711",
                *flags, image, "/opt/judge/bin/python", "/probe/probe.py", "inside", part, candidate, attack,
                *(["quick"] if QUICK else [])]),
                check=True, capture_output=True)
            subprocess.run(["docker", "cp", f"{probe}/.", f"{name}:/probe"], check=True, capture_output=True)
            done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {"error": "the container did not finish in 300 seconds"}
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        why = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
        return {"error": f"the judging process ended with status {done.returncode} (137: the kernel ended it "
                         f"when the container's memory ran out): {why[0][:160]}"}
    return json.loads(lines[-1])


def table(part: str, candidates: list[str], attacks: list[str], **room) -> dict:
    return {candidate: {attack: in_container(part, candidate, attack, **room) for attack in attacks}
            for candidate in candidates}


def main() -> None:
    from dodambench.languages import LANGUAGES
    from dodambench.task import load_task
    from dodambench.convert import verifier_env

    report = {
        "limits_of_a_case": LIMITS, "caps_of_every_container": CAPS,
        "when_the_tests_are_closed": table("closing", ["after-the-build", "before-anything-runs"], ["the build reads the tests"]),
        "what_a_command_line_says": table("command-line", ["arguments", "environment"], ["the program reads the process list"]),
        "who_the_program_runs_as": table("who", ["the-builds-user", "a-user-of-its-own-and-nothing-else", "chosen"], ["snoop"]),
        "what_bounds_files": table("files", ["file-size-only", "walk", "capped-folder"],
                                   ["one-huge-file", "many-files", "deleted-while-open", "deep-folders", "empty-files"]),
        "what_counts_as_memory": table("memory", ["processes-only", "whole-container"],
                                       ["memory", "memory-spread", "memory-file", "shared-memory"]),
        "what_outlives_a_process": {
            **table("outliving", ["kept", "removed-afterwards", "refused"], ["shared-memory", "message-queues"]),
            "in a container of 384 MB": table("outliving", ["removed-afterwards", "refused"],
                                              ["shared-memory-to-the-brim"], memory_mb=384)},
        "what_bounds_processes": table("processes", ["watching-only", "kernel-cap-one-above-and-watching", "kernel-cap"],
                                       ["fork", "threads"]),
        "how_a_case_is_ended": table("ending", ["signal-the-group", "walk-the-process-list", "as-the-programs-user"],
                                     ["daemon", "fork"]),
        "how_what_a_case_left_is_removed": table("removing", ["library", "bounded"], ["deep-folders", "empty-files"]),
        "how_often_the_judge_measures": table("interval", ["10", "5", "2", "1"], ["memory"]),
        "time_of_a_plain_case": table("time", ["the-program-alone", "a-whole-case"], ["plain"]),
    }
    sample = load_task(BENCH / "tasks/cli-csv-json")
    languages = {}
    for key in sample.languages:
        language = LANGUAGES[key]
        if QUICK and key != "python":
            continue   # the quick mode needs the Python image only
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app"
            shutil.copytree(sample.root / "solutions/secure" / key / "build", source)
            settings = verifier_env(sample, key)
            command = language.run_cmd.replace("{app}", "/app").split() + ["-", "-"]
            languages[key] = in_container("language", "-", "-", image=language.image, memory_mb=1400,
                                          extra={"app": source},
                                          env={"RUN_COMMAND": json.dumps(command), "BUILD_COMMAND": language.build_cmd,
                                               "DEPS_DIR": settings.get("DEPS_DIR", ""),
                                               "DEPS_LINKS": settings.get("DEPS_LINKS", "")})
    report["each_language"] = languages
    report["tables_that_did_not_come_out"] = missing(report)
    print(json.dumps(report, indent=1))
    if report["tables_that_did_not_come_out"]:
        raise SystemExit("a table did not come out: " + "; ".join(report["tables_that_did_not_come_out"]))


# What every cell of a table must say for the table to have come out. A candidate that lets the
# attack take the judging process down has that as its result; any other error is a table that no
# longer comes out of this script.
CELLS = {"when_the_tests_are_closed": "hidden_files_the_build_took", "what_a_command_line_says": "names the test",
         "who_the_program_runs_as": "succeeded", "what_bounds_files": "case_fails_for",
         "what_counts_as_memory": "case_fails_for", "what_outlives_a_process": "case_fails_for",
         "what_bounds_processes": "case_fails_for", "how_a_case_is_ended": "survivors",
         "how_what_a_case_left_is_removed": "removed", "how_often_the_judge_measures": "memory_mb_reached_in_20_tries",
         "time_of_a_plain_case": "median_ms", "each_language": "threads_while_waiting_for_input"}
JUDGE_TAKEN_DOWN = "the judging process ended with status"


def missing(report: dict) -> list[str]:
    """The cells of the report that do not hold what their table is about."""
    found = []

    def look(table: str, where: str, cell: dict) -> None:
        if CELLS[table] in cell or str(cell.get("error", "")).startswith(JUDGE_TAKEN_DOWN):
            return
        if cell and all(isinstance(value, dict) for value in cell.values()) and "error" not in cell:
            for name, below in cell.items():
                look(table, f"{where} / {name}", below)
        else:
            found.append(f"{table} / {where}: {str(cell.get('error', cell))[:160]}")

    for table in CELLS:
        for name, cell in report.get(table, {"the whole table": {}}).items():
            look(table, name, cell)
    return found


if __name__ == "__main__":
    if sys.argv[1:2] == ["inside"]:
        try:
            print(json.dumps(inside(*sys.argv[2:5], quick=sys.argv[5:6] == ["quick"])))
        except BaseException as error:  # the candidate could not even run: that is its result
            print(json.dumps({"error": f"{type(error).__name__}: {str(error)[:200]}"}))
    else:
        sys.path.insert(0, str(BENCH))
        QUICK = sys.argv[1:2] == ["--quick"]
        main()
