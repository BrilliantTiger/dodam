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
from pathlib import Path

import pytest

from conftest import CANARY, note_figures
from dodambench.convert import TEST_SH
from dodambench.judge import RECORDS_LEFT_OUT_MB, UNMEASURED
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


# What a container really holds is read from outside it, by a helper that has what the judged
# container must never get: the kernel's own files for the container's memory, open for writing.
# Asked by a hidden test, the helper has the kernel give back everything of the container's that
# it can give back, and reads what is then still charged to it, memory and swap together. Whatever
# keeps a page shows in that figure, known to the judge or not. Two such readings, one when a case
# begins and one while the program holds what it holds, differ by what the case really holds.
# Before it reads, the helper stops the judged container, has the machine write the pages that
# wait to be written to disk (such a page cannot be given back before it is written), has the
# kernel let go, and reads once the figure stands still; then it lets the container run on.
READER = r"""
import json, os, time
asked, held = "/reading", "/group"
os.chmod(asked, 0o700)


def number(name):
    try:
        with open(f"{held}/{name}") as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


def charged():
    return number("memory.current") + number("memory.swap.current")


def machine():
    # the memory the whole machine has in use: everything but what is free and what the machine
    # can give back; memory that is charged to no container shows here and nowhere else
    with open("/proc/meminfo") as f:
        kb = {line.split(":")[0]: int(line.split()[1]) for line in f}
    return (kb["MemTotal"] - kb["MemFree"] - kb["Buffers"] - kb["Cached"] + kb["Shmem"] - kb["SReclaimable"]) * 1024


def give_back(first):
    try:
        with open(f"{held}/memory.reclaim", "wb", buffering=0) as f:
            f.write(str(first).encode())
    except OSError:
        pass                          # the kernel says so when it could not give back all that was asked


def waiting_to_be_written():
    with open(f"{held}/memory.stat") as f:
        stat = dict(line.split()[:2] for line in f.read().splitlines())
    return int(stat.get("file_dirty", 0)) + int(stat.get("file_writeback", 0))


def reading():
    # The judged container stands still while it is read. A program that reads a file again and
    # again would otherwise bring back, during the reading, the pages the kernel has just given
    # back, and they would read as held although nothing keeps them.
    with open(f"{held}/cgroup.freeze", "w") as f:
        f.write("1")
    try:
        for _ in range(2000):
            with open(f"{held}/cgroup.events") as f:
                if "frozen 1" in f.read():
                    break
            time.sleep(0.001)
        return standing_still()
    finally:
        with open(f"{held}/cgroup.freeze", "w") as f:
            f.write("0")


def standing_still():
    started, first, passes, dropped = time.monotonic(), charged(), 0, True
    # a page of a file that is still to be written to disk cannot be given back before it is
    # written: the machine is told to write what waits, and the figure is read after that
    while waiting_to_be_written() > 65536 and time.monotonic() - started < 20:
        os.sync()
    last = first
    while passes < 12:
        give_back(first)
        passes += 1
        now = charged()
        if passes > 1 and now > last - 65536:
            break
        last = now
    # asked for the container alone, the kernel keeps some records nothing holds on to; asked for
    # the whole machine, it lets go of every one of them
    try:
        with open("/drop", "wb", buffering=0) as f:
            f.write(b"2")
    except OSError:
        dropped = False
    give_back(first)
    # what the kernel lets go of it frees a moment later: the figure is read once it has stood still,
    # with a time of its own, however long writing the waiting pages took
    last, still, settling = charged(), 0, time.monotonic()
    while still < 6 and time.monotonic() - settling < 3:
        time.sleep(0.005)
        now = charged()
        still = still + 1 if now > last - 65536 else 0
        last = min(last, now)
    with open(f"{held}/memory.stat") as f:
        stat = dict(line.split()[:2] for line in f.read().splitlines())
    return {"charged": charged(), "machine": machine(), "charged_before": first, "passes": passes, "dropped": dropped,
            "seconds": time.monotonic() - started,
            "stat": {name: int(stat.get(name, 0)) for name in (
                "anon", "file", "shmem", "kernel", "slab_reclaimable", "slab_unreclaimable", "sock", "pagetables",
                "unevictable", "file_mapped")}}


while True:
    for name in os.listdir(asked):
        if name.startswith("ask-") and not os.path.exists(f"{asked}/answer-{name[4:]}"):
            with open(f"{asked}/answer.tmp", "w") as f:
                json.dump(reading(), f)
            os.rename(f"{asked}/answer.tmp", f"{asked}/answer-{name[4:]}")
    time.sleep(0.002)
"""

# For the hidden tests of an attack: ask for a reading, and take one while the program holds what
# it holds. The program is given two files; it removes the first when it holds everything, and
# ends when the second is gone.
READING = r"""
import threading


def reading(label):
    with open("/logs/reading/ask.tmp", "w") as f:
        f.write(label)
    os.rename("/logs/reading/ask.tmp", "/logs/reading/ask-" + label)
    for _ in range(6000):
        try:
            with open("/logs/reading/answer-" + label) as f:
                return json.load(f)
        except (OSError, ValueError):
            time.sleep(0.005)
    raise AssertionError("no reading came from outside the container")


def read_while_held(c, label, into):
    c.write("holding-not-yet", "")
    c.write("not-read-yet", "")

    over = threading.Event()

    def wait():
        while not over.is_set():
            if not os.path.exists(c.work / "holding-not-yet"):
                room = os.statvfs(c.work)
                into.update(reading(label), files=(room.f_blocks - room.f_bfree) * room.f_frsize)
                break
            time.sleep(0.005)
        try:
            os.unlink(c.work / "not-read-yet")
        except OSError:
            pass
    waiting = threading.Thread(target=wait, daemon=True)
    waiting.start()

    def after_the_run():
        over.set()
        waiting.join(60)
    return after_the_run


def attempt(name, args, files=None):
    # one case: a reading when it begins, the program run once, a reading while it holds
    with case(files=files) as c:
        start, while_held = reading(name + "-start"), {}
        after_the_run = read_while_held(c, name + "-held", while_held)
        r = c.run(args)
        after_the_run()
        seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
        seen[name] = {"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-2000:], "err": r.err[-600:],
                      "start": start, "held": while_held, "measured": c.measured}
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump(seen, f)
        return r
"""
# the program's part of it
HELD = r"""
def held_until_read():
    os.unlink("holding-not-yet")
    for _ in range(12000):
        if not os.path.exists("not-read-yet"):
            return
        time.sleep(0.005)
"""


# How far a reading may lie from what it should be. The rule: take the largest use of tolerance
# over every turn of the reading's own test and of the bound tests on the machine of the
# automatic check; the tolerance is twice that, rounded up to a whole MB, and not under 2. Above 6
# it is not set: the reading is then not good enough. In the first six runs after the helper came
# to stop the container while it reads (two on each of the commits 1dee54b, c270aaf and 917115f),
# the largest uses were 1.96, 1.39, 1.32, 1.46, 1.59 and 1.71 MB, so the tolerance is 4. It is not
# worked out again from later runs: they are held to it. Held to one processor, ten readings of one program lie 0.03 MB apart; on two
# processors 0.63 MB, around the same mean: how far readings scatter grows with the processors.
TOLERANCE_MB = 4


def really_mb(seen: dict) -> float:
    """What a case really held while its program held what it held, as read from outside: what
    was charged to the container then, less what was charged when the case began and the case's
    own files, which have a limit of their own."""
    assert seen["held"], f"no reading was taken while the program held: {seen}"
    assert seen["held"]["dropped"] and seen["start"]["dropped"], f"the kernel was not asked to let go of everything: {seen}"
    return (seen["held"]["charged"] - seen["start"]["charged"] - seen["held"]["files"]) / 2**20


def charged_to_nobody_mb(seen: dict) -> float:
    """How much more the whole machine has in use while the program holds than when the case
    began, beyond what is charged to the judged container. Memory the kernel charges to no
    container would show here. Other things on the machine move this figure too, so it is
    printed and nothing is asserted on it."""
    return ((seen["held"]["machine"] - seen["start"]["machine"]) - (seen["held"]["charged"] - seen["start"]["charged"])) / 2**20


def left_out_mb(limit_mb: int) -> float:
    """The most of the kernel's records that is left out of a case's memory, by the judge's rule."""
    return min(limit_mb / 4, RECORDS_LEFT_OUT_MB)


def reader_command(name: str, group: str) -> list[str]:
    """The command that starts the helper for the judged container `name`, whose folder of the
    kernel's is `group`. The helper gets three things and no more: that one folder, the machine's
    file for letting go of unused records, and the folder it is asked through. It has no network,
    no rights beyond a container's usual ones, and a memory cap and a process cap of its own."""
    return ["docker", "run", "--detach", "--name", f"{name}-reader", "--network", "none", "--user", "0",
            "--memory", "256m", "--pids-limit", "64", "--volume", f"/sys/fs/cgroup/{group}:/group",
            "--volume", "/proc/sys/vm/drop_caches:/drop",
            "--volume", f"{name}-asked:/reading", IMAGE, "python3", "-c", READER]


def in_the_image(tests_dir: Path, solution: Path, out: Path, disk_mb: str = "32", memory_mb: int | None = None,
                 without: tuple[str, ...] = (), user: str = "agent", judge_args: str = "", before: str = "",
                 changed: tuple[tuple[str, str, str], ...] = (), seconds: int = 300, reading: bool = False,
                 processes: int | None = None, own_filter_only: bool = False,
                 capabilities: tuple[str, ...] = (), mounts: tuple[str, ...] = (), volumes: tuple[str, ...] = ()) -> dict:
    """What the verifier does: a fresh container without network, capped in memory, processors and
    processes, with the capped memory-backed folder for the cases; the judge as root with its own
    Python, started by the verifier's own script, the program's build user named agent. Returns the
    judge's exit code and whatever it wrote.

    `without` leaves the process cap, the cases' folder or its cap on entries out, to see the judge
    refuse before it runs anything. No container is ever started without a memory cap. `before` is
    a command run as root before the judge starts: what was there when the judging began. `changed`
    names texts to replace in the judge's files (file, text, replacement): a judge with one of its
    own stops taken out, to see what the kernel then does. A judging that does not end in `seconds`
    is ended and fails the test. With `reading` a helper runs outside the container and the hidden
    tests can ask it what the container really holds; the folder they ask through is root's alone.
    With `own_filter_only` the container gets no call filter from Docker, so that a call that is
    refused is refused by the judge's own. `capabilities` are given to the container's root beside
    Docker's usual ones, for a hidden test that needs one; the program's side never has any.
    `mounts` are memory-backed folders given to the container beside the cases' one, as Docker's
    `--tmpfs` takes them; `volumes` are Docker volumes given to it, as `--volume` takes them."""
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
            "processes": ["--pids-limit", str(processes or ROOM["processes"])],
            "cases": ["--tmpfs", cases.replace(",nr_inodes=20000", "") if "entries" in without else cases]}
    # where Docker gives every container a cap of its own, leaving the flag out would leave that one:
    # a cap far above anything that protects the machine stands in for none
    loose = {"processes": ["--pids-limit", "100000"]}
    flags = [flag for key, value in caps.items() for flag in (loose.get(key, []) if key in without else value)]
    hung = False
    if own_filter_only:
        flags += ["--security-opt", "seccomp=unconfined"]
    for capability in capabilities:
        flags += ["--cap-add", capability]
    for mount in mounts:
        flags += ["--tmpfs", mount]
    for volume in volumes:
        flags += ["--volume", volume]
    if reading:
        flags += ["--volume", f"{name}-asked:/logs/reading"]
    try:
        subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0", *flags,
                        "--cpus", str(ROOM["cpus"]), "-e", f"DODAM_APP_USER={user}", IMAGE, "sh", "-c", script],
                       check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{tests_dir}/.", f"{name}:/tests"], check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{solution}/.", f"{name}:/solution"], check=True, capture_output=True)
        running = subprocess.Popen(["docker", "start", "--attach", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if reading:
            # the helper gets the one folder of the kernel's that belongs to the judged container, the
            # machine's file for letting go of unused records, and the folder it is asked through
            ident = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", name], check=True, capture_output=True,
                                   text=True).stdout.strip()
            driver = subprocess.run(["docker", "info", "--format", "{{.CgroupDriver}}"], check=True, capture_output=True,
                                    text=True).stdout.strip()
            group = f"system.slice/docker-{ident}.scope" if driver == "systemd" else f"docker/{ident}"
            subprocess.run(reader_command(name, group), check=True, capture_output=True)
        try:
            said, errors = running.communicate(timeout=seconds)
            done = subprocess.CompletedProcess(running.args, running.returncode, said, errors)
        except subprocess.TimeoutExpired:
            running.kill()
            hung = True
        subprocess.run(["docker", "cp", f"{name}:/logs/verifier/.", str(out)], capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
        if reading:
            subprocess.run(["docker", "rm", "--force", f"{name}-reader"], capture_output=True)
            subprocess.run(["docker", "volume", "rm", "--force", f"{name}-asked"], capture_output=True)
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


# Named pipes and sockets any user may write, left by the build in the temporary folders and in its
# home, which it opens to everyone. When the build ends it notes when each was last changed.
SPECIAL = ("/tmp/pipe", "/var/tmp/pipe", "/home/agent/pipe", "/tmp/socket")
LEAVES_SPECIAL_FILES = (
    "chmod 0755 /home/agent && mkfifo /tmp/pipe /var/tmp/pipe /home/agent/pipe && "
    "chmod 0666 /tmp/pipe /var/tmp/pipe /home/agent/pipe && "
    "python3 -c \"import socket; socket.socket(socket.AF_UNIX).bind('/tmp/socket')\" && chmod 0777 /tmp/socket && "
    "python3 -c \"import json, os; json.dump({p: os.lstat(p).st_mtime_ns for p in "
    + repr(SPECIAL).replace('"', "'") + "}, open('/app/built.json', 'w'))\"")
TRIES_SPECIAL_FILES = r'''
import json, os, sys
SPECIAL = SPECIAL_FILES
if sys.argv[1:] == ["try"]:
    found = {}
    for place in SPECIAL:
        found[place] = {"writable": os.access(place, os.W_OK), "written": False}
        try:
            fd = os.open(place, os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, b"x")
            os.close(fd)
            found[place]["written"] = True
        except OSError:
            pass
    print(json.dumps(found))
else:
    print(json.dumps({"now": {place: os.lstat(place).st_mtime_ns for place in SPECIAL},
                      "built": json.load(open("/app/built.json"))}))
'''.replace("SPECIAL_FILES", repr(SPECIAL))
SPECIAL_TESTS = '''
def test_first_case_tries_what_the_build_left():
    with case() as c:
        r = c.run(["try"])
        with open("/logs/verifier/tried.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r


def test_second_case_looks_whether_any_of_it_changed():
    with case() as c:
        r = c.run(["look"])
        with open("/logs/verifier/looked.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''


# What the build and the working phase leave that a case may read: files and folders in the program's
# folder and in a temporary folder, one of them changed, by the build's say, in the year 2100. The first
# case notes when each was last read, then reads some and lists the folders; the second notes it again.
READ_MARKS = ("/app/bits/b0", "/app/bits/b1", "/app/bits/b2", "/app/bits/b3", "/app/bits", "/app/given.txt",
              "/tmp/t0", "/tmp/t1", "/tmp")
LEAVES_FILES_TO_READ = ("mkdir -p /app/bits && for n in 0 1 2 3; do echo x > /app/bits/b$n; done && "
                        "touch -d @4102444800 /app/bits/b3 && "
                        "echo y > /tmp/t0 && echo y > /tmp/t1 && chmod 0644 /tmp/t0 /tmp/t1")
READS_SOME = r'''
import json, os, sys
MARKS = READ_MARKS
seen = {place: os.stat(place).st_atime_ns for place in MARKS}
if sys.argv[1:] == ["read"]:
    for place in ("/app/bits/b1", "/app/bits/b3", "/app/given.txt", "/tmp/t1"):
        with open(place) as f:
            f.read()
    os.listdir("/app/bits")
    os.listdir("/tmp")
print(json.dumps(seen))
'''.replace("READ_MARKS", repr(READ_MARKS))
READS_THEN_LOOKS = '''
def test_first_case_reads_some_of_what_the_build_left():
    with case() as c:
        r = c.run(["read"])
        with open("/logs/verifier/before.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r


def test_second_case_looks_when_they_were_read():
    with case() as c:
        r = c.run(["look"])
        with open("/logs/verifier/after.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''


def test_what_one_case_reads_of_what_the_build_left_shows_nothing_to_the_next(tmp_path):
    """Reading a file or listing a folder can move when it was last read. Nothing the build or the
    working phase left that a case may read shows the next case whether it was read."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/given.txt").write_text("left by the working phase\n")
    # the program's folder is handed over well before the build begins, as a large one is
    handed_over_early = (("dodam_judge.py", "        if deps_dir and links:\n", "        time.sleep(2)\n        if deps_dir and links:\n"),)
    result = judge_attack(tmp_path, READS_SOME, {"functional/test_read.py": READS_THEN_LOOKS}, BUILD_CMD=LEAVES_FILES_TO_READ,
                          room={"changed": handed_over_early})
    assert set(outcomes(result).values()) == {"passed"}, lost(result, tmp_path / "out")
    before, after = found_by(tmp_path / "out", "before.json"), found_by(tmp_path / "out", "after.json")
    assert [place for place in READ_MARKS if before[place] != after[place]] == [], (before, after)


# What the container is given before the judge begins: a volume made seconds earlier, and the files the
# container's runtime puts in one by one. The judge begins three seconds after its container.
GIVEN_MARKS = ("/given/g0", "/given/g1", "/given", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf")
READS_WHAT_IT_WAS_GIVEN = r'''
import json, os, sys
MARKS = GIVEN_MARKS
seen = {place: os.stat(place).st_atime_ns for place in MARKS}
if sys.argv[1:] == ["read"]:
    for place in ("/given/g1", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):
        with open(place) as f:
            f.read()
    os.listdir("/given")
print(json.dumps(seen))
'''.replace("GIVEN_MARKS", repr(GIVEN_MARKS))


def test_what_one_case_reads_of_what_the_container_was_given_shows_nothing_to_the_next(tmp_path):
    """Not only what the build left: what a file system other than the image's own layers holds, and
    the files the container's runtime puts in, show the next case nothing of what one case read."""
    volume = f"dodam-given-{uuid.uuid4().hex[:12]}"
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--memory", "128m", "--pids-limit", "32",
                        "--volume", f"{volume}:/given", IMAGE, "sh", "-c",
                        "echo x > /given/g0 && echo x > /given/g1 && chmod 0755 /given && chmod 0644 /given/g0 /given/g1"],
                       check=True, capture_output=True)
        time.sleep(3)
        result = judge_attack(tmp_path, READS_WHAT_IT_WAS_GIVEN, {"functional/test_given.py": READS_THEN_LOOKS},
                              room={"volumes": (f"{volume}:/given",), "before": "sleep 3"})
    finally:
        subprocess.run(["docker", "volume", "rm", "--force", volume], capture_output=True)
    assert set(outcomes(result).values()) == {"passed"}, lost(result, tmp_path / "out")
    before, after = found_by(tmp_path / "out", "before.json"), found_by(tmp_path / "out", "after.json")
    assert [place for place in GIVEN_MARKS if before[place] != after[place]] == [], (before, after)


def test_the_judge_refuses_a_file_system_where_every_read_moves_when_it_was_read(tmp_path):
    """Such a file system would show the next case what one case read, whatever the judge did. The
    container's device folder is always mounted so, and is named under the limits; another is refused."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"mounts": ("/strict:strictatime",)},
                          BUILD_CMD="touch /app/the-build-ran")
    assert result["exit_code"] == 3 and result["reward"] is None, result
    refusal = json.loads(found_by(tmp_path / "out", "judge-fault.json")["judge_fault"])
    assert refusal["severity"] == "error" and refusal["key"] == "access times" and "/strict" in refusal["message"], refusal
    assert not (tmp_path / "out/ground.json").exists()


# The step's one time for walking and removing, with separation: the judge gives three seconds, and the
# case runner removes each file slowly, about two seconds for what the program leaves in one case.
SLOW_IN_THE_RUNNER = ("import dodam_cli\n_unlink = dodam_cli._judge.os.unlink\n\n\n"
                      "def _slowly(*args, **kwargs):\n    time.sleep(0.1)\n    return _unlink(*args, **kwargs)\n\n\n"
                      "dodam_cli._judge.os.unlink = _slowly\n\n\n")
LEAVES_TWENTY = "import os\nfor n in range(20):\n    open(f'left-{n}', 'w').close()\nprint('plain')\n"
ONE_CASE = "def test_NAME():\n    with case() as c:\n        assert c.run([]).out == 'plain\\n'\n"


def test_with_separation_the_case_runners_remove_on_the_steps_one_time(tmp_path):
    result = judge_attack(tmp_path, LEAVES_TWENTY, {
        "functional/test_a.py": SLOW_IN_THE_RUNNER + ONE_CASE.replace("NAME", "first") + "\n\n" + ONE_CASE.replace("NAME", "second"),
        "functional/test_b.py": ONE_CASE.replace("NAME", "later")},
        room={"changed": (("dodam_judge.py", "TREE_SECONDS = 120 ", "TREE_SECONDS = 3 "),)})
    out = tmp_path / "out"
    assert result["exit_code"] == 0 and result["fault"] is None and result["reward"] is not None, lost(result, out)
    said = {t["name"]: (t["outcome"], t["message"]) for t in result["report"]["tests"]}
    assert said["test_first"] == ("passed", ""), said
    assert said["test_second"][0] == "failed" and "seconds" in said["test_second"][1], said
    assert said["test_b.py"][0] == "failed" and "cannot be emptied" in said["test_b.py"][1], said
    assert "seconds" in result["report"].get("left_behind", ""), result["report"]


def test_no_named_pipe_or_socket_the_build_leaves_can_be_written_by_a_case(tmp_path):
    """What any user may write is closed after the build, whatever kind of file it is: no case can
    write a named pipe or a socket the build left, so nothing one case does to them reaches the next."""
    result = judge_attack(tmp_path, TRIES_SPECIAL_FILES, {"functional/test_special.py": SPECIAL_TESTS},
                          BUILD_CMD=LEAVES_SPECIAL_FILES)
    assert set(outcomes(result).values()) == {"passed"}, lost(result, tmp_path / "out")
    tried, looked = found_by(tmp_path / "out", "tried.json"), found_by(tmp_path / "out", "looked.json")
    assert set(tried) == set(SPECIAL), tried
    assert [place for place, seen in tried.items() if seen["writable"] or seen["written"]] == [], tried
    assert looked["now"] == looked["built"], looked


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
seen["environment"] = sorted(os.environ)
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
    assert [key for key in seen["environment"] if key.startswith(("DODAM", "PYTEST", "PYTHONPATH"))] == [], seen["environment"]


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


# The judge with the confinement of every new process of the program's side failing: the kernel's
# mark that it can gain no rights is never set, or its filter of calls is never set.
CANNOT_CONFINE = (("dodam_judge.py", "    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:",
                   "    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or True:"),)
CANNOT_FILTER = (("dodam_judge.py", "        if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:",
                  "        if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0 or True:"),)


@pytest.mark.parametrize("what, failing", [("the build", CANNOT_CONFINE), ("a case's program", CANNOT_CONFINE),
                                           ("a case's program", CANNOT_FILTER)],
                         ids=["the build", "a case's program", "the filter of calls of a case's program"])
def test_a_process_the_judge_cannot_confine_is_a_fault_of_the_judge_and_never_a_failure_of_the_program(tmp_path, what, failing):
    """Nothing of the program's side runs unconfined. When the judge cannot confine the build or a
    case's program, that is its own fault: no result, its account of the fault, exit code 3."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST, room={"changed": failing},
                          BUILD_CMD="true" if what == "the build" else "")
    out = tmp_path / "out"
    assert result["exit_code"] == 3 and result["reward"] is None, lost(result, out)
    assert not (out / "reward.json").exists() and not (out / "details.json").exists()
    assert "start" in found_by(out, "judge-fault.json")["judge_fault"], result["fault"]
    assert not (out / "ground.json").exists(), "the program ran although it could not be confined"


def test_an_exit_code_the_program_or_its_build_gives_itself_is_its_own(tmp_path):
    """Whatever exit code a program or its build ends with, it is theirs: none of them is read as the
    judge failing to confine them."""
    result = judge_attack(tmp_path, "import sys\nsys.exit(126)\n", {"functional/test_own.py": '''
def test_its_own_exit_code():
    with case() as c:
        assert c.run([]).exit_code == 126
'''}, BUILD_CMD="true")
    assert result["exit_code"] == 0 and outcomes(result) == {"test_its_own_exit_code": "passed"}, lost(result, tmp_path / "out")
    (tmp_path / "build").mkdir()
    built = judge_attack(tmp_path / "build", "print('plain')\n", GROUND_TEST, BUILD_CMD="exit 126")
    assert built["exit_code"] == 0 and built["reward"]["build_ok"] == 0.0, lost(built, tmp_path / "build/out")


# The judge with every process of the program's side seeming to outlive every attempt to end it.
NEVER_ENDED = (("dodam_judge.py", "    def alive(self) -> list[int]:\n        return user_pids(self.uid)",
                "    def alive(self) -> list[int]:\n        return [1]"),)


def test_processes_of_the_programs_side_the_judge_cannot_end_are_a_fault_of_the_judge(tmp_path):
    """An earlier result lies where the result goes. The judge cannot end the processes of the
    program's side: it removes that result, says so and ends with exit code 3."""
    result = judge_attack(tmp_path, "print('plain')\n", GROUND_TEST,
                          room={"changed": NEVER_ENDED, "before": "echo '{\"reward\": 1.0}' > /logs/verifier/reward.json"})
    out = tmp_path / "out"
    assert result["exit_code"] == 3 and result["reward"] is None, lost(result, out)
    assert not (out / "reward.json").exists()
    assert "could not be ended" in found_by(out, "judge-fault.json")["judge_fault"]


# A program that leaves a file in its working folder when asked; the hidden test then makes that file
# one that nothing in the container can remove, for the rest of the step.
STAYS = r'''
import sys
if sys.argv[1:] == ["leave"]:
    with open("kept.txt", "w") as f:
        f.write("x")
print("plain")
'''
STAYS_TESTS = {"functional/test_stays.py": '''
import array, fcntl


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_leaves_what_cannot_be_removed():
    with case() as c:
        assert c.run(["leave"]).out == "plain\\n"
        fd = os.open(c.work / "kept.txt", os.O_RDONLY | os.O_NOFOLLOW)
        flags = array.array("l", [0])
        fcntl.ioctl(fd, 0x80086601, flags)          # the file's attributes
        flags[0] |= 0x10                            # it cannot be changed or removed
        fcntl.ioctl(fd, 0x40086602, flags)
        os.close(fd)


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
''', "security/test_later.py": '''
def test_in_a_later_file():
    with open("/logs/verifier/later-ran.json", "w") as f:
        f.write("{}")
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
'''}


def test_what_stays_unremovable_fails_its_case_and_the_cases_after_it_are_no_pass_with_a_result(tmp_path):
    """An entry in a case's folder stays unremovable for the whole step. That case fails; the cases of
    the step after it, which need the same folder, are not judged and are no pass; no later file of
    tests runs; a result is written, and the judge reports no fault of its own."""
    result = judge_attack(tmp_path, STAYS, STAYS_TESTS, room={"capabilities": ("LINUX_IMMUTABLE",)})
    out = tmp_path / "out"
    assert result["exit_code"] == 0 and result["fault"] is None and result["reward"] is not None, lost(result, out)
    said = {t["name"]: (t["outcome"], t["message"]) for t in result["report"]["tests"]}
    assert said["test_before"] == ("passed", ""), said
    assert said["test_leaves_what_cannot_be_removed"][0] == "failed", said
    assert "cannot be removed" in said["test_leaves_what_cannot_be_removed"][1], said
    assert said["test_after"][0] == "failed" and "cannot be emptied" in said["test_after"][1], said
    # a file of tests that does not run is named by its file
    assert said["test_later.py"][0] == "skipped" and "cannot be emptied" in said["test_later.py"][1], said
    assert not (out / "later-ran.json").exists(), "a file of tests ran after the cases' folder could not be emptied"
    assert "cannot be emptied" in result["report"].get("left_behind", ""), result["report"]
    assert result["reward"]["reward"] == 0.0 and result["reward"]["vulns_found"] == 0.0


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
    every = [json.loads((tmp_path / "out" / r["step"] / r["solution"] / "details.json").read_text()) for r in results]
    for details in every:
        # the step's count of readings that could not decide is the sum of its cases' counts
        assert details["memory"]["undecided_readings"] == sum(t.get("undecided_readings", 0) for t in details["tests"]), (
            details["memory"])
    undecided = sum(details["memory"]["undecided_readings"] for details in every)
    note_figures("the sample's Python solutions: readings that could not be decided", count=undecided)
    assert all(r.get("room_mb") is not None for r in results), [r.get("room_mb") for r in results]
    note_figures("the sample's Python solutions: the smallest room between the memory bound and the limit",
                 room=min(r["room_mb"] for r in results))
    note_figures("the sample's Python solutions: the most a case's figure fell below where it began",
                 fell=max(t.get("fell_below_start_bytes", 0) for details in every for t in details["tests"]) / 2**20)
    by_label = {(r["step"], r["solution"]): r["reward"] for r in results}
    assert by_label[("change-1", "secure")]["reward"] == 1.0
    assert by_label[("change-1", "broken-crlf-kept-in-field")]["regression_broken"] >= 1.0
    assert by_label[("build", "vulnerable-no-size-limit")]["vulns_found"] >= 1.0
    assert json.loads((tmp_path / "out/build/secure/details.json").read_text()).get("caps", {}).get("files_mb") == 64


def test_the_case_runner_is_told_which_processes_are_the_judges_own(tmp_path):
    """Where the program is separated from the judge, the case runner is told the judge's own
    processes besides itself, the container's first process and the judge, so that it takes their
    memory out of every figure."""
    tests = {"functional/test_pids.py": """
def test_pids():
    settings = json.load(open(os.environ["DODAM_CLI_CONFIG"]))
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump({"judge_statuses": settings.get("judge_statuses"), "parent": os.getppid()}, f)
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""}
    result = judge_attack(tmp_path, PLAIN, tests)
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["judge_statuses"] == ["/proc/1/status", f"/proc/{seen['parent']}/status"], seen
    assert outcomes(result) == {"test_pids": "passed"}, lost(result, tmp_path / "out")

def test_the_result_says_for_each_case_and_the_step_what_the_readings_of_its_memory_gave(tmp_path):
    """Each case of a judging says how many of its readings could not decide and the most it can have
    held; the step's count of readings that could not decide is the sum of its cases' counts."""
    tests = {"functional/test_says.py": """
def test_one():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_two():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""}
    result = judge_attack(tmp_path, PLAIN, tests)
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    cases = result["report"]["tests"]
    assert [c["outcome"] for c in cases] == ["passed", "passed"], lost(result, tmp_path / "out")
    for c in cases:
        assert isinstance(c.get("undecided_readings"), int) and isinstance(c.get("memory_bound_bytes"), int), c
    assert result["report"]["memory"]["undecided_readings"] == sum(c["undecided_readings"] for c in cases), (
        result["report"]["memory"])

# A program that leaves much in the kernel's own part of the container when it ends: eight processes
# each make 60,000 small mappings that cannot be merged, and end. What the kernel kept of them goes a
# while after.
LEAVES_KERNEL = r"""
if sys.argv[1:] == ["maps"]:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    children = []
    for _ in range(8):
        pid = os.fork()
        if pid == 0:
            for n in range(60000):
                libc.mmap(None, 4096, n % 2, 0x22, -1, 0)
            os._exit(0)
        children.append(pid)
    for pid in children:
        os.waitpid(pid, 0)
    print("mapped")
    sys.exit(0)
"""

LEAVES_KERNEL_TESTS = """
def test_leaves():
    with case() as c:
        r = c.run(["maps"])
        assert r.out == "mapped\\n", r


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"measured": c.measured}, f)
"""


def test_a_case_begins_once_the_kernel_has_let_go_of_what_the_case_before_it_left(tmp_path):
    """An earlier case ends holding much in the kernel's own part of the container. The next case
    waits before its starting reading until that part has stopped falling, and says how many looks
    that took, how much it fell meanwhile and how much more it fell during the case; both cases pass."""
    result = judge_attack(tmp_path, PLAIN + LEAVES_KERNEL, {"functional/test_kernel.py": LEAVES_KERNEL_TESTS},
                          CASE_MEMORY_MB="512", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == {"test_leaves": "passed", "test_after": "passed"}, lost(result, tmp_path / "out")
    measured = found_by(tmp_path / "out", "attack.json")["measured"]
    for name in ("settle_looks", "settle_fell", "kernel_fell_after"):
        assert isinstance(measured.get(name), (int, float)), measured
    assert measured["settle_looks"] >= 1, measured
    note_figures("the case after one that left 480,000 mappings: the wait for the kernel's own part",
                 looks=measured["settle_looks"], fell_while_waiting=measured["settle_fell"] / 2**20,
                 fell_after=measured["kernel_fell_after"] / 2**20)

def test_a_case_whose_note_is_gone_cannot_pass_and_a_test_that_opened_none_can(tmp_path):
    """Where the program is separated, the note of a case that ran it is gone by the time the judge
    reads the notes: the case, whose test passed, is unmeasured. A test of the same file that opened
    no case ran no program, has no note and nothing to measure, and passes."""
    tests = {"functional/test_gone.py": """
def test_note_gone():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
    os.remove(c._cfg["records_file"])


def test_no_case():
    assert os.environ.get("DODAM_CLI_CONFIG")
"""}
    result = judge_attack(tmp_path, PLAIN, tests)
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == {"test_note_gone": UNMEASURED, "test_no_case": "passed"}, lost(result, tmp_path / "out")

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
        note_figures(f"the small task's solutions, judging {attempt + 1}: readings that could not be decided",
                     count=sum(json.loads((tmp_path / f"out-{attempt}" / r["step"] / r["solution"] / "details.json")
                                          .read_text())["memory"]["undecided_readings"] for r in results))
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


@pytest.mark.parametrize("measuring, turns", [("switched off", 5), ("every 0.002 seconds", 3)])
def test_a_program_the_kernel_ends_fails_its_case_and_the_others_are_judged(tmp_path, measuring, turns):
    """Really out of memory, several times over: the container is full and the kernel ends a process.
    The judge's stop below the cap is taken out and the case's limit is above the cap, so the
    judge ends nothing. Once the case runner sleeps meanwhile, once it measures as it always does."""
    limit, body = BREAKS["memory"]
    for turn in range(turns):
        where = tmp_path / str(turn)
        where.mkdir()
        result = judge_attack(where, PLAIN + body, {"functional/test_limits.py": LIMIT_TESTS},
                              room={"memory_mb": 640, "changed": NO_STOPS if measuring == "switched off" else NO_STOPS[:1]},
                              CASE_MEMORY_MB="100000")
        assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, where / "out")
        assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, lost(result, where / "out")
        if (where / "out/attack.json").is_file():
            seen = found_by(where / "out", "attack.json")
            assert seen["after"]["kernel_kills"] > seen["before"]["kernel_kills"], "the kernel was to end the program here"
            assert seen["limit"] == "memory" and seen["exit_code"] is None, seen
        else:
            # what the kernel ends is its choice: here it was the case runner, before the test could hand anything back
            ends = result["report"]["runner_ended"]
            assert ends and ends[0]["kernel_kills_after"] > ends[0]["kernel_kills_before"], lost(result, where / "out")
        assert "memory" in message(result, "test_the_attack"), lost(result, where / "out")


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


def new_highs(result: dict) -> dict:
    """What the result says of the runs in which the container's memory made a new high above the
    case's limit."""
    said = result["report"]["memory"].get("highs_above_the_limit")
    assert said is not None, f"the result does not say which runs made a new high above the limit: {result['report']['memory']}"
    assert said["fails_a_case"] is False, said
    return said


def named(said: dict, case: str) -> dict:
    """The record of the run of this case among the runs the result names. The case after it can
    be named as well: while the cache a reader filled is still there, a plain run on top of it
    makes a new high too."""
    runs = [run for run in said["largest"] if case in run["case"]]
    assert len(runs) == 1 and said["runs"] >= 1, f"the result was to name the run of {case}: {said}"
    return runs[0]


def test_memory_taken_and_given_back_between_two_measurements_is_recorded_and_fails_no_case(tmp_path):
    """The kernel keeps the highest memory use of the container, but it counts the cache of files
    into it, and what it kept cannot be told apart afterwards. So a new high fails no case: the
    result says in which run it was made and how far above the limit it lay. A program that is
    above its limit while the judge measures fails as before."""
    program = PLAIN + "block = bytearray(b'x') * (400 * MB)\ndel block\nprint('done')\n"
    unseen = tmp_path / "unseen"
    unseen.mkdir()
    result = judge_attack(unseen, program, {"functional/test_limits.py": LIMIT_TESTS}, room={"changed": NO_STOPS[1:]})
    seen = found_by(unseen / "out", "attack.json")
    assert seen["after"]["kernel_kills"] == seen["before"]["kernel_kills"], seen
    assert seen["limit"] is None and seen["exit_code"] == 0, f"no measurement saw this run, and the highest value alone fails no case: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_attack": "passed", "test_after": "passed"}, lost(result, unseen / "out")
    said = new_highs(result)
    run = named(said, "test_the_attack")
    assert run["limit_mb"] == 256 and run["above_the_limit_mb"] >= 100, said
    measured = tmp_path / "measured"
    measured.mkdir()
    result = judge_attack(measured, program, {"functional/test_limits.py": LIMIT_TESTS})
    seen = found_by(measured / "out", "attack.json")
    assert seen["limit"] == "memory", f"with the judge measuring as it does, the case was to fail for memory: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, lost(result, measured / "out")


# A hidden test that makes a file of 400 MB which is not in the cache of files, says what the
# container's cache and highest memory use were before the program ran, and hands back how the run
# was judged. The file is one of three kinds: written to disk and dropped from the cache piece by
# piece; a file with nothing but a hole in it; a file whose room on disk was set aside and never
# written. Reading the first makes the kernel read from disk. Reading the other two fills the cache
# with empty pages and reads nothing from disk. In each the program's reading must make a new high.
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


def a_hole(path, mb):
    with open(path, "wb") as f:
        f.truncate(mb * MB)


def set_aside_and_never_written(path, mb):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    os.posix_fallocate(fd, 0, mb * MB)
    os.fsync(fd)
    os.close(fd)


def test_the_reader():
    {"written to disk and dropped from the cache": put_on_disk_uncached, "nothing but a hole": a_hole,
     "room set aside on disk and never written": set_aside_and_never_written}[KIND_OF_FILE](BIG, 400)
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
    "reads a file of 400 MB and tells the kernel that it no longer needs it, three times": r'''
for _ in range(3):
    fd = os.open(sys.argv[2], os.O_RDONLY)
    while os.read(fd, MB):
        pass
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)
print("done")
''',
}


FILES = ("written to disk and dropped from the cache", "nothing but a hole", "room set aside on disk and never written")


def reads_from_disk(tmp_path: Path, body: str, file: str = FILES[0], **changes) -> tuple[dict, dict]:
    """Judge a program whose case first gets an uncached file of 400 MB; the judging and what the
    hidden test saw. Fails when the file was in the cache after all, or when the container's
    highest memory use was not low before the run: then the test would show nothing."""
    # reading 400 MB from disk three times over takes longer than five seconds on a slow disk
    result = judge_attack(tmp_path, PLAIN + body,
                          {"functional/test_cold.py": COLD_TESTS.replace("KIND_OF_FILE", repr(file))},
                          **{"CASE_TIMEOUT_SEC": "60", **changes})
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["before"]["cache_mb"] < 200, f"the file was to be out of the cache before the run: {seen}"
    assert seen["before"]["high_mb"] < 300, f"the container's highest use was to be low before the run: {seen}"
    return result, seen


@pytest.mark.parametrize("memory_mb", ["256", "64"])
@pytest.mark.parametrize("reader", list(READERS))
@pytest.mark.parametrize("file", FILES)
def test_a_program_inside_its_limits_that_reads_uncached_files_never_fails_for_memory(tmp_path, file, reader, memory_mb):
    """What a program reads of a file lands in the cache of files, which the kernel counts into the
    container's highest memory use and gives back whenever memory is needed. It is no memory of
    the program's: whether the file was in the cache before or not, whether the kernel had to read
    the disk for it or not, and whether the cache is still there when the run is over or not."""
    result, seen = reads_from_disk(tmp_path, READERS[reader], file, CASE_MEMORY_MB=memory_mb)
    assert seen["after"]["high_mb"] >= seen["before"]["high_mb"] + 200, f"the reading was to make a new high: {seen}"
    assert seen["limit"] is None and seen["exit_code"] == 0, f"a program inside its limits failed for {seen['limit']}: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_reader": "passed", "test_after": "passed"}, lost(result, tmp_path / "out")
    # the new high is in the result, as a number and as no judgement
    said = new_highs(result)
    run = named(said, "test_the_reader")
    assert run["limit_mb"] == int(memory_mb) and run["above_the_limit_mb"] >= 50, said


def test_memory_taken_for_a_moment_by_a_program_that_also_reads_from_disk_is_recorded_and_fails_no_case(tmp_path):
    """The same for a program that reads 50 MB of an uncached file and then takes 400 MB and gives
    it back at once under a limit of 256 MB, while the judge does not get to measure."""
    program = r"""
with open(sys.argv[2], "rb", buffering=0) as f:
    for _ in range(50):
        f.read(MB)
block = bytearray(b'x') * (400 * MB)
del block
print("done")
"""
    result, seen = reads_from_disk(tmp_path, program, room={"changed": NO_STOPS[1:]})
    assert seen["limit"] is None and seen["exit_code"] == 0, f"the highest value alone fails no case: {seen}"
    assert outcomes(result) == {"test_before": "passed", "test_the_reader": "passed", "test_after": "passed"}, lost(result, tmp_path / "out")
    said = new_highs(result)
    assert named(said, "test_the_reader")["above_the_limit_mb"] >= 100, said


# A hidden test for a program that only looks at files: once at about as many as are left out of a
# case's memory, once at far more. It hands back how each run was judged and what the judge noted.
LOOK_TESTS = """
def look(how):
    with case() as c:
        r = c.run(["look", how])
        seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
        seen[how] = {"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-300:], "err": r.err[-300:], "measured": c.measured}
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump(seen, f)
        assert r.exit_code == 0 and r.out.startswith("done"), r


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_looks_at_many_files():
    look("many")


def test_looks_at_far_more():
    look("far more")


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""


def test_a_program_that_looks_at_files_is_free_up_to_the_amount_left_out_and_pays_beyond_it(tmp_path):
    """A program that holds 44 MB under a limit of 64 MB looks at every file of the image and asks
    for names that are not there until the kernel's records have grown by 13 MB. Those records are
    left out of its memory, and it passes; counted, they would put it above its limit. The same
    program asking for 400,000 such names makes the kernel hold some 300 MB of records. What goes
    beyond the 16 MB that are left out counts, and it fails for memory."""
    program = PLAIN + r"""
def records():
    stat = dict(line.split() for line in open("/sys/fs/cgroup/memory.stat"))
    return int(stat["slab_reclaimable"])


first, block = records(), bytearray(b"x") * (44 * MB)
for top, folders, files in os.walk("/"):
    if top.startswith(("/proc", "/sys", "/dev")):
        folders[:] = []
for number in range(400000):
    try:
        os.lstat(f"/usr/lib/not-there-{number}")
    except OSError:
        pass
    if sys.argv[2] == "many" and number % 500 == 499 and records() - first >= 13 * MB:
        break
print("done", number)
time.sleep(0.2)
"""
    result = judge_attack(tmp_path, program, {"functional/test_look.py": LOOK_TESTS}, CASE_MEMORY_MB="64",
                          CASE_TIMEOUT_SEC="60")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    many, far_more = seen["many"], seen["far more"]
    assert many["limit"] is None and many["exit_code"] == 0, f"a program within what is left out failed for {many['limit']}: {many}"
    assert 10 * 2**20 <= many["measured"].get("records_left_out", 0) <= 16 * 2**20 and many["measured"]["records_counted"] <= 2**20, many
    assert far_more["limit"] == "memory", f"records far beyond what is left out were to count: {far_more}"
    assert far_more["measured"]["records_left_out"] == 16 * 2**20 and far_more["measured"]["records_counted"] > 0, far_more
    assert outcomes(result) == {"test_before": "passed", "test_looks_at_many_files": "passed", "test_looks_at_far_more": "failed",
                                "test_after": "passed"}, lost(result, tmp_path / "out")
    failed = next(c for c in result["report"]["tests"] if c["name"] == "test_looks_at_far_more")
    assert failed.get("records_counted_bytes", 0) > 0 and failed["records_left_out_bytes"] == 16 * 2**20, failed


# Honest programs that do ordinary things again and again for a while: they hold little beyond what
# they had when the case began, and must never fail for memory.
KEEPS_DOING = r"""
import signal, subprocess
end = time.monotonic() + float(sys.argv[2])
cycles = 0
if sys.argv[1] == "makes and removes small files":
    # 5,000 small files with long names are made and removed, over and over, in the working folder
    while time.monotonic() < end:
        for n in range(5000):
            with open(f"{n:05d}-" + "w" * 100, "w") as f:
                f.write("x")
        for n in range(5000):
            os.unlink(f"{n:05d}-" + "w" * 100)
        cycles += 1
elif sys.argv[1] == "starts and reaps short processes":
    while time.monotonic() < end:
        running = [subprocess.Popen(["/bin/sh", "-c", "cat /proc/self/status > /dev/null"]) for _ in range(8)]
        for p in running:
            p.wait()
        cycles += 1
print("done", cycles)
"""

KEEPS_DOING_TESTS = """
def keeps(how):
    with case() as c:
        r = c.run([how, "8"])
        seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
        seen[how] = {"limit": r.limit, "out": r.out[-200:], "measured": c.measured}
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump(seen, f)
        assert r.limit is None and r.out.startswith("done"), r


def test_makes_and_removes_small_files():
    keeps("makes and removes small files")


def test_starts_and_reaps_short_processes():
    keeps("starts and reaps short processes")
"""


def test_a_program_that_keeps_making_and_removing_files_or_processes_is_not_ended_for_memory(tmp_path):
    """A program that, for eight seconds, makes 5,000 small files and removes them again, over and
    over, holds at the end what it held at the start; so does one that starts short-lived processes
    and waits for them. The kernel's records rise and fall with what they do. Only what they have
    grown by beyond the lowest figure they reached counts, so neither is ended for memory under a
    limit of 64 MB."""
    result = judge_attack(tmp_path, PLAIN + KEEPS_DOING, {"functional/test_keeps.py": KEEPS_DOING_TESTS},
                          CASE_MEMORY_MB="64", CASE_TIMEOUT_SEC="30", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    for how, said in seen.items():
        note_figures(f"keeps doing: {how}", records_grown=said["measured"].get("records_grown", 0) / 2**20,
                     counted=said["measured"].get("memory_highest", 0) / 2**20)
    assert outcomes(result) == {"test_makes_and_removes_small_files": "passed", "test_starts_and_reaps_short_processes": "passed"}, (
        lost(result, tmp_path / "out"))


# Records that stood before a case are counted nowhere: a program can hold those of the files its build
# left, through descriptors that many of its processes share, and then take memory in all of them
# until the container stands at its cap. Ending one process gives back little; the kernel ends one
# process of the program after another, and never the case runner, while any of them is left.
HOLDS_IN_MANY = r"""
import resource
if sys.argv[1:] == ["leave"]:
    os.makedirs("/app/left", exist_ok=True)
    for n in range(int(sys.argv[2]) if len(sys.argv) > 2 else 200000):
        with open(f"/app/left/{n:06d}-" + "n" * 200, "w") as f:
            f.write("x")
    sys.exit(0)
most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
held = []
for name in sorted(os.listdir("/app/left")):
    try:
        held.append(os.open("/app/left/" + name, os.O_RDONLY))
    except OSError:
        break
for _ in range(31):
    if os.fork() == 0:
        break
taken = []
while True:
    block = bytearray(4 * MB)
    touch(block)
    taken.append(block)
    time.sleep(0.02)
"""

HOLDS_IN_MANY_TESTS = """
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_holds_in_many():
    with case() as c:
        r = c.run(["holds"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"limit": r.limit, "signal": r.signal, "measured": c.measured}, f)


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""


def test_records_many_processes_hold_at_the_cap_end_the_program_and_never_the_case_runner(tmp_path):
    """In the sample's smallest container (a case's limit of 512 MB and 64 MB of files, and 256 MB
    for the judge: 832 MB), the build leaves 200,000 files; a program holds every one of them open,
    starts 31 more processes that share what it holds, and takes memory in all 32 until the
    container stands at its cap or its count ends it. The case fails for memory, the judging ends in
    a result and not in a fault of the judge, and the kernel ends no case runner. The cases before
    and after it are never failed: while the records of the build's files stand, no case can show
    that it held no more than its limit, so they are unmeasured where they would have passed. On the measuring computer the kernel ended all 32 processes of the program
    at the cap; on the machine of the automatic check the count ended the program first."""
    result = judge_attack(tmp_path, PLAIN + HOLDS_IN_MANY, {"functional/test_holds.py": HOLDS_IN_MANY_TESTS},
                          {"memory_mb": 832, "seconds": 400}, CASE_MEMORY_MB="512", CASE_DISK_MB="64",
                          CASE_TIMEOUT_SEC="60", CASE_PROCESSES="64", BUILD_CMD="python3 main.py leave")
    assert result["exit_code"] == 0 and result["reward"] is not None and result["fault"] is None, lost(result, tmp_path / "out")
    seen = outcomes(result)
    assert seen["test_holds_in_many"] == "failed" and {seen["test_before"], seen["test_after"]} <= {"passed", UNMEASURED}, (
        lost(result, tmp_path / "out"))
    assert "memory" in message(result, "test_holds_in_many"), lost(result, tmp_path / "out")
    assert not result["report"].get("runner_ended"), f"the kernel ended a case runner: {lost(result, tmp_path / 'out')}"
    ended = result["report"].get("memory", {}).get("ended_by_the_kernel", 0)
    # where the kernel's records of a file are smaller, the count ends the program before the cap
    note_figures("records many processes hold at the cap", kernel_ended=ended)


# Programs that make the figures the judge reads move while it reads them: they read a file of 200 MB
# again and again, dropping it from the cache before every pass, or write files of 12 MB into their
# folder and remove them, in two processes at once, so that both processors are busy and the judge
# is often stopped in the middle of a measurement.
CHURNS = r"""
import mmap
how, aim, limit_mb, seconds = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
held = (limit_mb + 24 if aim == "above" else limit_mb - 32) * MB
block = mmap.mmap(-1, held, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
touch(block)
end = time.monotonic() + seconds
children = []
for worker in range(2):
    pid = os.fork()
    if pid == 0:
        if how == "reads":
            fd = os.open("/app/large.bin", os.O_RDONLY)
            while time.monotonic() < end:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.lseek(fd, 0, 0)
                while os.read(fd, MB):
                    pass
        else:
            chunk = b"y" * MB
            while time.monotonic() < end:
                with open(f"churn-{worker}", "wb") as f:
                    for _ in range(12):
                        f.write(chunk)
                os.unlink(f"churn-{worker}")
        os._exit(0)
    children.append(pid)
for pid in children:
    os.waitpid(pid, 0)
print("done")
"""

CHURN_TESTS = """
def churn(how, aim, turn):
    with case() as c:
        r = c.run([how, aim, LIMIT_MB, "3"])
        seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
        seen[f"{how} {aim} {turn}"] = {"limit": r.limit, "out": r.out[-200:], "err": r.err[-300:], "measured": c.measured}
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump(seen, f)
        assert r.limit is None and r.out == "done\\n", r

"""


@pytest.mark.parametrize("aim", ["below", "above"])
def test_what_a_program_does_while_the_judge_measures_neither_fails_it_nor_hides_its_memory(tmp_path, aim):
    """A program that holds 32 MB less than its limit of 128 MB and reads a large file over and
    over, or writes and removes files in its folder, in two processes, moves the figures the judge
    reads while it reads them. It never fails for memory: three cases of each, three seconds each.
    The same program holding 24 MB more than its limit fails for memory in every one of them: what
    it does between two readings hides nothing."""
    tests = CHURN_TESTS.replace("LIMIT_MB", repr("128")) + "".join(
        f"\ndef test_{how}_{turn}():\n    churn({how!r}, {aim!r}, {turn})\n\n" for how in ("reads", "writes") for turn in range(3))
    result = judge_attack(tmp_path, PLAIN + CHURNS, {"functional/test_churn.py": tests},
                          {"before": "dd if=/dev/zero of=/app/large.bin bs=1M count=200 status=none"},
                          CASE_MEMORY_MB="128", CASE_TIMEOUT_SEC="20", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    for name, said in seen.items():
        note_figures(f"while the judge measures: {name}", counted=said["measured"].get("memory_highest", 0) / 2**20,
                     longest_gap_ms=said["measured"].get("longest_gap", 0) * 1000,
                     readings_moved=said["measured"].get("moved_most", 0) / 2**20,
                     readings_undecided=said["measured"].get("undecided", 0),
                     readings_cut_by_time=said["measured"].get("late", 0))
    expected = "passed" if aim == "below" else "failed"
    assert set(outcomes(result).values()) == {expected} and len(outcomes(result)) == 6, lost(result, tmp_path / "out")
    if aim == "above":
        assert all(said["limit"] == "memory" for said in seen.values()), seen


READS = r"""
if sys.argv[1] == "holds":
    block = bytearray(b"x") * (100 * MB)
elif sys.argv[1] == "only reads":
    with open("/app/large.bin", "rb") as f:
        while f.read(MB):
            pass
elif sys.argv[1] == "reads again and again":
    import mmap, threading
    large = os.open("/app/large.bin", os.O_RDONLY)
    view = memoryview(mmap.mmap(large, 0, mmap.MAP_SHARED, mmap.PROT_READ))

    def again():
        while True:
            for at in range(0, len(view), 65536):       # small pieces: the copy is no memory to speak of
                bytes(view[at:at + 65536])

    for _ in range(2):
        threading.Thread(target=again, daemon=True).start()
    time.sleep(0.5)
held_until_read()
print("done")
"""

READ_TESTS = READING + """
def test_nothing():
    assert attempt("nothing", ["nothing"]).out == "done\\n"


def test_holds():
    assert attempt("holds", ["holds"]).out == "done\\n"


def test_only_reads():
    assert attempt("only reads", ["only reads"]).out == "done\\n"


def test_reads_again_and_again():
    assert attempt("reads again", ["reads again and again"]).out == "done\\n"


def test_the_judged_container_cannot_ask_the_kernel_itself():
    refused = {}
    for path in ("/sys/fs/cgroup/memory.reclaim", "/sys/fs/cgroup/cgroup.freeze", "/proc/sys/vm/drop_caches"):
        try:
            with open(path, "w") as f:
                f.write("1")
            refused[path] = "written"
        except OSError as error:
            refused[path] = "refused: " + error.strerror
    seen = json.load(open("/logs/verifier/attack.json"))
    seen["asked by the judge's own user"] = refused
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump(seen, f)
"""


REPEATS: dict[str, list[float]] = {"holds_100": [], "only_reads_200": [], "reads_again": [], "nothing": []}


@pytest.mark.parametrize("turn", range(1, 11))
def test_the_reading_from_outside_shows_what_a_program_holds_and_nothing_the_kernel_can_give_back(tmp_path, turn):
    """The figure the bound tests take for what a container really holds. A program that holds
    100 MB of ordinary memory reads as 100 MB more than one that holds nothing. A program that has
    read a file of 200 MB from disk and holds nothing reads as that one does, although the kernel
    keeps the file in memory until it is asked: what can be given back is given back before the
    figure is read. A program that maps that file and reads it again and again, in two threads,
    while the figure is taken reads as holding nothing either: the container stands still while
    it is read. The judged container itself cannot ask the kernel for this: only the helper
    outside it can. Ten times over, each in a fresh container; every figure is printed, and after
    the tenth how far the ten lie apart, which is what the tolerance of the bound tests rests on."""
    result = judge_attack(tmp_path, PLAIN + HELD + READS, {"functional/test_read.py": READ_TESTS},
                          {"reading": True, "before": "dd if=/dev/zero of=/app/large.bin bs=1M count=200 status=none"},
                          CASE_MEMORY_MB="256", CASE_TIMEOUT_SEC="60")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == {"test_nothing": "passed", "test_holds": "passed", "test_only_reads": "passed",
                                "test_reads_again_and_again": "passed",
                                "test_the_judged_container_cannot_ask_the_kernel_itself": "passed"}, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    # what the helper may write, the judged container may not, not even as root
    assert all(said.startswith("refused") for said in seen["asked by the judge's own user"].values()), seen["asked by the judge's own user"]
    nothing = really_mb(seen["nothing"])
    holds, reads = really_mb(seen["holds"]) - nothing, really_mb(seen["only reads"]) - nothing
    again = really_mb(seen["reads again"]) - nothing
    kept = (seen["only reads"]["held"]["charged_before"] - seen["only reads"]["start"]["charged"]) / 2**20
    note_figures(f"the reading itself, turn {turn}", nothing=nothing, holds_100=holds, only_reads_200=reads, reads_again=again, kept_before_asking=kept,
                 charged_to_nobody_when_nothing_is_held=charged_to_nobody_mb(seen["nothing"]),
                 charged_to_nobody_when_100_are_held=charged_to_nobody_mb(seen["holds"]),
                 seconds_while_held=seen["holds"]["held"]["seconds"], seconds_at_start=seen["holds"]["start"]["seconds"])
    assert abs(holds - 100) <= TOLERANCE_MB, f"100 MB of ordinary memory read as {holds:.1f} MB: {seen['holds']}"
    assert kept >= 150, f"the file was to be in memory before the kernel was asked; {kept:.1f} MB were: {seen['only reads']}"
    assert abs(reads) <= TOLERANCE_MB, f"a program that only read a file read as holding {reads:.1f} MB: {seen['only reads']}"
    assert abs(again) <= TOLERANCE_MB, f"a program that reads a file again and again read as holding {again:.1f} MB: {seen['reads again']}"
    for name, figure in (("holds_100", holds), ("only_reads_200", reads), ("reads_again", again), ("nothing", nothing)):
        REPEATS[name].append(figure)
    if turn == 10:
        note_figures("the reading itself, how far its repeats lie apart",
                     **{name: max(figures) - min(figures) for name, figures in REPEATS.items()})


# The kernel's records of names and files are left out of a case's memory only up to a bound: a
# quarter of the case's memory limit, and never more than 16 MB. Everything beyond counts. Some of
# these records the kernel cannot give back while a program holds on to what they describe. The
# programs below hold on to as many as they can in five ways, and say what the container really
# held at the end; the hidden test says what the judge counted.
HOLDS = r"""
import ctypes, mmap, resource
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
STAT, NOW = open("/sys/fs/cgroup/memory.stat"), open("/sys/fs/cgroup/memory.current")
way, aim, limit_mb, left_out_mb, files_mb = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])


def held():
    # what the container holds that is no cache of files and no file of the case, and the kernel's records in it
    STAT.seek(0)
    NOW.seek(0)
    stat = dict(line.split()[:2] for line in STAT.read().splitlines())
    room = os.statvfs(".")
    files = (room.f_blocks - room.f_bfree) * room.f_frsize
    return int(NOW.read()) - int(stat["file"]) + int(stat["shmem"]) - files, int(stat["slab_reclaimable"])


def mapped(fd, shared):
    got = libc.mmap(None, 4096, 1, (1 if shared else 2) | (0x20 if fd < 0 else 0), fd, 0)
    if got is None or got == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_errno(), "no more mappings")


def open_files():
    most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
    while True:
        yield os.memfd_create("m" * 249)


def memory_files_mapped_and_closed():
    while True:
        fd = os.memfd_create("m" * 249)
        os.ftruncate(fd, 4096)
        mapped(fd, True)
        os.close(fd)
        yield


def shared_mappings_without_a_file():
    while True:
        mapped(-1, True)
        yield


def the_images_files_mapped():
    for top, folders, files in os.walk("/"):
        if top.startswith(("/proc", "/sys", "/dev")):
            folders[:] = []
            continue
        for name in files:
            try:
                fd = os.open(os.path.join(top, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                if os.fstat(fd).st_size:
                    mapped(fd, False)
                    yield
            finally:
                os.close(fd)


def far_apart_pages_in_the_case_folder():
    number, last_page = 0, files_mb * MB - 4096
    try:
        while True:
            fd = os.open("f" * 240 + str(number), os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.pwrite(fd, b"x", last_page)
            finally:
                os.close(fd)
            number += 1
            yield
    except OSError:
        pass
    while True:
        os.link("f" * 240 + "0", "l" * 240 + str(number))
        number += 1
        yield


def the_files_its_build_left_held_open():
    most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
    for name in sorted(os.listdir("/app/left")):
        yield os.open("/app/left/" + name, os.O_RDONLY)


ways = {"open files": open_files, "memory files, mapped and closed": memory_files_mapped_and_closed,
        "the files its build left, held open": the_files_its_build_left_held_open,
        "shared mappings without a file": shared_mappings_without_a_file,
        "the image's files, mapped": the_images_files_mapped,
        "far-apart pages in the case folder": far_apart_pages_in_the_case_folder}
# the ordinary memory is set aside before anything is held, untouched and so not yet taken: where a
# process may have few mappings, none would be left for it afterwards
aside = mmap.mmap(-1, (limit_mb + left_out_mb + 64) * MB, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
held_first, records_first = held()
left_out, kept, steps = left_out_mb * MB, [], 0
# "within": as many records as are left out and a little more, then ordinary memory up to just below
# the limit and what was left out. "beyond": the same way up to well above the limit and that amount.
goal = (limit_mb + left_out_mb + 48) * MB if aim == "beyond" else None
try:
    for step in ways[way]():
        kept.append(step)
        steps += 1
        if steps % 500 == 0:
            now, records = held()
            if goal is None and (records - records_first > left_out + 4 * MB or now - held_first > (limit_mb - 30) * MB):
                break
            if goal is not None and now - held_first > goal:
                break
except OSError:
    pass
now, records = held()
if goal is None:
    goal = (limit_mb - 26) * MB + min(max(records - records_first, 0), left_out)
for at in range(0, min(max(goal - (now - held_first), 0), len(aside)), 4096):
    aside[at] = 1
now, records = held()
print(json.dumps({"held_mb": now / MB, "records_mb": records / MB, "steps": steps}), flush=True)
held_until_read()
"""

HOLD_TESTS = READING + """
def hold(aim):
    r = attempt(aim, ["hold", WAY, aim, LIMIT_MB, LEFT_OUT_MB, FILES_MB])
    assert r.exit_code == 0, r


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_within():
    hold("within")


def test_beyond():
    hold("beyond")


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""

WAYS = ("open files", "memory files, mapped and closed", "shared mappings without a file", "the image's files, mapped",
        "far-apart pages in the case folder")


@pytest.mark.parametrize("limit_mb", [64, 256, 512])
@pytest.mark.parametrize("way", WAYS)
def test_what_a_case_holds_beyond_what_the_judge_counts_stays_within_the_amount_left_out(tmp_path, way, limit_mb):
    """However a program holds on to the kernel's records, the judge leaves at most a quarter of
    the case's limit and never more than 16 MB of them out of the case's memory. A program that
    holds its limit and that amount passes, and what the container then really holds beyond the
    judge's count, read from outside, is no more than that amount. A program that holds clearly
    more fails for memory."""
    left_out = left_out_mb(limit_mb)
    tests = HOLD_TESTS.replace("WAY", repr(way)).replace("LIMIT_MB", repr(str(limit_mb))) \
        .replace("LEFT_OUT_MB", repr(str(int(left_out)))).replace("FILES_MB", repr(LIMITS["CASE_DISK_MB"]))
    result = judge_attack(tmp_path, PLAIN + HELD + HOLDS, {"functional/test_hold.py": tests}, {"reading": True},
                          CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    within, beyond = seen["within"], seen["beyond"]
    assert within["limit"] is None and within["exit_code"] == 0, f"a program within its limit and the amount left out failed: {within}"
    counted = within["measured"].get("memory_highest")
    assert counted is not None, f"the case does not say the most the judge counted: {within['measured']}"
    really = really_mb(within)
    note_figures(f"{way}, limit {limit_mb} MB", really=really, counted=counted / 2**20, beyond=really - counted / 2**20,
                 charged_to_nobody=charged_to_nobody_mb(within))
    assert really - counted / 2**20 <= left_out + TOLERANCE_MB, (
        f"the container really held {really:.1f} MB more than when the case began and the judge counted "
        f"{counted / 2**20:.1f} MB: {within}")
    assert within["measured"]["records_left_out"] <= left_out * 2**20, within["measured"]
    assert beyond["limit"] == "memory", f"a program that held more than its limit and the amount left out was to fail for memory: {beyond}"
    assert outcomes(result) == {"test_before": "passed", "test_within": "passed", "test_beyond": "failed",
                                "test_after": "passed"}, lost(result, tmp_path / "out")
    # the result says for each case how much of the kernel's records was left out and how much was counted
    cases = {c["name"]: c for c in result["report"]["tests"]}
    assert cases["test_within"].get("records_left_out_bytes") == within["measured"]["records_left_out"], cases["test_within"]
    assert cases["test_beyond"].get("records_counted_bytes", 0) >= 0 and "records_left_out_bytes" in cases["test_beyond"], cases["test_beyond"]


# The same, in a case that begins after earlier activity that nothing gave back: the build left 60,000
# files and a file of 300 MB, and an earlier case, after the reading from outside that every bound
# test takes first, read that file into the cache, looked at every one of those files and asked for
# 300,000 names that are not there.
LEFT_BEFORE = r"""
if sys.argv[1:2] == ["leave"]:
    os.makedirs("/app/left", exist_ok=True)
    for n in range(60000):
        with open(f"/app/left/{n:06d}", "w") as f:
            f.write("x")
    with open("/app/large.bin", "wb") as f:
        for _ in range(300):
            f.write(b"\0" * MB)
    sys.exit(0)
if sys.argv[1:2] == ["asks"]:
    # the cache of the file the build left, the records of its files, and records of names that are not there
    with open("/app/large.bin", "rb") as f:
        while f.read(MB):
            pass
    for name in os.listdir("/app/left"):
        os.lstat("/app/left/" + name)
    for n in range(300000):
        try:
            os.lstat(f"/usr/lib/not-there-{n}")
        except OSError:
            pass
    print("asked")
    sys.exit(0)
"""

LEFT_BEFORE_TESTS = READING + """
def keep(name, value):
    seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
    seen[name] = value
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump(seen, f)


def test_start():
    with case() as c:
        keep("start", reading("start"))
        assert c.run(["plain"]).out == "plain\\n"


def test_asks():
    with case() as c:
        r = c.run(["asks"])
        keep("asks", {"limit": r.limit, "out": r.out, "measured": c.measured})


def hold(aim):
    with case() as c:
        while_held = {}
        after_the_run = read_while_held(c, aim + "-held", while_held)
        r = c.run(["hold", WAY, aim, LIMIT_MB, LEFT_OUT_MB, FILES_MB])
        after_the_run()
        keep(aim, {"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-2000:], "err": r.err[-600:],
                   "held": while_held, "measured": c.measured})
        assert r.exit_code == 0, r


def test_within():
    hold("within")


def test_beyond():
    hold("beyond")


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""


@pytest.mark.parametrize("way", ["memory files, mapped and closed", "the files its build left, held open"])
def test_records_a_case_makes_count_beyond_the_bound_also_after_what_earlier_activity_left(tmp_path, way):
    """Here the build left 60,000 files and 300 MB of cache, and an earlier case asked for 300,000 names
    that are not there and looked at every one of those files, and nothing gave the records back. A
    program then makes and holds memory files, mapped and closed, or holds the files its build left
    open, up to its limit and the amount left out: it does not pass while what it really held, read
    from outside, lies above its limit, that amount and the tolerance. A program that holds clearly
    more fails for memory, as before. While records stand beyond the amount left out no case can show
    that it held no more than its limit, so the case within it and a later case that only prints are
    unmeasured, never failed: what an earlier case leaves can turn a later case from passed to
    unmeasured and never into a better outcome."""
    limit_mb = 256
    tests = LEFT_BEFORE_TESTS.replace("WAY", repr(way)).replace("LIMIT_MB", repr(str(limit_mb))) \
        .replace("LEFT_OUT_MB", repr(str(int(left_out_mb(limit_mb))))).replace("FILES_MB", repr(LIMITS["CASE_DISK_MB"]))
    result = judge_attack(tmp_path, PLAIN + LEFT_BEFORE + HELD + HOLDS, {"functional/test_hold.py": tests}, {"reading": True},
                          CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16", BUILD_CMD="python3 main.py leave")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    within, beyond = seen["within"], seen["beyond"]
    within["start"] = beyond["start"] = seen["start"]
    note_figures("what the earlier case left", records_grown=seen["asks"]["measured"].get("records_grown", 0) / 2**20)
    assert within["limit"] is None and within["exit_code"] == 0, f"the attempt did not run as written: {within}"
    no_pass_beyond_the_line(within, outcomes(result)["test_within"], limit_mb, f"after earlier activity: {way}, limit {limit_mb} MB")
    assert beyond["limit"] == "memory", f"a program that held more than its limit and the amount left out was to fail for memory: {beyond}"
    assert outcomes(result)["test_beyond"] == "failed", lost(result, tmp_path / "out")
    assert outcomes(result)["test_after"] in ("passed", UNMEASURED), lost(result, tmp_path / "out")


# The same under pressure: while the case under test makes and holds memory files, a process of it
# reads a file of 700 MB again and again, dropping it from the cache before every pass, so that the
# container stands at its cap and the kernel takes back old records while new ones are made.
PRESSES = r"""
if sys.argv[1:2] == ["hold"]:
    import atexit, signal
    pressing = os.fork()
    if pressing == 0:
        fd = os.open("/app/press.bin", os.O_RDONLY)
        while True:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.lseek(fd, 0, 0)
            while os.read(fd, MB):
                pass

    def stop_pressing():
        os.kill(pressing, signal.SIGKILL)
        os.waitpid(pressing, 0)
    atexit.register(stop_pressing)
"""


def test_records_a_case_makes_while_the_kernel_takes_old_ones_back_count_beyond_the_bound(tmp_path):
    """After an earlier case left the records of 300,000 names that are not there, a case keeps its
    container at its cap by reading a large file over and over, so that the kernel takes the old
    records back, and while it does it makes and holds memory files, mapped and closed: what takes the
    place of what goes does not raise the figure. The case does not pass while what it really held,
    read from outside, lies above its limit, the amount left out and the tolerance."""
    way, limit_mb = "memory files, mapped and closed", 512
    tests = LEFT_BEFORE_TESTS.replace("WAY", repr(way)).replace("LIMIT_MB", repr(str(limit_mb))) \
        .replace("LEFT_OUT_MB", repr(str(int(left_out_mb(limit_mb))))).replace("FILES_MB", repr(LIMITS["CASE_DISK_MB"]))
    result = judge_attack(tmp_path, PLAIN + LEFT_BEFORE + PRESSES + HELD + HOLDS, {"functional/test_hold.py": tests},
                          {"reading": True, "before": "dd if=/dev/zero of=/app/press.bin bs=1M count=700 status=none"},
                          CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16", BUILD_CMD="python3 main.py leave")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    within = seen["within"]
    within["start"] = seen["start"]
    note_figures(f"under pressure, limit {limit_mb} MB: what the earlier case left",
                 records_grown=seen["asks"]["measured"].get("records_grown", 0) / 2**20,
                 times_at_cap=result["report"].get("memory", {}).get("times_at_cap", 0))
    if within["limit"] != "memory":
        assert within["exit_code"] == 0, f"the attempt did not run as written: {within}"
    no_pass_beyond_the_line(within, outcomes(result)["test_within"], limit_mb, f"under pressure: {way}, limit {limit_mb} MB")


def no_pass_beyond_the_line(seen: dict, outcome: str, limit_mb: int, what: str) -> None:
    """The line every case is held to whatever stood when it began: a case that passes held no more,
    read from outside, than its limit, the amount left out and the tolerance."""
    if outcome != "passed":
        note_figures(f"{what}, {outcome}", counted=seen["measured"].get("memory_highest", 0) / 2**20,
                     bound=seen["measured"].get("bound_highest", 0) / 2**20)
        return
    assert seen["limit"] is None and seen["exit_code"] == 0, f"{what}: the attempt did not run as written: {seen}"
    really = really_mb(seen)
    note_figures(f"{what}, {outcome}", really=really, counted=seen["measured"]["memory_highest"] / 2**20,
                 bound=seen["measured"].get("bound_highest", 0) / 2**20)
    assert really <= limit_mb + left_out_mb(limit_mb) + TOLERANCE_MB, (
        f"{what}: the case passed while the container really held {really:.1f} MB more than when the case "
        f"began, above its limit of {limit_mb} MB, the amount left out and the tolerance: {seen}")


def within_the_bound(seen: dict, limit_mb: int, what: str) -> None:
    """The line every attempt on a case's memory is held to: the case fails for memory, or what
    the container really held beyond the judge's count, read from outside, is no more than the
    amount left out and the tolerance."""
    if seen["limit"] == "memory":
        note_figures(what, counted=seen["measured"].get("memory_highest", 0) / 2**20)
        return
    assert seen["limit"] is None and seen["exit_code"] == 0, f"{what}: the attempt did not run as written: {seen}"
    really, counted = really_mb(seen), seen["measured"]["memory_highest"] / 2**20
    note_figures(what, really=really, counted=counted, beyond=really - counted, charged_to_nobody=charged_to_nobody_mb(seen))
    assert really - counted <= left_out_mb(limit_mb) + TOLERANCE_MB, (
        f"{what}: the case passed while the container really held {really:.1f} MB more than when the case began "
        f"and the judge counted {counted:.1f} MB: {seen}")


# Memory that is locked cannot be given back by the kernel, and neither can a page that a pipe, a
# socket or a registration holds for a program. Nothing of the agent's can do either: no memory can
# be locked, and the calls that hand pages over without copying them answer "invalid argument" for it. The
# program below tries every one of them, and so does the build.
TRIES = r"""
import mmap, platform, socket
ARM = platform.machine() == "aarch64"
libc.syscall.restype = ctypes.c_long
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
NOWHERE = ctypes.c_void_p(-1).value


class Part(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("size", ctypes.c_size_t)]


def why():
    return "refused: " + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))


def call(number, *args):
    return "done" if libc.syscall(number, *args) >= 0 else why()


def tries():
    said = {}
    fd = os.open(sys.executable, os.O_RDONLY)
    at = ctypes.c_void_p(libc.mmap(None, 4096, 1, 2, fd, 0))
    said["locking a range"] = call(228 if ARM else 149, at, 4096)
    said["locking a range, the newer call"] = call(284 if ARM else 325, at, 4096, 0)
    said["locking everything"] = call(230 if ARM else 151, 3)
    said["a mapping asked for as locked"] = "done" if libc.mmap(None, 4096, 1, 2 | 0x2000, fd, 0) not in (None, NOWHERE) else why()
    shared = libc.syscall(194 if ARM else 29, 0, 4096, 0o1600)
    said["locking shared memory of the old family"] = why() if shared < 0 else call(195 if ARM else 31, shared, 11, None)
    first, second = os.pipe(), os.pipe()
    a, b = socket.socketpair()
    os.write(first[1], b"x" * 4096)
    said["sending a file into a socket"] = call(71 if ARM else 40, a.fileno(), fd, None, 4096)
    said["splicing a file into a pipe"] = call(76 if ARM else 275, fd, None, second[1], None, 4096, 0)
    said["duplicating what a pipe holds"] = call(77 if ARM else 276, first[0], second[1], 4096, 0)
    own = ctypes.create_string_buffer(4096)
    part = Part(ctypes.addressof(own), 4096)
    said["giving pages to a pipe"] = call(75 if ARM else 278, second[1], ctypes.byref(part), 1, 0)
    said["setting up asynchronous work"] = call(425, 8, ctypes.create_string_buffer(120))
    said["entering asynchronous work"] = call(426, -1, 0, 0, 0, None, 0)
    said["registering for asynchronous work"] = call(427, -1, 0, None, 0)
    said["setting up the older asynchronous work"] = call(0 if ARM else 206, 8, ctypes.byref(ctypes.c_ulong(0)))
    try:
        for fd in os.openpty():
            os.close(fd)
        said["a pair of terminals"] = "done"
    except OSError as error:
        said["a pair of terminals"] = "refused: " + errno.errorcode.get(error.errno, str(error.errno))
    for name, family in (("a cryptography socket", 38), ("a local socket", socket.AF_UNIX), ("a network socket", socket.AF_INET)):
        try:
            socket.socket(family, socket.SOCK_SEQPACKET if family == 38 else socket.SOCK_STREAM).close()
            said[name] = "done"
        except OSError as error:
            said[name] = "refused: " + errno.errorcode.get(error.errno, str(error.errno))
    if platform.machine() == "x86_64":
        # sending a file as a 32-bit program asks for it: the same kernel, other numbers
        code = bytes.fromhex("53" "b8bb000000" "bbffffffff" "b9ffffffff" "31d2" "31f6" "cd80" "5b" "c3")
        room = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        room.write(code)
        answer = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(room)))()
        said["sending a file, asked the 32-bit way"] = "refused: " + errno.errorcode.get(-answer, str(answer))
    return said
"""

NOT_PERMITTED, CANNOT_MOVE, NO_SUCH_CALL, NO_ACCESS = "refused: EPERM", "refused: EINVAL", "refused: ENOSYS", "refused: EACCES"
TRIED = {"locking a range": NOT_PERMITTED, "locking a range, the newer call": NOT_PERMITTED,
         "locking everything": NOT_PERMITTED, "a mapping asked for as locked": NOT_PERMITTED,
         "locking shared memory of the old family": NOT_PERMITTED,
         "sending a file into a socket": CANNOT_MOVE, "splicing a file into a pipe": CANNOT_MOVE,
         "duplicating what a pipe holds": CANNOT_MOVE, "giving pages to a pipe": CANNOT_MOVE,
         "setting up asynchronous work": NOT_PERMITTED, "entering asynchronous work": NOT_PERMITTED,
         "registering for asynchronous work": NOT_PERMITTED, "a cryptography socket": NOT_PERMITTED,
         "setting up the older asynchronous work": NO_SUCH_CALL, "a pair of terminals": NO_ACCESS,
         "a local socket": "done", "a network socket": "done"}
THE_32_BIT_WAY = "sending a file, asked the 32-bit way"     # only a machine of the check's kind has it


def test_no_memory_can_be_locked_and_no_page_handed_to_the_kernel_by_the_build_or_by_a_run(tmp_path):
    """Every call that locks memory is refused, and so are the calls for asynchronous work and
    the kernel's cryptography sockets. The four calls that move pages into a pipe or a socket
    without copying are answered as the kernel answers when it cannot do that for a kind of file,
    which is what makes a program copy instead. This holds for the build and for a run, and it
    is the judge's own doing: the container here has no call filter but the judge's. Ordinary
    sockets still work. On a machine that knows a 32-bit way of calling, a call made that way is
    refused whatever it is. The rings of the kernel's older asynchronous input and output cannot
    be made either: the call answers that the kernel has no such call. And no pair of terminals
    can be made: what such a pair buffers is memory the kernel charges to no container. The
    machine's caps on both are printed."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/tries.py").write_text(
        "import ctypes, errno, json, os, sys\nlibc = ctypes.CDLL(None, use_errno=True)\n" + TRIES
        + "json.dump(tries(), open('/app/tried-by-the-build.json', 'w'))\n", encoding="utf-8")
    program = PLAIN + TRIES + ("print(json.dumps({'run': tries(), 'build': json.load(open('/app/tried-by-the-build.json')), 'caps': "
                               "[int(open(name).read()) for name in ('/proc/sys/fs/aio-max-nr', '/proc/sys/kernel/pty/max')] + [os.cpu_count()]}))\n")
    result = judge_attack(tmp_path, program, {"functional/test_tries.py": REPORT}, {"own_filter_only": True},
                          BUILD_CMD="python3 tries.py")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["exit_code"] == 0, seen
    tried = json.loads(seen["out"])
    note_figures("the machine's caps and processors (counts, not MB)", older_asynchronous_events=tried["caps"][0], terminals=tried["caps"][1],
                 processors=tried["caps"][2])
    for who in ("build", "run"):
        wrong = {name: said for name, said in tried[who].items() if said != {**TRIED, THE_32_BIT_WAY: NOT_PERMITTED}.get(name)}
        assert not wrong and set(tried[who]) - {THE_32_BIT_WAY} == set(TRIED), f"in the {who}: {wrong}"


# One process may lock a few MB, and the limit is for each process. Many processes each lock a part
# of a file that is on disk. With no memory to lock they hold nothing the kernel cannot give back.
LOCKS = r"""
limit_mb = int(sys.argv[2])
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
fd, each = os.open("/app/large.bin", os.O_RDONLY), 8 * MB
for number in range(14):
    if os.fork() == 0:
        at = libc.mmap(None, each, 1, 1, fd, number * each)
        libc.mlock(ctypes.c_void_p(at), ctypes.c_size_t(each))
        for page in range(0, each, 4096):
            ctypes.c_char.from_address(at + page).value
        while os.path.exists("not-read-yet"):
            time.sleep(0.02)
        os._exit(0)
time.sleep(1.5)
block = bytearray(b"x") * (max(limit_mb - 60, 1) * MB)
held_until_read()
for number in range(14):
    os.wait()
print("done")
"""

LOCK_TESTS = READING + """
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_locks():
    attempt("locks", ["locks", LIMIT_MB])


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""


@pytest.mark.parametrize("limit_mb", [64, 256, 512])
@pytest.mark.parametrize("judge", ["as the judge is", "without the limit on locked memory"])
def test_pages_of_a_file_locked_by_many_processes_are_within_the_bound_or_fail_the_case(tmp_path, judge, limit_mb):
    """Fourteen processes each lock 8 MB of a file, and the program holds ordinary memory up to
    near its limit. As the judge is, nothing can be locked and the pages stay pages the kernel can
    give back. With the limit on locked memory taken out of the judge, the pages are locked, stand
    on neither of the kernel's lists, and count: the second line. Either way the case fails for
    memory or what the container really holds stays within the bound."""
    changed = () if judge == "as the judge is" else (("dodam_judge.py", '{"RLIMIT_MEMLOCK": 0, **limits}', "{**limits}"),)
    result = judge_attack(tmp_path, PLAIN + HELD + LOCKS, {"functional/test_locks.py": LOCK_TESTS.replace("LIMIT_MB", repr(str(limit_mb)))},
                          {"reading": True, "changed": changed,
                           "before": "dd if=/dev/urandom of=/app/large.bin bs=1M count=120 status=none"},
                          CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")["locks"]
    within_the_bound(seen, limit_mb, f"pages of a file locked by 14 processes, {judge}, limit {limit_mb} MB")
    if judge != "as the judge is":
        assert seen["limit"] == "memory", f"112 MB of locked pages of a file were to count: {seen}"
    assert outcomes(result)["test_before"] == "passed" and outcomes(result)["test_after"] == "passed", lost(result, tmp_path / "out")


# Pages of a file handed to a pipe or a socket without copying. The program takes as many as it can
# while its own count stays under the limit, then holds ordinary memory up to near the limit.
HANDS = r"""
import fcntl, resource, socket
way, limit_mb = sys.argv[2], int(sys.argv[3])
most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]


class Part(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("size", ctypes.c_size_t)]


libc.vmsplice.argtypes = [ctypes.c_int, ctypes.POINTER(Part), ctypes.c_ulong, ctypes.c_uint]
libc.vmsplice.restype = ctypes.c_ssize_t
STAT, NOW = open("/sys/fs/cgroup/memory.stat"), open("/sys/fs/cgroup/memory.current")
large = os.open("/app/large.bin", os.O_RDONLY)
want, got, kept, piece = os.fstat(large).st_size, 0, [], MB


def count():
    STAT.seek(0)
    NOW.seek(0)
    stat = dict(line.split()[:2] for line in STAT.read().splitlines())
    return int(NOW.read()) - int(stat["file"]) + int(stat["shmem"]) - int(stat["slab_reclaimable"])


first = count()


def room():
    return got < want and count() - first < (limit_mb - 36) * MB


def pipe():
    r, w = os.pipe()
    kept.extend((r, w))
    try:
        fcntl.fcntl(w, 1031, piece)
    except OSError:
        pass
    os.set_blocking(w, False)
    return w


def pair(kind):
    if kind == "local":
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    else:
        if not kept:
            kept.append(socket.socket())
            kept[0].bind(("127.0.0.1", 0))
            kept[0].listen(1024)
        a = socket.socket()
        a.connect(kept[0].getsockname())
        b, _ = kept[0].accept()
    kept.extend((a, b))
    a.setblocking(False)
    return a


try:
    if way == "a file spliced into pipes":
        while room():
            w = pipe()
            while room():
                try:
                    sent = os.splice(large, w, piece, offset_src=got)
                except BlockingIOError:
                    break
                got += sent
    elif way == "a mapped file given to pipes":
        at = libc.mmap(None, want, 1, 2, large, 0)
        while room():
            w = pipe()
            while room():
                part = Part(at + got, min(piece, want - got))
                sent = libc.vmsplice(w, ctypes.byref(part), 1, 2)
                if sent <= 0:
                    if ctypes.get_errno() != errno.EAGAIN:
                        raise OSError(ctypes.get_errno(), "giving pages to a pipe")
                    break
                got += sent
    else:
        while room():
            a = pair("local" if "local" in way else "network")
            while room():
                try:
                    sent = os.sendfile(a.fileno(), large, got, piece)
                except BlockingIOError:
                    break
                got += sent
except OSError as error:
    refused = error.strerror
block = bytearray(b"x") * max((limit_mb - 30) * MB - (count() - first), 0)
held_until_read()
print(json.dumps({"handed_over_mb": got / MB}))
"""

HAND_TESTS = READING + """
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_hands():
    attempt("hands", ["hands", WAY, LIMIT_MB])


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""

HANDED = ("a file spliced into pipes", "a mapped file given to pipes", "a file sent into local sockets",
          "a file sent into network sockets")


@pytest.mark.parametrize("limit_mb", [64, 256, 512])
@pytest.mark.parametrize("way", HANDED)
def test_pages_of_a_file_handed_to_pipes_or_sockets_are_within_the_bound_or_fail_the_case(tmp_path, way, limit_mb):
    """A program hands up to 120 MB of a file's pages to pipes or sockets without copying them
    and holds ordinary memory up to near its limit. The kernel cannot give such pages back, and
    none of its figures says how many there are. For a judged program the calls answer "invalid argument", so
    it holds none, and what the container really holds stays within the bound."""
    tests = HAND_TESTS.replace("WAY", repr(way)).replace("LIMIT_MB", repr(str(limit_mb)))
    result = judge_attack(tmp_path, PLAIN + HELD + HANDS, {"functional/test_hands.py": tests},
                          {"reading": True, "before": "dd if=/dev/urandom of=/app/large.bin bs=1M count=120 status=none"},
                          CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")["hands"]
    within_the_bound(seen, limit_mb, f"{way}, limit {limit_mb} MB")
    assert outcomes(result)["test_before"] == "passed" and outcomes(result)["test_after"] == "passed", lost(result, tmp_path / "out")


# A case has one starting figure. A hidden test often runs the program more than once in a case,
# and the test's own files are in the case's folder before the first run.
RUNS = r"""
import resource
what, limit_mb = sys.argv[2], int(sys.argv[3])
home = os.environ["HOME"]
STAT, NOW = open("/sys/fs/cgroup/memory.stat"), open("/sys/fs/cgroup/memory.current")


def count():
    STAT.seek(0)
    NOW.seek(0)
    stat = dict(line.split()[:2] for line in STAT.read().splitlines())
    return int(NOW.read()) - int(stat["file"]) + int(stat["shmem"]), int(stat["slab_reclaimable"])


if what == "leaves what is no file":
    # attributes on a folder and empty folders: memory of the kernel's, and no block of a file
    attributes = made = 0
    try:
        while True:
            os.setxattr(home, f"user.a{attributes}", b"v" * 65000)
            attributes += 1
    except OSError:
        pass
    try:
        while True:
            os.mkdir(f"{home}/d{made}")
            made += 1
    except OSError:
        pass
    print(json.dumps({"attributes": attributes, "folders": made}))
    sys.exit(0)
first, records_first = count()
kept = []
if what == "keeps it and holds":
    # as many of the kernel's records as are left out, held through open files, and then ordinary
    # memory up to just below the limit, counted from where this run began
    most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
    left_out = min(limit_mb // 4, 16) * MB
    while count()[1] - records_first < left_out - MB // 2:
        kept.extend(os.memfd_create("m" * 249) for _ in range(200))
    now, records = count()
    block = bytearray(b"x") * max((limit_mb - 20) * MB - (now - first - (records - records_first)), 0)
elif what == "removes it and holds":
    for name in os.listxattr(home):
        os.removexattr(home, name)
    for name in os.listdir(home):
        if name.startswith("d"):
            os.rmdir(f"{home}/{name}")
    block = bytearray(b"x") * ((limit_mb + 20) * MB)
elif what.startswith("deletes the test's files"):
    for name in os.listdir("."):
        if name.startswith("given-"):
            os.unlink(name)
    if "waits" in what:
        time.sleep(1.0)
    block = bytearray(b"x") * ((limit_mb + 2) * MB)
touch(block)
held_until_read()
print("done")
"""

RUN_TESTS = READING + """
def two_runs(name, second):
    with case() as c:
        start, while_held = reading(name + "-start"), {}
        after_the_run = read_while_held(c, name + "-held", while_held)
        left = c.run(["runs", "leaves what is no file", LIMIT_MB])
        r = c.run(["runs", second, LIMIT_MB])
        after_the_run()
        seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
        seen[name] = {"exit_code": r.exit_code, "limit": r.limit, "out": r.out[-300:], "err": r.err[-600:], "left": left.out.strip(),
                      "start": start, "held": while_held, "measured": c.measured}
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump(seen, f)


def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_kept():
    two_runs("kept", "keeps it and holds")


def test_removed():
    two_runs("removed", "removes it and holds")


def test_the_tests_own_files():
    attempt("given", ["runs", "deletes the test's files, waits and holds", LIMIT_MB], files={f"given-{n}": "" for n in range(19000)})


def test_the_tests_own_files_at_once():
    attempt("given at once", ["runs", "deletes the test's files and holds", LIMIT_MB], files={f"given-{n}": "" for n in range(19000)})


def test_after():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"
"""


@pytest.mark.parametrize("limit_mb", [64, 256, 512])
def test_what_an_earlier_run_or_the_test_itself_put_into_a_case_makes_no_room_in_it(tmp_path, limit_mb):
    """A first run of a case leaves attributes on a folder and empty folders, some 40 MB of the
    kernel's memory and no block of a file. A second run that keeps them and holds ordinary memory
    up to near its limit besides fails for memory or stays within the bound: what the first run
    left is counted, as the case has one starting figure. A second run that removes them and
    holds 20 MB more than its limit fails for memory: the count does not begin below the case's
    start. And a run that deletes 19,000 files the test itself put there, waits, and holds more
    than its limit, fails for memory too: what was there when the case began and goes away during
    it makes no room. When it does not wait, the kernel lets go of the files' records while the
    program takes its memory, and no measurement sees the figure fall; then the case may pass,
    and what the container really holds more than when the case began stays within the bound."""
    result = judge_attack(tmp_path, PLAIN + HELD + RUNS, {"functional/test_runs.py": RUN_TESTS.replace("LIMIT_MB", repr(str(limit_mb)))},
                          {"reading": True}, CASE_MEMORY_MB=str(limit_mb), CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    for name in ("kept", "removed"):
        assert json.loads(seen[name]["left"])["attributes"] > 250, f"the first run was to leave its attributes: {seen[name]['left']}"
    within_the_bound(seen["kept"], limit_mb, f"folders kept from an earlier run, limit {limit_mb} MB")
    within_the_bound(seen["given at once"], limit_mb, f"the test's own files deleted while memory is taken, limit {limit_mb} MB")
    for name, what in (("removed", "that removed what an earlier run left"), ("given", "that deleted the test's own files")):
        assert seen[name]["limit"] == "memory", f"a run {what} held more than its limit and was to fail for memory: {seen[name]}"
        note_figures(f"a run {what}, limit {limit_mb} MB", counted=seen[name]["measured"].get("memory_highest", 0) / 2**20)
    assert outcomes(result)["test_before"] == "passed" and outcomes(result)["test_after"] == "passed", lost(result, tmp_path / "out")


# What is still charged to the container when the next case begins, or when the first one begins
# after the build. The build can write into one memory-backed folder; a case can leave folders,
# open files and processes.
CARRIES = r"""
import resource
what = sys.argv[2]
if what == "folders":
    made = 0
    try:
        while True:
            os.mkdir(f"{os.environ['HOME']}/d{made}")
            made += 1
    except OSError:
        pass
elif what == "open files":
    most = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (most, most))
    kept = [os.memfd_create("m") for _ in range(120000)]
elif what == "processes":
    for number in range(14):
        if os.fork() == 0:
            block = bytearray(b"x") * (14 * MB)
            time.sleep(30)
            os._exit(0)
    time.sleep(0.5)
print("done")
"""

CARRY_TESTS = READING + """
def note(name, value):
    seen = json.load(open("/logs/verifier/attack.json")) if os.path.exists("/logs/verifier/attack.json") else {}
    seen[name] = value
    with open("/logs/verifier/attack.json", "w") as f:
        json.dump(seen, f)


def begins(name):
    with case() as c:
        note(name, reading(name))
        assert c.run(["plain"]).out == "plain\\n"


def leaves(what):
    with case() as c:
        c.run(["carries", what])


def test_0_the_first_case():
    stat = dict(line.split()[:2] for line in open("/sys/fs/cgroup/memory.stat"))
    note("after the build", {"in the memory folder": sorted(os.listdir("/dev/shm")), "shared_mb": int(stat["shmem"]) / 2**20})
    begins("the first case")


def test_1_leaves_folders():
    leaves("folders")


def test_2_after_folders():
    begins("after folders")


def test_3_leaves_open_files():
    leaves("open files")


def test_4_after_open_files():
    begins("after open files")


def test_5_leaves_processes():
    leaves("processes")


def test_6_after_processes():
    begins("after processes")
"""


def test_nothing_is_carried_into_the_starting_figure_of_the_next_case(tmp_path):
    """The build writes 60 MB into the container's memory-backed folder: the judge empties that
    folder before the first case, so nothing of it stays charged. Then three cases leave what a
    case can leave, some 20,000 folders, 120,000 open files, fourteen processes holding 14 MB
    each. What the container really holds when the case after each of them begins is what it held
    when the first case began."""
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/leaves.py").write_text(
        "with open('/dev/shm/left', 'wb') as f:\n    f.write(b'x' * (60 << 20))\n", encoding="utf-8")
    result = judge_attack(tmp_path, PLAIN + CARRIES, {"functional/test_carries.py": CARRY_TESTS}, {"reading": True},
                          BUILD_CMD="python3 leaves.py", CASE_TIMEOUT_SEC="60", CASE_PROCESSES="16")
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    assert seen["after the build"]["in the memory folder"] == [] and seen["after the build"]["shared_mb"] < 1, (
        f"what the build left in the memory-backed folder is still there when the first case begins: {seen['after the build']}")
    first = seen["the first case"]["charged"]
    carried = {name: (seen[name]["charged"] - first) / 2**20 for name in ("after folders", "after open files", "after processes")}
    note_figures("carried into the next case's start", **{name.replace(" ", "_"): value for name, value in carried.items()})
    for name, value in carried.items():
        assert value <= TOLERANCE_MB, f"{value:.1f} MB more are charged to the container {name} than when the first case began: {seen}"
    said = outcomes(result)
    assert all(said[name] == "passed" for name in said if "after" in name or "first" in name), lost(result, tmp_path / "out")


# What the kernel ends first when the container's memory runs out is the judge's decision, not a
# matter of which process happens to hold more: the program's side, then the case runner, and the
# judge last.
ORDER_TESTS = '''
def of(pid):
    return {"mark": int(open(f"/proc/{pid}/oom_score_adj").read()), "score": int(open(f"/proc/{pid}/oom_score").read())}


def test_the_order():
    with case() as c:
        r = c.run(["order"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"program": json.loads(r.out), "case_runner": of("self"), "judge": of(os.getppid())}, f)
'''


def test_when_memory_runs_out_the_kernel_ends_the_program_first_then_the_case_runner_and_the_judge_last(tmp_path):
    program = PLAIN + '''
import json
held = bytearray(b"x") * (8 * MB)
print(json.dumps({"mark": int(open("/proc/self/oom_score_adj").read()), "score": int(open("/proc/self/oom_score").read())}))
'''
    result = judge_attack(tmp_path, program, {"functional/test_order.py": ORDER_TESTS}, room={"memory_mb": 640})
    assert outcomes(result) == {"test_the_order": "passed"}, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    marks = [seen[who]["mark"] for who in ("program", "case_runner", "judge")]
    assert marks == [1000, 500, 0], f"the marks by which the kernel chooses what it ends: {seen}"
    assert seen["program"]["score"] > seen["case_runner"]["score"] > seen["judge"]["score"], seen


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
    for row in report["rows"]:
        # every run gave a judgement, with or without the judge's stop 128 MB below the cap
        assert row["judged_as_meant"] == row["runs"] == 1, row
        assert "kernel_ended_the_case_runner" in row, f"the row does not say whether the kernel ended the case runner: {row}"
        if row["stop_128_mb_below_the_cap"]:
            assert row["measured_runs"] == 1, row
        else:
            assert row["measured_runs"] + row["kernel_ended_the_case_runner"] == 1, row


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


FILLS_ITS_LIMIT = r'''
import json, os, threading, time
stop = threading.Event()
while True:
    try:
        threading.Thread(target=stop.wait, daemon=True).start()
    except RuntimeError:
        break
time.sleep(0.5)
uid, own, others = os.getuid(), 0, 0
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        for task in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{task}/status") as f:
                owner = int(next(line for line in f if line.startswith("Uid:")).split()[1])
            own, others = (own + 1, others) if owner == uid else (own, others + 1)
    except (OSError, StopIteration):
        pass
with open("/sys/fs/cgroup/pids.current") as f:
    print(json.dumps({"own": own, "others": others, "container": int(f.read())}))
stop.set()
'''


@pytest.mark.parametrize("limit", [16, 64])
def test_the_judge_needs_no_more_processes_than_the_checker_leaves_it(tmp_path, limit):
    """While a case runs, the container holds the case's processes and the judge's own: the verifier's
    script, the judge and the case runner, three, and when a program left processes, one more that
    ends them. The checker leaves the judge eight above a case's limit. With the container's cap
    there, a program that makes processes until it is refused reaches its own limit, of 16 and of 64."""
    tests = {"functional/test_fill.py": '''
def test_fill():
    with case() as c:
        r = c.run([])
        with open("/logs/verifier/attack.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''}
    result = judge_attack(tmp_path, FILLS_ITS_LIMIT, tests, {"processes": limit + 8}, CASE_PROCESSES=str(limit))
    assert result["exit_code"] == 0 and outcomes(result) == {"test_fill": "passed"}, lost(result, tmp_path / "out")
    seen = found_by(tmp_path / "out", "attack.json")
    note_figures(f"processes while a case holds its limit of {limit}", own=seen["own"], judge=seen["others"])
    assert seen["own"] == limit and seen["others"] <= 3 and seen["container"] <= limit + 3, seen
