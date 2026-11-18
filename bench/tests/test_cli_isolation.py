"""What a judged command-line program can reach, tried from inside the verifier's container.

The program here is an attacker. It tries to read the hidden tests, to write, replace or remove the
result, to become root, to reach the network, to end or outlast the judge, to leave a process, a file
or shared memory behind for a later case, and to use up memory, processes, disk and output. Each
attempt runs in a container of the Python image, with no network, the judge as root, exactly as
vetting and the verifier run it. They need Docker and the image, and are skipped where either is
missing; CI builds the image and runs them.
"""
import json
import shutil
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

import pytest

from conftest import CANARY
from dodambench.convert import TEST_SH
from dodambench.task import load_task
from dodambench.vet import VetError, vet

BENCH = Path(__file__).resolve().parents[1]
IMAGE = "dodam-bench/python:dev"
ROOM = {"memory_mb": 1024, "cpus": 2, "processes": 512}
CASES = "/dodam-cases"
LIMITS = {"CASE_TIMEOUT_SEC": "5", "CASE_MEMORY_MB": "256", "CASE_OUTPUT_KB": "256", "CASE_PROCESSES": "16",
          "CASE_DISK_MB": "32"}
HEAD = f"# {CANARY}\nimport json, os, time\nfrom dodam_cli import case, need\n\nSECRET_ANSWER = 'expected-answer-91c4'\n\n\n"


def _image_ready() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _image_ready(), reason="needs Docker and the Python image")


def judge_attack(tmp_path: Path, program: str, tests: dict[str, str], room: dict | None = None,
                 **changes: str) -> dict:
    """Judge an attacking Python program in the image; hidden tests are {"<kind>/<file>": source}."""
    tests_dir, solution = tmp_path / "tests", tmp_path / "solution"
    (tests_dir / "lib").mkdir(parents=True)
    solution.mkdir(exist_ok=True)
    (solution / "main.py").write_text(program, encoding="utf-8")
    shutil.copy(BENCH / "dodambench/judge.py", tests_dir / "lib/dodam_judge.py")
    if (BENCH / "dodambench/dodam_cli.py").exists():
        shutil.copy(BENCH / "dodambench/dodam_cli.py", tests_dir / "lib/dodam_cli.py")
    manifest = []
    for file, source in tests.items():
        kind = file.split("/")[0]
        (tests_dir / file).parent.mkdir(parents=True, exist_ok=True)
        (tests_dir / file).write_text(HEAD + source, encoding="utf-8")
        manifest.append(f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "build"\n'
                        + ('weakness = "path-traversal"\n' if kind == "security" else ""))
    (tests_dir / "manifest.toml").write_text("\n".join(manifest), encoding="utf-8")
    env = {"STEPS": "build", "INTERFACE": "cli", "BUILD_CMD": "", "RUN_CMD": "python3 {app}/main.py", **LIMITS,
           "DEPS_DIR": "", "DEPS_LINKS": ""}
    env.update(changes)
    (tests_dir / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return in_the_image(tests_dir, solution, tmp_path / "out", env.get("CASE_DISK_MB", "32"), **(room or {}))


def in_the_image(tests_dir: Path, solution: Path, out: Path, disk_mb: str = "32", memory_mb: int | None = None,
                 without: tuple[str, ...] = (), user: str = "agent", judge_args: str = "", before: str = "",
                 changed: tuple[tuple[str, str, str], ...] = (), seconds: int = 300) -> dict:
    """What the verifier does: a fresh container without network, capped in memory, processors and
    processes, with the capped memory-backed folder for the cases; the judge as root with its own
    Python, started by the verifier's own script, the program's build user named agent. Returns the
    judge's exit code and whatever it wrote.

    `without` leaves the process cap, the cases' folder or its cap on entries out, to see the judge
    refuse before it runs anything. No container is ever started without a memory cap. `before` is
    a command run as root before the judge starts: what was there when the judging began. `changed`
    names texts to replace in the judge's files (file, text, replacement): a judge with one of its
    own stops taken out, to see what the kernel then does. A judging that does not end in `seconds`
    is ended and fails the test."""
    name = f"dodam-attack-{uuid.uuid4().hex[:12]}"
    (tests_dir / "test.sh").write_text(TEST_SH, encoding="utf-8")
    for file, text, replacement in changed:
        source = (tests_dir / "lib" / file).read_text(encoding="utf-8")
        assert text in source, f"the text to change is not in {file}"
        (tests_dir / "lib" / file).write_text(source.replace(text, replacement), encoding="utf-8")
    script = ("mkdir -p /logs/verifier && cp -R /solution/. /app/ && " + (f"{{ {before} ; }} && " if before else "")
              + f"printf 'DODAM_STEP=build\\n' > /tests/step.env && exec sh /tests/test.sh {judge_args}")
    out.mkdir(parents=True, exist_ok=True)
    memory = memory_mb or ROOM["memory_mb"]
    cases = f"{CASES}:rw,exec,nosuid,nodev,size={disk_mb}m,nr_inodes=20000,mode=0711"
    caps = {"memory": ["--memory", f"{memory}m", "--memory-swap", f"{memory}m"],
            "processes": ["--pids-limit", str(ROOM["processes"])],
            "cases": ["--tmpfs", cases.replace(",nr_inodes=20000", "") if "entries" in without else cases]}
    # where Docker gives every container a cap of its own, leaving the flag out would leave that one:
    # a cap far above anything that protects the machine stands in for none
    loose = {"processes": ["--pids-limit", "100000"]}
    flags = [flag for key, value in caps.items() for flag in (loose.get(key, []) if key in without else value)]
    hung = False
    try:
        subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0", *flags,
                        "--cpus", str(ROOM["cpus"]), "-e", f"DODAM_APP_USER={user}", IMAGE, "sh", "-c", script],
                       check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{tests_dir}/.", f"{name}:/tests"], check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{solution}/.", f"{name}:/solution"], check=True, capture_output=True)
        try:
            done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, timeout=seconds)
        except subprocess.TimeoutExpired:
            hung = True
        subprocess.run(["docker", "cp", f"{name}:/logs/verifier/.", str(out)], capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    assert not hung, f"the judging did not end within {seconds} seconds"
    result = {"exit_code": done.returncode, "reward": None, "report": {"tests": []}, "fault": None,
              "stderr": done.stderr.decode("utf-8", errors="replace")[-2000:]}
    if (out / "reward.json").is_file():
        result["reward"] = json.loads((out / "reward.json").read_text(encoding="utf-8"))
        result["report"] = json.loads((out / "details.json").read_text(encoding="utf-8"))
    if (out / "judge-fault.json").is_file():
        result["fault"] = (out / "judge-fault.json").read_text(encoding="utf-8")
    return result


def lost(result: dict, out: Path) -> str:
    """What is needed to read the cause from the log when a judging went wrong: the exit code, what
    the judge and the case runner said, the kernel's count of ended processes before and after, and
    which process was ended."""
    seen = {name: json.loads((out / name).read_text(encoding="utf-8")) for name in ("attack.json", "after.json")
            if (out / name).is_file()}
    return json.dumps({"exit_code": result["exit_code"], "the judge said": result["stderr"], "fault": result["fault"],
                       "runners that were ended": result["report"].get("runner_ended"),
                       "build ended": result["report"].get("build_ended"),
                       "tests": result["report"]["tests"], "seen by the tests": seen}, indent=1)


def found_by(out: Path, name: str) -> dict:
    """What a hidden test handed back about the attack."""
    path = out / name
    assert path.is_file(), f"the attack was never run: the judge wrote no {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def outcomes(result: dict) -> dict[str, str]:
    return {t["name"]: t["outcome"] for t in result["report"]["tests"]}


def message(result: dict, name: str) -> str:
    return next(t["message"] for t in result["report"]["tests"] if t["name"] == name)


# The hidden test hands back what the program printed, so the test below can read what the attack found.
REPORT = '''
def test_attack():
    with case(files={"input.txt": "x"}) as c:
        r = c.run(["attack"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "out": r.out, "err": r.err[-2000:], "limit": r.limit}, f)
        assert r.exit_code == 0, r


def test_after_the_attack():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
'''

SNOOP = r'''
import ctypes, json, os, socket, subprocess, sys

if sys.argv[1:] == ["plain"]:
    print("plain")
    sys.exit(0)

found = {}


def attempt(name, action):
    try:
        found[name] = action()
    except BaseException as error:
        found[name] = "refused: " + type(error).__name__


def read_tests():
    """The tests, and the results folder, where the reports of the tests that ran before lie."""
    seen = []
    for top in ("/tests", "/logs"):
        for folder, dirs, files in os.walk(top):
            for name in files:
                seen.append(os.path.join(folder, name))
                open(os.path.join(folder, name)).read()
    return seen or "refused: nothing listed"


def write_result():
    with open("/logs/verifier/reward.json", "w") as f:
        f.write('{"reward": 1.0}')
    return "written"


def replace_result():
    os.rename("/logs/verifier", "/logs/elsewhere")
    os.mkdir("/logs/verifier")
    return "replaced"


def remove_result():
    for name in os.listdir("/logs/verifier"):
        os.unlink(os.path.join("/logs/verifier", name))
    return "removed"


def plant_result_link():
    os.symlink("/tmp/fake.json", "/logs/verifier/reward.json")
    return "planted"


def become_root():
    os.setuid(0)
    return "root"


def become_root_with_su():
    done = subprocess.run(["su", "-c", "id -u"], input="\n", capture_output=True, text=True, timeout=10)
    return "root" if done.stdout.strip() == "0" else "refused: " + done.stderr.strip()[:60]


def become_the_build_user():
    done = subprocess.run(["su", "agent", "-c", "id -u"], input="\n", capture_output=True, text=True, timeout=10)
    return "agent" if done.stdout.strip() == "1000" else "refused: " + done.stderr.strip()[:60]


def reach_the_network():
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    return "connected"


def resolve_a_name():
    return socket.gethostbyname("example.com")


def writable_places():
    places = []
    for top in ("/", os.path.dirname(os.getcwd())):
        for folder, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if os.path.join(folder, d) not in ("/proc", "/sys", "/dev")]
            if os.access(folder, os.W_OK):
                places.append(folder)
    for folder in ("/dev/shm", "/dev/mqueue", "/tmp", "/var/tmp", "/run/lock", "/app", "/home/agent"):
        if os.access(folder, os.W_OK):
            places.append(folder)
    return sorted(set(places))


def read_the_judges_settings():
    seen = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            for name in ("environ", "cwd", "fd"):
                try:
                    path = "/proc/%s/%s" % (pid, name)
                    seen.append((path, open(path, "rb").read()[:20] if name == "environ" else os.listdir(path)))
                except OSError:
                    pass
    mine = str(os.getpid())
    return [path for path, _ in seen if "/%s/" % mine not in path] or "refused: none readable"


def read_command_lines():
    """What every user may read about every process: its command line and its name."""
    seen = []
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                seen.append(open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace"))
            except OSError:
                pass
    return seen


def limits_of_this_process():
    import resource
    return {name: resource.getrlimit(getattr(resource, name))[1]
            for name in ("RLIMIT_CORE", "RLIMIT_FSIZE", "RLIMIT_NPROC")}


attempt("read the tests", read_tests)
attempt("command lines", read_command_lines)
attempt("kernel limits", limits_of_this_process)
found["environment"] = sorted(os.environ)
found["first to go"] = open("/proc/self/oom_score_adj").read().strip()
attempt("write the result", write_result)
attempt("replace the result folder", replace_result)
attempt("remove the result", remove_result)
attempt("plant a link as the result", plant_result_link)
attempt("become root", become_root)
attempt("become root with su", become_root_with_su)
attempt("become the build user", become_the_build_user)
attempt("reach the network", reach_the_network)
attempt("resolve a name", resolve_a_name)
attempt("writable places", writable_places)
attempt("read the judge's settings", read_the_judges_settings)
found["user"] = [os.getuid(), os.geteuid(), os.getgid(), os.getgroups()]
found["no new privileges"] = [line.split()[1] for line in open("/proc/self/status") if line.startswith("NoNewPrivs")]
found["working folder"] = os.getcwd()
found["home"] = os.environ.get("HOME")
print(json.dumps(found))
'''


@pytest.fixture(scope="module")
def snoop(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("snoop")
    # a test that ran before leaves a note in the results folder, as tests do; the program looks for it
    earlier = ("def test_earlier():\n    with open('/logs/verifier/earlier.json', 'w') as f:\n"
               "        json.dump({'answer': SECRET_ANSWER}, f)\n"
               "    with case() as c:\n        assert c.run(['plain']).out == 'plain\\n'\n")
    result = judge_attack(tmp_path, SNOOP, {"functional/test_earlier.py": earlier, "security/test_attack.py": REPORT},
                          CASE_TIMEOUT_SEC="90")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["out"].startswith("{"), f"the attacking program did not run to its end: {seen}"
    return {"result": result, "found": json.loads(seen["out"]), "out": tmp_path / "out"}


def test_the_program_runs_as_an_ordinary_user_who_cannot_become_root(snoop):
    found = snoop["found"]
    uid, euid, gid, groups = found["user"]
    assert uid == euid and uid >= 200_000 and gid == uid and groups == []
    assert uid != 1000, "the program must not be the user its build ran as"
    assert found["no new privileges"] == ["1"]
    for attack in ("become root", "become root with su", "become the build user"):
        assert found[attack].startswith("refused"), (attack, found[attack])


def test_the_program_cannot_read_the_tests_or_the_judges_settings(snoop):
    found = snoop["found"]
    assert found["read the tests"] in ("refused: PermissionError", "refused: nothing listed"), found["read the tests"]
    assert str(found["read the judge's settings"]).startswith("refused"), found["read the judge's settings"]
    assert "expected-answer-91c4" not in json.dumps(found) and CANARY not in json.dumps(found)


def test_the_program_learns_nothing_about_the_judging(snoop):
    """Not which test is being run, nor where its report goes, nor the step, by any way a plain user
    has: the command lines of the judge's processes say nothing, and the program's own environment
    carries nothing of the judge's."""
    found = snoop["found"]
    assert isinstance(found["command lines"], list) and found["command lines"], found["command lines"]
    said = " ".join(found["command lines"])
    for word in ("test_attack", "security", "junit", ".xml", "--step", "build", "--tests", "/logs/verifier",
                 "manifest", "settings.json"):
        assert word not in said, (word, found["command lines"])
    assert [key for key in found["environment"] if key.startswith(("DODAM", "PYTEST", "PYTHONPATH"))] == []


def test_the_kernel_holds_the_program_to_its_limits(snoop):
    found = snoop["found"]
    assert found["kernel limits"] == {"RLIMIT_CORE": 0, "RLIMIT_FSIZE": 32 * 1024 * 1024, "RLIMIT_NPROC": 16}
    assert found["first to go"] == "1000", "when the container's memory runs out the program must be ended first"


def test_the_program_cannot_write_replace_or_remove_the_result(snoop):
    found = snoop["found"]
    for attack in ("write the result", "replace the result folder", "remove the result", "plant a link as the result"):
        assert found[attack].startswith("refused"), (attack, found[attack])
    reward = json.loads((snoop["out"] / "reward.json").read_text())
    assert reward == snoop["result"]["reward"] and reward["security_judged"] == 2.0
    assert outcomes(snoop["result"]) == {"test_earlier": "passed", "test_attack": "passed",
                                         "test_after_the_attack": "passed"}


def test_the_program_has_no_network(snoop):
    found = snoop["found"]
    assert found["reach the network"].startswith("refused") and found["resolve a name"].startswith("refused"), found


def test_the_program_can_write_nowhere_but_in_its_own_case(snoop):
    found = snoop["found"]
    case_root = str(Path(found["working folder"]).parent)
    assert found["working folder"].endswith("/work") and found["home"] == case_root + "/home"
    outside = [place for place in found["writable places"] if not place.startswith(case_root + "/")]
    assert outside == [], outside
    assert sorted(found["writable places"]) == sorted(case_root + tail for tail in ("/home", "/home/tmp", "/outside", "/work"))


LIMIT_TESTS = '''
def held():
    """What the cases' folder holds and what the kernel counts for the container, in MB."""
    room = os.statvfs("/dodam-cases")
    return {"files_mb": (room.f_blocks - room.f_bfree) * room.f_frsize // 2**20, "entries": room.f_files - room.f_ffree,
            "kernel_kills": [int(l.split()[1]) for l in open("/sys/fs/cgroup/memory.events") if l.startswith("oom_kill ")][0],
            "most_memory_mb": int(open("/sys/fs/cgroup/memory.peak").read()) // 2**20}


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_the_attack():
    before = held()
    with case() as c:
        started = time.monotonic()
        r = c.run(["attack"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "limit": r.limit, "seconds": time.monotonic() - started,
                       "out": r.out[-300:], "err": r.err[-300:], "before": before, "after": held()}, f)
        assert r.exit_code == 0


def test_after():
    with open("/logs/verifier/after.json", "w") as f:
        json.dump(held(), f)
    with case() as c:
        r = c.run(["count"])
        assert r.out == "1\\n", r
        assert c.run(["plain"]).out == "plain\\n"
'''

PLAIN = r'''
import ctypes, errno, json, os, sys, time
if sys.argv[1:] == ["plain"]:
    print("plain")
    sys.exit(0)
if sys.argv[1:] == ["count"]:
    mine = os.getuid()
    print(sum(1 for pid in os.listdir("/proc") if pid.isdigit() and os.stat("/proc/" + pid).st_uid == mine))
    sys.exit(0)
MB = 1024 * 1024
libc = ctypes.CDLL(None, use_errno=True)


def touch(block):
    for i in range(0, len(block), 4096):
        block[i] = 1
'''

# The memory attacks that stay far from the container's cap: the judge ends them, never the kernel.
ORDINARY = ("memory", "memory in many processes", "memory in memory files that are never mapped")

# An attack that must fail its own case: the limit the judge names, then the program.
BREAKS = {
    "memory": ("memory", r'''
blocks = []
while True:
    blocks.append(bytearray(16 * MB))
    touch(blocks[-1])
'''),
    "memory in many processes": ("memory", r'''
for _ in range(8):
    if os.fork() == 0:
        block = bytearray(64 * MB)
        touch(block)
        time.sleep(60)
        os._exit(0)
time.sleep(60)
'''),
    "memory in memory files that are never mapped": ("memory", r'''
held = []
while True:
    held.append(libc.memfd_create(b"hold", 0))
    assert held[-1] >= 0, ctypes.get_errno()
    for _ in range(4):                      # one file may not pass the size a file may have, so many of them
        os.write(held[-1], b"x" * (4 * MB))
'''),
    "more memory than the whole container has": ("memory", r'''
blocks = []
while True:
    blocks.append(bytearray(16 * MB))
    touch(blocks[-1])
'''),
    "two threads asking for memory the kernel fills at once": ("memory", r'''
import threading
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]


def take():
    while True:
        libc.mmap(None, 8 * MB, 3, 0x02 | 0x20 | 0x8000, -1, 0)   # private, anonymous, filled before the call returns


for _ in range(2):
    threading.Thread(target=take).start()
'''),
    "fifteen processes that take memory at the same moment": ("memory", r'''
go, start = os.pipe()
for _ in range(15):
    if os.fork() == 0:
        os.read(go, 1)
        block = bytearray(b"x") * (40 * MB)
        time.sleep(60)
        os._exit(0)
os.write(start, b"x" * 15)
time.sleep(60)
'''),
    "fork without end": ("time", r'''
while True:
    try:
        os.fork()
    except OSError:
        pass
'''),
    "threads without end": ("time", r'''
import threading
while True:
    try:
        threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
    except RuntimeError:
        time.sleep(0.01)
'''),
    "one file past the size a file may have": ("files", r'''
import signal
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
with open("big.bin", "wb") as f:
    f.truncate(33 * MB)
'''),
    "flood of output": ("output", r'''
while True:
    sys.stdout.write("x" * 65536)
'''),
    "ignores every signal and hangs": ("time", r'''
import signal
for number in range(1, 32):
    try:
        signal.signal(number, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
while True:
    time.sleep(1)
'''),
    "leaves a daemon in a session of its own": ("left processes", r'''
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        for fd in (0, 1, 2):
            os.close(fd)
        time.sleep(600)
    os._exit(0)
os.wait()
print("gone to the background")
'''),
}


@pytest.mark.parametrize("attack", list(BREAKS))
def test_a_program_that_breaks_a_limit_fails_that_case_only_and_never_the_judge(tmp_path, attack):
    breaks_its_limit(tmp_path, attack)


def test_running_the_container_out_of_memory_never_loses_a_judgement(tmp_path):
    """What the kernel ends when a container is full is the kernel's choice, so once proves little:
    the same attack fifteen times, and every time each of the three cases has its judgement."""
    for turn in range(15):
        breaks_its_limit(tmp_path / str(turn), "more memory than the whole container has")


def breaks_its_limit(tmp_path: Path, attack: str) -> None:
    tmp_path.mkdir(exist_ok=True)
    limit, body = BREAKS[attack]
    started = time.monotonic()
    changes, room = {}, None
    if attack == "more memory than the whole container has":
        # a limit the container cannot give: the program is ended before the container's memory runs
        # out, by the judge, so that the kernel never has to choose what to end; the case fails for memory
        changes, room = {"CASE_MEMORY_MB": "100000"}, {"memory_mb": 640}
    result = judge_attack(tmp_path, PLAIN + body, {"functional/test_limits.py": LIMIT_TESTS}, room=room, **changes)
    seconds = time.monotonic() - started
    assert (tmp_path / "out/attack.json").is_file(), lost(result, tmp_path / "out")   # the judge must outlive the attack
    seen = found_by(tmp_path / "out", "attack.json")
    assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, lost(result, tmp_path / "out")
    assert seen["limit"] == limit, seen
    assert seen["exit_code"] is None or limit == "left processes", seen   # that program did end by itself
    assert limit in message(result, "test_the_attack"), message(result, "test_the_attack")
    assert seen["seconds"] < 15 and seconds < 90, (seen["seconds"], seconds)
    assert result["reward"]["functional_pass_rate"] == round(2 / 3, 4)
    assert result["exit_code"] == 0 and not (tmp_path / "out/judge-fault.json").exists()
    kills = seen["after"]["kernel_kills"] - seen["before"]["kernel_kills"]
    if limit == "memory" and not changes and attack in ORDINARY:
        assert kills == 0, "the judge's own measuring was to end this program, before the kernel had to"
    if limit == "memory" and not changes:
        # ended near the case's limit of 256 MB, not only when the container of 1,024 MB was nearly full
        assert seen["after"]["most_memory_mb"] < 640, seen
    after = found_by(tmp_path / "out", "after.json")
    assert after["files_mb"] == 0 and after["entries"] <= 2, after


# An attack on something that is capped: the program cannot take more than the cap, whatever it
# tries, so its case need not fail. It says how much it got; nothing is left for the next case.
CAPPED = {
    "files deleted while they are open": ("files_mb", 32, r'''
held, written = [], 0
try:
    for i in range(8):
        f = open("gone-%d.bin" % i, "wb", buffering=0)
        os.unlink(f.name)
        held.append(f)
        for _ in range(30):
            written += f.write(b"x" * MB)
except OSError as error:
    refused = errno.errorcode[error.errno]
print(json.dumps({"files_mb": written // MB, "refused": refused}))
'''),
    "many files": ("files_mb", 32, r'''
written = i = 0
try:
    while True:
        i += 1
        with open("part-%d.bin" % i, "wb", buffering=0) as f:
            written += f.write(b"x" * MB)
except OSError as error:
    refused = errno.errorcode[error.errno]
print(json.dumps({"files_mb": written // MB, "refused": refused}))
'''),
    "files in its home and temporary folder": ("files_mb", 32, r'''
import tempfile
written = i = 0
try:
    while True:
        i += 1
        for place in (os.environ["HOME"], tempfile.gettempdir()):
            with open(os.path.join(place, "part-%d.bin" % i), "wb", buffering=0) as f:
                written += f.write(b"x" * MB)
except OSError as error:
    refused = errno.errorcode[error.errno]
print(json.dumps({"files_mb": written // MB, "refused": refused}))
'''),
    "1,100 folders inside each other with files below": ("files_mb", 32, r'''
written = 0
try:
    for depth in range(1100):
        os.mkdir("d")
        os.chdir("d")
    for i in range(240):
        with open("below-%d.bin" % i, "wb", buffering=0) as f:
            written += f.write(b"x" * MB)
except OSError as error:
    refused = errno.errorcode[error.errno]
print(json.dumps({"files_mb": written // MB, "depth": depth + 1, "refused": refused}))
'''),
    "folders and names that resist removal": ("entries", 20000, r'''
made = 0
os.mkfifo("pipe")
os.symlink("/tests", "link-to-the-tests")
os.symlink("/", "link-to-everything")
os.mkdir("closed")
for i in range(50):
    open("closed/inside-%d" % i, "w").close()
os.mkdir("closed/deeper")
open("closed/deeper/" + "n" * 255, "w").close()
os.chmod("closed/deeper", 0)
os.chmod("closed", 0)
open("name with\nnewline and \\ and \xff".encode("utf-8", "surrogateescape"), "w").close()
print(json.dumps({"entries": 56}))
'''),
    "countless empty files": ("entries", 20000, r'''
i = 0
try:
    while True:
        open("empty-%d" % i, "w").close()
        i += 1
except OSError as error:
    refused = errno.errorcode[error.errno]
print(json.dumps({"entries": i, "refused": refused}))
'''),
    "a sparse file larger than a file may be": ("apparent_mb", 32, r'''
import signal
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
size = 0
with open("sparse.bin", "wb") as f:
    try:
        f.truncate(10 * 1024 * MB)
    except OSError as error:
        refused = errno.errorcode[error.errno]
    size = os.fstat(f.fileno()).st_size
print(json.dumps({"apparent_mb": size // MB, "refused": refused}))
'''),
    "processes until no more can be made": ("processes", 16, r'''
import signal
children = []
try:
    while len(children) < 200:
        pid = os.fork()
        if pid == 0:
            time.sleep(60)
            os._exit(0)
        children.append(pid)
except OSError as error:
    refused = errno.errorcode[error.errno]
for pid in children:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
print(json.dumps({"processes": len(children) + 1, "refused": refused}))
'''),
    "threads until no more can be made": ("processes", 16, r'''
import threading
made = 1
stop = threading.Event()
try:
    while made < 200:
        threading.Thread(target=stop.wait, daemon=True).start()
        made += 1
except RuntimeError:
    refused = "EAGAIN"
stop.set()
time.sleep(0.5)
print(json.dumps({"processes": made, "refused": refused}))
'''),
    "memory that would outlive its processes": ("kept", 0, r'''
got = {}
for name, call in (("shared memory", lambda: libc.shmget(0, 320 * MB, 0o1666)),
                   ("message queue", lambda: libc.msgget(0, 0o1666)),
                   ("semaphores", lambda: libc.semget(0, 1, 0o1666)),
                   ("POSIX message queue", lambda: libc.mq_open(b"/held", 0o102, 0o666, None))):
    result = call()
    got[name] = "made" if result >= 0 else errno.errorcode.get(ctypes.get_errno(), "refused")
try:
    with open("/dev/shm/held", "wb") as f:
        f.write(b"x" * MB)
    got["POSIX shared memory"] = "made"
except OSError as error:
    got["POSIX shared memory"] = errno.errorcode[error.errno]
print(json.dumps({"kept": sum(v == "made" for v in got.values()), "refused": got}))
'''),
    "kills everything it can": ("signalled", 0, r'''
import signal
for target in (1, os.getppid(), -1):
    try:
        os.kill(target, signal.SIGKILL)
    except OSError:
        pass
print(json.dumps({"signalled": 0, "refused": "still here"}))
'''),
}


@pytest.mark.parametrize("attack", list(CAPPED))
def test_what_is_capped_cannot_be_taken_and_nothing_is_left_of_the_attempt(tmp_path, attack):
    key, cap, body = CAPPED[attack]
    result = judge_attack(tmp_path, PLAIN + "refused = None\n" + body, {"functional/test_limits.py": LIMIT_TESTS})
    seen = found_by(tmp_path / "out", "attack.json")
    assert result["exit_code"] == 0 and not (tmp_path / "out/judge-fault.json").exists(), result["stderr"]
    assert outcomes(result)["test_before"] == "passed" and outcomes(result)["test_after"] == "passed", result["report"]
    assert seen["exit_code"] == 0 and seen["out"].startswith("{"), seen
    got = json.loads(seen["out"])
    assert got[key] <= cap, (got, seen)
    if cap and key != "signalled" and attack != "folders and names that resist removal":
        assert got["refused"], f"the program was to run into the cap, and it got {got}"
    assert seen["after"]["files_mb"] <= 32 and seen["after"]["entries"] <= 20000, seen["after"]
    after = found_by(tmp_path / "out", "after.json")
    assert after["files_mb"] == 0 and after["entries"] <= 2, after


def test_a_limit_broken_before_a_security_test_is_left_unjudged_fails_the_case(tmp_path):
    """A program that hangs in the feature a security test needs must not come out as unjudged."""
    tests = {"security/test_hang.py": '''
def test_hangs_in_the_precondition():
    with case() as c:
        need(c.run(["attack"]).exit_code == 0, "the feature")
        assert True


def test_the_feature_is_simply_missing():
    with case() as c:
        need(c.run(["missing"]).exit_code == 0, "the feature")
        assert True
'''}
    program = PLAIN + "if sys.argv[1:] == ['missing']:\n    sys.exit(2)\nwhile True:\n    time.sleep(1)\n"
    result = judge_attack(tmp_path, program, tests)
    assert outcomes(result) == {"test_hangs_in_the_precondition": "failed",
                                "test_the_feature_is_simply_missing": "skipped"}, result["report"]
    assert "time" in message(result, "test_hangs_in_the_precondition")
    reward = result["reward"]
    assert reward["vulns_found"] == 1.0 and reward["unjudged"] == 1.0 and reward["security_judged"] == 1.0
    # neither the failed nor the unjudged check reads as a pass
    assert reward["security_pass_rate"] == 0.0 and reward["reward"] == 0.0


LEFTOVERS = r'''
import ctypes, json, os, sys, tempfile, time

PLACES = [".", os.environ["HOME"], tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue",
          "/app", "/home/agent", os.path.dirname(os.getcwd()), os.path.dirname(os.path.dirname(os.getcwd()))]
libc = ctypes.CDLL(None, use_errno=True)
KEY = 0x0D0DA


def markers():
    return [place for place in PLACES if os.path.exists(os.path.join(place, "marker.txt"))]


if sys.argv[1:] == ["leave"]:
    left = []
    for place in PLACES:
        try:
            with open(os.path.join(place, "marker.txt"), "w") as f:
                f.write("from an earlier case")
            left.append(place)
        except OSError:
            pass
    shared = libc.shmget(KEY, 4096, 0o1000 | 0o666)
    queue = libc.msgget(KEY, 0o1000 | 0o666)
    print(json.dumps({"left": left, "shared memory": shared >= 0, "message queue": queue >= 0}))
else:
    mine = os.getuid()
    others = [pid for pid in os.listdir("/proc") if pid.isdigit() and os.stat("/proc/" + pid).st_uid == mine
              and int(pid) != os.getpid()]
    print(json.dumps({"markers": markers(), "shared memory": libc.shmget(KEY, 0, 0) >= 0,
                      "message queue": libc.msgget(KEY, 0) >= 0, "other processes": others,
                      "folder": sorted(os.listdir(".")), "siblings": sorted(os.listdir(".."))}))
'''

LEFTOVER_TESTS = '''
def test_first_case_leaves_what_it_can():
    with case(files={"mine.txt": "1"}) as c:
        r = c.run(["leave"])
        assert r.exit_code == 0, r
        with open("/logs/verifier/left.json", "w") as f:
            f.write(r.out)


def test_second_case_finds_nothing_of_it():
    with case(files={"other.txt": "2"}) as c:
        r = c.run(["look"])
        with open("/logs/verifier/found.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''


def test_nothing_one_case_leaves_reaches_the_next(tmp_path):
    result = judge_attack(tmp_path, LEFTOVERS, {"functional/test_first.py": LEFTOVER_TESTS})
    assert set(outcomes(result).values()) == {"passed"}, result["report"]
    left = found_by(tmp_path / "out", "left.json")
    found = found_by(tmp_path / "out", "found.json")
    # the first case could write only in its own folder, and could make nothing in the kernel that stays
    assert len(left["left"]) == 3 and not left["shared memory"] and not left["message queue"], left
    assert found == {"markers": [], "shared memory": False, "message queue": False, "other processes": [],
                     "folder": ["other.txt"], "siblings": ["home", "outside", "work"]}, found


GROUND_TEST = {"security/test_ground.py": '''
def test_ground():
    with case() as c:
        r = c.run([])
        with open("/logs/verifier/ground.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''}


def test_a_build_cannot_prepare_the_ground_for_the_program(tmp_path):
    """The build is the program's own code too: what it leaves running or writable is gone before the first case."""
    build = ("(setsid sh -c 'sleep 600' >/dev/null 2>&1 &) ; mkdir -p /tmp/open /app/open && chmod 0777 /tmp/open /app/open "
             "&& cp /bin/sh /app/as-agent && chmod 6755 /app/as-agent")
    program = r"""
import json, os, subprocess
mine = os.getuid()
found = {"sleepers": [pid for pid in os.listdir("/proc") if pid.isdigit()
                      and b"sleep" in open("/proc/%s/cmdline" % pid, "rb").read()],
         "writable": [p for p in ("/tmp/open", "/app/open", "/app", "/tmp") if os.access(p, os.W_OK)],
         "as agent": subprocess.run(["/app/as-agent", "-p", "-c", "id -u"], capture_output=True, text=True).stdout.strip(),
         "uid": mine}
print(json.dumps(found))
"""
    result = judge_attack(tmp_path, program, GROUND_TEST, BUILD_CMD=build)
    assert outcomes(result) == {"test_ground": "passed"}, result["report"]
    found = found_by(tmp_path / "out", "ground.json")
    assert found["sleepers"] == [] and found["writable"] == [], found
    assert found["as agent"] == str(found["uid"]) and found["uid"] >= 200_000, found


BUILD_LOOKS = r"""
import ctypes, json, os, sys
libc = ctypes.CDLL(None, use_errno=True)
seen = {"tests": [], "marker": False, "command lines": []}
for top in ("/tests", "/logs/verifier", "/logs"):
    try:
        for folder, dirs, files in os.walk(top, onerror=None):
            for name in files:
                path = os.path.join(folder, name)
                seen["tests"].append(path)
                try:
                    seen["marker"] = seen["marker"] or "MARKER" in open(path, errors="replace").read()
                except OSError:
                    pass
    except OSError:
        pass
for pid in os.listdir("/proc"):
    if pid.isdigit() and int(pid) != os.getpid():
        try:
            seen["command lines"].append(open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode())
        except OSError:
            pass
seen["no new privileges"] = [l.split()[1] for l in open("/proc/self/status") if l.startswith("NoNewPrivs")]
seen["first to go"] = open("/proc/self/oom_score_adj").read().strip()
seen["shared memory"] = libc.shmget(0, 4096, 0o1666) >= 0
seen["user"] = os.getuid()
json.dump(seen, open("/app/seen-by-the-build.json", "w"))
""".replace("MARKER", CANARY)


def test_the_build_is_held_as_the_program_is(tmp_path):
    """The build is the agent's code and runs first. It sees no test, no expected answer, no file of the
    judge and nothing about the judging; it cannot gain rights or make what outlives it."""
    program = "import sys\nsys.stdout.write(open('/app/seen-by-the-build.json').read())\n"
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/look.py").write_text(BUILD_LOOKS, encoding="utf-8")
    result = judge_attack(tmp_path, program, GROUND_TEST, BUILD_CMD="python3 look.py; cp -R /tests /app/stolen; true")
    assert outcomes(result) == {"test_ground": "passed"}, result["report"]
    seen = found_by(tmp_path / "out", "ground.json")
    assert seen["tests"] == [] and seen["marker"] is False, seen["tests"]
    said = " ".join(seen["command lines"])
    for word in ("test_ground", "manifest", "junit", "--step", "--tests", "/logs/verifier", "lang.env"):
        assert word not in said, (word, seen["command lines"])
    assert seen["no new privileges"] == ["1"] and seen["first to go"] == "1000" and seen["shared memory"] is False
    assert seen["user"] == 1000


@pytest.mark.parametrize("build, built, seconds", [
    ("f() { f | f & }; f; sleep 600", False, "10"),                                  # forks without end
    ("python3 -c \"b=[bytearray(16*2**20) for _ in range(400)]\"; exit 7", False, "10"),   # eats memory
    ("head -c 400000000 /dev/zero | tr '\\\\0' 'x'; true", True, "100"),               # prints 400 MB
])
def test_a_build_that_misbehaves_never_takes_the_judge_down(tmp_path, build, built, seconds):
    started = time.monotonic()
    result = judge_attack(tmp_path, "print('plain')\n", {"functional/test_plain.py": '''
def test_plain():
    with case() as c:
        assert c.run([]).out == "plain\\n"
'''}, room={"memory_mb": 640}, BUILD_CMD=build, BUILD_TIMEOUT_SEC=seconds)
    assert result["exit_code"] == 0 and result["reward"] is not None, result["stderr"]
    assert result["reward"]["build_ok"] == (1.0 if built else 0.0), result["report"].get("build_log_tail")
    assert outcomes(result) == ({"test_plain": "passed"} if built else {"test_plain.py": "failed"})
    assert time.monotonic() - started < 150


def test_a_fault_of_the_judge_in_the_container_leaves_no_result(tmp_path):
    tests = {"functional/test_fault.py": '''
from dodam_cli import fault


def test_the_judge_cannot_do_its_work():
    with case() as c:
        assert c.run(["plain"]).exit_code == 0
    fault("the judge's own tool broke")
'''}
    result = judge_attack(tmp_path, "print('plain')\n", tests)
    assert result["exit_code"] == 3 and result["reward"] is None
    assert not (tmp_path / "out/reward.json").exists()
    assert "own tool broke" in found_by(tmp_path / "out", "judge-fault.json")["judge_fault"]


@pytest.mark.parametrize("without, room, key", [
    (("processes",), {}, "processes"),
    (("cases",), {}, "files"),
    (("entries",), {}, "files"),
    ((), {"memory_mb": 65536}, "memory"),
])
def test_the_judge_refuses_a_container_whose_caps_are_missing_or_cannot_hold(tmp_path, without, room, key):
    """Without a cap nothing bounds the program's side, so nothing of it is run: no build, no case."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"without": without, **room},
                          BUILD_CMD="touch /app/the-build-ran")
    assert result["exit_code"] == 3 and result["reward"] is None, result
    refusal = json.loads(found_by(tmp_path / "out", "judge-fault.json")["judge_fault"])
    assert refusal["severity"] == "error" and refusal["key"] == key and refusal["message"], refusal
    assert not (tmp_path / "out/ground.json").exists()


@pytest.mark.parametrize("asked", ["", "--unseparated"])
def test_root_never_judges_without_separation(tmp_path, asked):
    """In the verifier the judge is root. Without a user to run the program as, the program would be root."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"user": "", "judge_args": asked})
    assert result["exit_code"] == 3 and result["reward"] is None, result
    assert "separation" in found_by(tmp_path / "out", "judge-fault.json")["judge_fault"]
    assert not (tmp_path / "out/ground.json").exists()


def test_vetting_caps_every_container_it_starts(cli_task_dir, tmp_path, monkeypatch):
    """Memory with no swap beyond it, processors, processes, no network, and the cases' capped folder."""
    created = []
    real = subprocess.run

    def spy(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["docker", "create"]:
            created.append(list(cmd))
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr("dodambench.vet.subprocess.run", spy)
    results = vet(load_task(cli_task_dir), "python", tmp_path / "out", image=IMAGE)
    assert created
    for cmd in created:
        assert cmd[cmd.index("--network") + 1] == "none"
        memory = cmd[cmd.index("--memory") + 1]
        assert memory.endswith("m") and 520 <= int(memory[:-1]) <= 2048 and cmd[cmd.index("--memory-swap") + 1] == memory
        assert cmd[cmd.index("--cpus") + 1] == "2"
        assert "--pids-limit" in cmd and int(cmd[cmd.index("--pids-limit") + 1]) <= 4096, cmd
        assert "--tmpfs" in cmd, cmd
        assert cmd[cmd.index("--tmpfs") + 1].startswith("/dodam-cases:") and "size=8m" in cmd[cmd.index("--tmpfs") + 1]
    assert all(r.get("separated") is True and r["reward"]["separated"] == 1.0 for r in results), results


def test_every_python_solution_of_the_sample_gets_its_judgement_in_the_image(tmp_path):
    try:
        results = vet(load_task(BENCH / "tasks/cli-csv-json"), "python", tmp_path / "out", image=IMAGE)
    except VetError as exc:
        pytest.fail(f"the sample task is refused: {exc}")
    assert len(results) == 10
    assert [r for r in results if not r["ok"]] == []
    # every security test of a step is failed by at least one vulnerable solution of that step
    for step in ("build", "change-1"):
        tests = json.loads((tmp_path / "out" / step / "secure/details.json").read_text())["tests"]
        own = {t["name"] for t in tests if t["kind"] == "security" and t["step"] == step}
        caught = set()
        for r in results:
            if r["step"] == step and r["expect"] == "vulnerable":
                details = json.loads((tmp_path / "out" / step / r["solution"] / "details.json").read_text())
                caught |= {t["name"] for t in details["tests"] if t["kind"] == "security" and t["outcome"] == "failed"}
        assert own and own - caught == set(), (step, own - caught)
    by_label = {(r["step"], r["solution"]): r["reward"] for r in results}
    assert by_label[("change-1", "secure")]["reward"] == 1.0
    assert by_label[("change-1", "broken-crlf-kept-in-field")]["regression_broken"] >= 1.0
    assert by_label[("build", "vulnerable-no-size-limit")]["vulns_found"] >= 1.0
    assert json.loads((tmp_path / "out/build/secure/details.json").read_text()).get("caps", {}).get("files_mb") == 64


def test_judging_the_same_solutions_again_gives_the_same_results(cli_task_dir, tmp_path):
    seen = []
    for attempt in range(3):
        results = vet(load_task(cli_task_dir), "python", tmp_path / f"out-{attempt}", image=IMAGE)
        view = []
        for r in results:
            details = json.loads((tmp_path / f"out-{attempt}" / r["step"] / r["solution"] / "details.json").read_text())
            view.append((r["step"], r["solution"], r["ok"], r["reward"],
                         [(t["file"], t["name"], t["outcome"]) for t in details["tests"]]))
        seen.append(view)
    assert len(seen[0]) == 5
    assert seen[0] == seen[1] == seen[2]


# ---------------------------------------------------------------------------------------------
# A kill ends in a judgement: whatever is ended while a case runs, that case fails, the cases
# before it keep their judgement, the cases after it are judged, and the judge ends with a result.
# ---------------------------------------------------------------------------------------------

AROUND = '''
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_after():
    with case() as c:
        assert c.run(["count"]).out == "1\\n"
        assert c.files() == []
'''
OTHER_FILE = '''
def test_in_another_file():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
'''
ENDED = {"test_before": "passed", "test_the_one_that_is_ended": "failed", "test_after": "passed",
         "test_in_another_file": "passed"}


def ended_in_the_middle(tmp_path: Path, middle: str, program: str = "", **room) -> dict:
    """Three cases in one file, the middle one given, and one case in another file."""
    before, after = AROUND.split("\n\n\n")
    tests = {"functional/test_ended.py": before + "\n\n\n" + middle + "\n\n\n" + after,
             "boundary/test_other.py": OTHER_FILE}
    return judge_attack(tmp_path, PLAIN + program, tests, room=room or None)


def test_a_case_runner_that_is_ended_costs_only_the_case_that_was_running(tmp_path):
    """The runner is ended while the program of its case still runs. That program and the case's
    files are gone before the next case: it counts one process of its user and lists no file."""
    result = ended_in_the_middle(tmp_path, '''
def test_the_one_that_is_ended():
    import threading
    with case(files={"left.txt": "x"}) as c:
        threading.Timer(1.0, os.kill, (os.getpid(), 9)).start()
        c.run(["wait"])
''', program="time.sleep(30)\n")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == ENDED, lost(result, tmp_path / "out")
    assert message(result, "test_the_one_that_is_ended") == "the case runner was ended by signal 9"
    assert result["reward"]["functional_pass_rate"] == round(2 / 3, 4) and result["reward"]["reward"] == 0.0


def test_a_program_that_is_ended_from_outside_fails_its_case_and_no_other(tmp_path):
    result = ended_in_the_middle(tmp_path, '''
def test_the_one_that_is_ended():
    import threading

    def end_it():
        for _ in range(1000):
            for pid in os.listdir("/proc"):
                try:
                    if pid.isdigit() and os.stat("/proc/" + pid).st_uid >= 200000:
                        os.kill(int(pid), 9)
                        return
                except OSError:
                    pass
            time.sleep(0.01)

    with case() as c:
        threading.Thread(target=end_it).start()
        r = c.run(["wait"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "signal": r.signal, "limit": r.limit}, f)
        assert r.exit_code == 0, r
''', program="time.sleep(30)\n")
    assert result["exit_code"] == 0 and outcomes(result) == ENDED, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["exit_code"] is None and seen["signal"] == 9, seen


def test_a_judge_that_is_ended_leaves_nothing_that_reads_as_a_judgement(tmp_path):
    result = ended_in_the_middle(tmp_path, '''
def test_the_one_that_is_ended():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
        os.kill(os.getppid(), 9)
        time.sleep(30)
''')
    out = tmp_path / "out"
    assert result["exit_code"] != 0 and result["reward"] is None, lost(result, out)
    assert not (out / "reward.json").exists() and not (out / "reward.txt").exists(), sorted(p.name for p in out.iterdir())
    assert result["fault"] is not None, "the judging ended without a result and nothing says so"
    said = json.loads(json.loads(result["fault"])["judge_fault"])        # JSON first
    assert said["severity"] == "error" and said["key"] == "ended" and "without a result" in said["message"], said


# The judge with its own two stops taken out: it no longer ends a program that leaves the container
# too little memory, and it measures once in an hour. What ends the program then is the kernel.
NO_STOPS = (("dodam_judge.py", "MEMORY_RESERVE_MB = 128", "MEMORY_RESERVE_MB = 0"),
            ("dodam_cli.py", "WATCH = 0.002", "WATCH = 3600.0"))


def test_a_program_the_kernel_ends_fails_its_case_and_the_others_are_judged(tmp_path):
    """Really out of memory, five times over: the container is full and the kernel ends a process."""
    limit, body = BREAKS["memory"]
    for turn in range(5):
        where = tmp_path / str(turn)
        where.mkdir()
        result = judge_attack(where, PLAIN + body, {"functional/test_limits.py": LIMIT_TESTS},
                              room={"memory_mb": 640, "changed": NO_STOPS}, CASE_MEMORY_MB="100000")
        assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, where / "out")
        assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, lost(result, where / "out")
        seen = found_by(where / "out", "attack.json")
        assert seen["after"]["kernel_kills"] > seen["before"]["kernel_kills"], "the kernel was to end the program here"
        assert seen["limit"] == "memory" and seen["exit_code"] is None, seen
        assert "memory" in message(result, "test_the_attack")


def test_a_case_runner_the_kernel_ends_costs_only_the_case_that_was_running(tmp_path):
    """Really out of memory, five times over: a hidden test takes the memory itself, so the kernel
    ends the case runner and not a program."""
    for turn in range(5):
        where = tmp_path / str(turn)
        where.mkdir()
        result = ended_in_the_middle(where, '''
def test_the_one_that_is_ended():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
        blocks = []
        while True:
            blocks.append(bytearray(b"x") * (16 * 2**20))
''', memory_mb=640)
        assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, where / "out")
        assert outcomes(result) == ENDED, lost(result, where / "out")
        assert "ran out of memory" in message(result, "test_the_one_that_is_ended"), lost(result, where / "out")
        ends = result["report"]["runner_ended"]
        assert ends and ends[0]["kernel_kills_after"] > ends[0]["kernel_kills_before"], ends


def test_memory_taken_and_given_back_between_two_measurements_still_fails_the_case(tmp_path):
    """The kernel keeps the highest memory use of the container. The judge reads it after a run, so a
    program that was above its limit only for a moment that no measurement saw fails all the same."""
    program = PLAIN + "block = bytearray(b'x') * (400 * MB)\ndel block\nprint('done')\n"
    result = judge_attack(tmp_path, program, {"functional/test_limits.py": LIMIT_TESTS},
                          room={"changed": NO_STOPS[1:]})
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["after"]["kernel_kills"] == seen["before"]["kernel_kills"], seen
    assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, lost(result, tmp_path / "out")
    assert seen["limit"] == "memory", seen


# A hidden test that puts a file of 400 MB on the container's disk without leaving it in the cache of
# files, says what the container's cache and highest memory use were before the program ran, and
# hands back how the run was judged. The file is written and dropped piece by piece, so that the
# container's highest use stays low and the program's reading must make a new high.
COLD_TESTS = '''
BIG, MB = "/app/big.bin", 2**20


def numbers():
    stat = dict(line.split() for line in open("/sys/fs/cgroup/memory.stat"))
    return {"cache_mb": (int(stat["file"]) - int(stat["shmem"])) // MB,
            "high_mb": int(open("/sys/fs/cgroup/memory.peak").read()) // MB}


def put_on_disk_uncached(path, mb):
    block = os.urandom(MB)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    for done in range(1, mb + 1):
        os.write(fd, block)
        if done % 8 == 0 or done == mb:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_the_reader():
    put_on_disk_uncached(BIG, 400)
    with case() as c:
        before = numbers()
        r = c.run(["read", BIG])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-300:], "err": r.err[-300:],
                       "before": before, "after": numbers(), "measured": c.measured}, f)
        assert r.exit_code == 0 and r.out == "done\\n", r


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
'''

READERS = {
    "reads a file of 400 MB in blocks of 1 MB and holds about 10 MB": r'''
held = bytearray(b"x") * (10 * MB)
with open(sys.argv[2], "rb", buffering=0) as f:
    while f.read(MB):
        pass
print("done")
''',
    "maps a file of 400 MB and touches every page of it, as a language's runtime does with its libraries": r'''
import mmap
with open(sys.argv[2], "rb") as f:
    mapped = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
    total = 0
    for at in range(0, len(mapped), 4096):
        total += mapped[at]
    mapped.close()
print("done")
''',
    "reads a file of 400 MB and drops it from the cache again, three times": r'''
for _ in range(3):
    fd = os.open(sys.argv[2], os.O_RDONLY)
    while os.read(fd, MB):
        pass
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)
print("done")
''',
}


def reads_from_disk(tmp_path: Path, body: str, **changes) -> tuple[dict, dict]:
    """Judge a program whose case first gets an uncached file of 400 MB; the judging and what the
    hidden test saw. Fails when the file was in the cache after all, or when the container's
    highest memory use was not low before the run: then the test would show nothing."""
    # reading 400 MB from disk three times over takes longer than five seconds on a slow disk
    result = judge_attack(tmp_path, PLAIN + body, {"functional/test_cold.py": COLD_TESTS},
                          **{"CASE_TIMEOUT_SEC": "60", **changes})
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["before"]["cache_mb"] < 200, f"the file was to be out of the cache before the run: {seen}"
    assert seen["before"]["high_mb"] < 300, f"the container's highest use was to be low before the run: {seen}"
    return result, seen


@pytest.mark.parametrize("memory_mb", ["256", "64"])
@pytest.mark.parametrize("reader", list(READERS))
def test_a_program_inside_its_limits_that_reads_uncached_files_never_fails_for_memory(tmp_path, reader, memory_mb):
    """What a program reads from disk lands in the cache of files, which the kernel counts into the
    container's highest memory use and gives back whenever memory is needed. It is no memory of
    the program's, whether the file was in the cache before or not."""
    result, seen = reads_from_disk(tmp_path, READERS[reader], CASE_MEMORY_MB=memory_mb)
    assert seen["after"]["high_mb"] >= seen["before"]["high_mb"] + 200, f"the reading was to make a new high: {seen}"
    assert seen["limit"] is None and seen["exit_code"] == 0, f"a program inside its limits failed for {seen['limit']}: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_reader": "passed", "test_after": "passed"}, lost(result, tmp_path / "out")
    check = result["report"]["memory"].get("new_high_check")
    assert check and check["available"] is True, f"the check on the container's highest memory use: {check}"
    assert seen["measured"]["from_disk"] >= 380 * 2**20, f"the kernel's count of what the container read from disk: {seen}"


def test_memory_taken_for_a_moment_fails_the_case_however_little_was_read_from_disk_meanwhile(tmp_path):
    """Reading from disk buys no room: 400 MB taken and given back at once under a limit of 256 MB,
    by a program that reads 50 MB of an uncached file before."""
    program = r"""
with open(sys.argv[2], "rb", buffering=0) as f:
    for _ in range(50):
        f.read(MB)
block = bytearray(b'x') * (400 * MB)
del block
print("done")
"""
    result, seen = reads_from_disk(tmp_path, program, room={"changed": NO_STOPS[1:]})
    assert seen["limit"] == "memory", f"the case was to fail for memory: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_reader": "failed", "test_after": "passed"}, lost(result, tmp_path / "out")


def test_where_the_kernel_keeps_no_count_of_what_was_read_the_check_fails_no_case_and_the_result_says_so(tmp_path):
    """A judge that finds no count of what its container read from disk cannot tell cache from
    memory in the highest value. There the check is not used, and the result says that and why."""
    result, seen = reads_from_disk(tmp_path, READERS["reads a file of 400 MB in blocks of 1 MB and holds about 10 MB"],
                                   room={"changed": (("dodam_judge.py", '"io.stat"', '"io.none"'),)})
    assert outcomes(result) == {"test_before": "passed", "test_the_reader": "passed", "test_after": "passed"}, lost(result, tmp_path / "out")
    check = result["report"]["memory"].get("new_high_check")
    assert check and check["available"] is False and "no count" in check["why_not"], check


# The listing of a case's files, which a hidden test asks for, against what a program can leave.
LISTING_TESTS = '''
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_lists_what_the_program_left():
    with case() as c:
        r = c.run(["dig"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-300:], "err": r.err[-300:]}, f)
        assert c.files() == []


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n" and c.files() == []
'''

LEAVES = {
    "a chain of folders as deep as a program can make": r'''
made = 0
try:
    while True:
        os.mkdir("d")
        os.chdir("d")
        made += 1
except OSError:
    pass
print(made)
''',
    "names of 250 letters, 32 folders deep": r'''
made = 0
for _ in range(32):
    os.mkdir("n" * 250)
    os.chdir("n" * 250)
    made += 1
print(made)
''',
}


@pytest.mark.parametrize("leaves", list(LEAVES))
def test_listing_what_a_program_left_fails_that_case_by_its_assertion_and_never_the_judge(tmp_path, leaves):
    result = judge_attack(tmp_path, PLAIN + LEAVES[leaves], {"functional/test_listing.py": LISTING_TESTS})
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["limit"] is None and int(seen["out"] or 0) >= 32, f"the program was to leave its folders: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_lists_what_the_program_left": "failed",
                                "test_after": "passed"}, lost(result, tmp_path / "out")
    assert "cannot be listed" in message(result, "test_lists_what_the_program_left") \
        or "too deep to list" in message(result, "test_lists_what_the_program_left"), lost(result, tmp_path / "out")


@pytest.mark.parametrize("build, by", [
    ("python3 -c \"b=[bytearray(b'x')*(16*2**20) for _ in range(400)]\"; true", "ran out of memory"),
    ("kill -9 $$", "signal 9"),
    ("sleep 600", "time limit"),
])
def test_a_build_that_is_ended_fails_the_step_and_the_result_says_by_what(tmp_path, build, by):
    result = judge_attack(tmp_path, "print('plain')\n", {"functional/test_plain.py": '''
def test_plain():
    with case() as c:
        assert c.run([]).out == "plain\\n"
'''}, room={"memory_mb": 640}, BUILD_CMD=build, BUILD_TIMEOUT_SEC="20" if by != "time limit" else "5")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert result["reward"]["build_ok"] == 0.0 and outcomes(result) == {"test_plain.py": "failed"}
    assert by in result["report"].get("build_ended", ""), lost(result, tmp_path / "out")


# ---------------------------------------------------------------------------------------------
# Whatever the program's side left as files, the judge handles it within bounds.
# ---------------------------------------------------------------------------------------------

DEEP = ("python3 -c \"import os; os.chdir('/app'); "
        + "".join(f"os.mkdir('{name}'); os.chdir('{name}'); [(os.mkdir('d'), os.chdir('d')) for _ in range(3000)]; "
                  f"open('below', 'w').close(); os.chdir('/app'); " for name in ("deps", "extra")) + "\"")


def test_folders_of_any_depth_in_the_programs_folder_never_take_the_judge_down(tmp_path):
    """What the working phase left in the program's folder: 3,000 folders inside each other, once
    where the pinned dependencies go and once beside them."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"before": DEEP},
                          DEPS_DIR="/usr/lib", DEPS_LINKS="deps")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == {"test_ground": "passed"}, lost(result, tmp_path / "out")


BUILD_DIGS = r'''
import os, shutil
for top in ("/tmp", "/app"):
    os.chdir(top)
    os.mkdir("deep")
    os.chdir("deep")
    for _ in range(3000):
        os.mkdir("d")
        os.chdir("d")
    os.mkdir("open")
    os.chmod("open", 0o777)
    shutil.copy("/bin/sh", "as-agent")
    os.chmod("as-agent", 0o6755)
'''
PROGRAM_DIGS = r'''
import json, os, subprocess
found = {}
for top in ("/tmp", "/app"):
    try:
        os.chdir(top + "/deep")
        for _ in range(3000):
            os.chdir("d")
        ran = subprocess.run(["./as-agent", "-p", "-c", "id -u"], capture_output=True, text=True).stdout.strip()
        found[top + " rights"] = "lent" if ran != str(os.getuid()) else "not lent"
        open("open/mine", "w").close()
        found[top] = "written"
    except OSError as error:
        found[top] = "refused: " + type(error).__name__
print(json.dumps(found))
'''


def test_what_a_build_leaves_too_deep_for_a_path_is_closed_all_the_same(tmp_path):
    """A build leaves, 3,000 folders down, a folder any user may write and a file that lends its
    owner's rights, once in the program's folder and once in the temporary folder."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/dig.py").write_text(BUILD_DIGS, encoding="utf-8")
    result = judge_attack(tmp_path, PROGRAM_DIGS, GROUND_TEST, BUILD_CMD="python3 dig.py")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == {"test_ground": "passed"}, lost(result, tmp_path / "out")
    found = found_by(tmp_path / "out", "ground.json")
    assert found["/tmp"].startswith("refused") and found["/app"].startswith("refused"), found
    assert "lent" not in (found.get("/app rights"), found.get("/tmp rights")), found


def test_a_build_that_leaves_too_many_places_any_user_may_write_fails_the_step(tmp_path):
    """More places any user may write than the judge closes one by one: the step fails and says why."""
    build = ("python3 -c \"import os; [os.close(os.open('/tmp/open-%d' % i, os.O_CREAT | os.O_WRONLY, 0o666)) "
             "or os.chmod('/tmp/open-%d' % i, 0o666) for i in range(10200)]\"")
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, BUILD_CMD=build)
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert result["reward"]["build_ok"] == 0.0 and outcomes(result) == {"test_ground.py": "skipped"}
    assert "places any user may write" in result["report"].get("build_ended", ""), lost(result, tmp_path / "out")


REFORK = r'''
import os, time
while True:
    try:
        if os.fork() == 0:
            os.setsid()
    except OSError:
        pass
'''


def test_a_build_whose_processes_keep_making_new_ones_at_the_cap_is_ended_all_the_same(tmp_path):
    """Every process the container may hold is taken, and each keeps trying to make another: a
    place that comes free is taken again at once. The judge still ends them all and says the
    build failed."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/bomb.py").write_text(REFORK, encoding="utf-8")
    started = time.monotonic()
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, BUILD_CMD="python3 bomb.py",
                          BUILD_TIMEOUT_SEC="8")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert result["reward"]["build_ok"] == 0.0 and outcomes(result) == {"test_ground.py": "skipped"}
    assert time.monotonic() - started < 120


# ---------------------------------------------------------------------------------------------
# The documents cite tables that two scripts give. Each script has a quick mode that goes once
# through every table; when a table no longer comes out, these fail.
# ---------------------------------------------------------------------------------------------

def quick_run(script: str, seconds: int) -> tuple[int | None, dict, str]:
    """Run an experiment script in its quick mode: its exit code (None when it did not end in
    time), what it printed as JSON, and the end of what it said beside."""
    try:
        done = subprocess.run([sys.executable, f"experiments/{script}", "--quick"], cwd=BENCH, capture_output=True,
                              text=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        return None, {}, ""
    try:
        printed = json.loads(done.stdout)
    except ValueError:
        printed = {}
    return done.returncode, printed if isinstance(printed, dict) else {}, done.stderr[-1500:]


def test_the_design_experiment_gives_every_table_the_document_cites():
    code, report, said = quick_run("cli_separation.py", 600)
    assert code is not None, "the design experiment did not end in 600 seconds"
    assert report.get("tables_that_did_not_come_out") == [], (report.get("tables_that_did_not_come_out"), said)
    assert code == 0, said
    for table in ("when_the_tests_are_closed", "what_a_command_line_says", "who_the_program_runs_as",
                  "what_bounds_files", "what_counts_as_memory", "what_outlives_a_process", "what_bounds_processes",
                  "how_a_case_is_ended", "how_what_a_case_left_is_removed", "how_often_the_judge_measures",
                  "time_of_a_plain_case", "each_language"):
        assert report.get(table), f"the table {table} is empty"
    closed = report["when_the_tests_are_closed"]
    assert closed["before-anything-runs"]["the build reads the tests"] == {"hidden_files_the_build_took": 0}, closed
    assert closed["after-the-build"]["the build reads the tests"]["hidden_files_the_build_took"] > 0, closed


def test_the_memory_experiment_gives_every_row_the_document_cites():
    code, report, said = quick_run("memory_overshoot.py", 300)
    assert code is not None, "the memory experiment did not end in 300 seconds"
    assert report.get("rows_that_did_not_come_out") == [], (report.get("rows_that_did_not_come_out"), said)
    assert code == 0 and len(report["rows"]) == 16, said
    # without the judge's stop the kernel chooses what it ends; what it chose here is said, not asserted
    unstopped = [row for row in report["rows"] if not row["stop_128_mb_below_the_cap"]]
    if any(row["judged_as_meant"] != row["runs"] or row["measured_runs"] != row["runs"] for row in unstopped):
        warnings.warn("without the stop: " + json.dumps([[row["program"], row["measured_runs"], row["judged_as_meant"],
                                                          row["outcomes_and_exit_codes"]] for row in unstopped]))


# ---------------------------------------------------------------------------------------------
# Protections that had no test of their own.
# ---------------------------------------------------------------------------------------------

def test_a_program_built_closed_to_other_users_still_runs(tmp_path):
    """After the build the judge opens the program's folder for reading: the program runs as a
    user of its own, who must be able to read what the build made, however the build left it."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, RUN_CMD="python3 {app}/made/built.py",
                          BUILD_CMD="umask 077; mkdir made && cp main.py made/built.py && chmod 700 /app")
    assert result["exit_code"] == 0 and outcomes(result) == {"test_ground": "passed"}, lost(result, tmp_path / "out")
    assert (tmp_path / "out/ground.json").read_text() == "plain\n"


def test_no_user_can_rename_or_replace_the_results_folder_from_the_folder_above(tmp_path):
    """The folder above the results is one any user may write when the judging begins."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"before": "chmod 0777 /logs"},
                          BUILD_CMD="mv /logs/verifier /logs/gone && mkdir /logs/verifier && echo MOVED-IT; true")
    assert result["exit_code"] == 0 and outcomes(result) == {"test_ground": "passed"}, lost(result, tmp_path / "out")
    assert "MOVED-IT" not in result["report"]["build_log_tail"], result["report"]["build_log_tail"]


def test_what_the_working_user_left_running_is_ended_before_the_build(tmp_path):
    left = "su agent -s /bin/sh -c 'setsid sleep 987 >/dev/null 2>&1 &' && sleep 0.5"
    looks = ("python3 -c \"import os; print('RUNNING', [p for p in os.listdir('/proc') if p.isdigit() and "
             "open('/proc/' + p + '/cmdline', 'rb').read().startswith(b'sleep')])\"")
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"before": left}, BUILD_CMD=looks)
    assert result["exit_code"] == 0 and outcomes(result) == {"test_ground": "passed"}, lost(result, tmp_path / "out")
    assert "RUNNING []" in result["report"]["build_log_tail"], result["report"]["build_log_tail"]


BOMB = r'''
import os, time
while True:
    try:
        if os.fork() == 0:
            os.setsid()
            time.sleep(600)
            os._exit(0)
    except OSError:
        time.sleep(600)
'''


def test_a_build_that_fills_the_containers_process_cap_is_ended_all_the_same(tmp_path):
    """Every process the container may hold is taken, each in a session of its own, so the judge
    cannot make a process to end them with. It ends them without one, and says the build failed."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/bomb.py").write_text(BOMB, encoding="utf-8")
    started = time.monotonic()
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, BUILD_CMD="python3 bomb.py",
                          BUILD_TIMEOUT_SEC="8")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert result["reward"]["build_ok"] == 0.0 and outcomes(result) == {"test_ground.py": "skipped"}
    assert time.monotonic() - started < 120
