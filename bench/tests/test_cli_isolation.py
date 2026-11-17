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
import time
import uuid
from pathlib import Path

import pytest

from conftest import CANARY
from dodambench.task import load_task
from dodambench.vet import VetError, vet

BENCH = Path(__file__).resolve().parents[1]
IMAGE = "dodam-bench/python:dev"
ROOM = {"memory_mb": 2048, "cpus": 2}
LIMITS = {"CASE_TIMEOUT_SEC": "5", "CASE_MEMORY_MB": "256", "CASE_OUTPUT_KB": "256", "CASE_PROCESSES": "16",
          "CASE_DISK_MB": "32"}
HEAD = f"# {CANARY}\nimport json, os, time\nfrom dodam_cli import case, need\n\nSECRET_ANSWER = 'expected-answer-91c4'\n\n\n"


def _image_ready() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _image_ready(), reason="needs Docker and the Python image")


def judge_attack(tmp_path: Path, program: str, tests: dict[str, str], **changes: str) -> dict:
    """Judge an attacking Python program in the image; hidden tests are {"<kind>/<file>": source}."""
    tests_dir, solution = tmp_path / "tests", tmp_path / "solution"
    (tests_dir / "lib").mkdir(parents=True)
    solution.mkdir()
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
    return in_the_image(tests_dir, solution, tmp_path / "out")


def in_the_image(tests_dir: Path, solution: Path, out: Path) -> dict:
    """What the verifier does: a fresh container without network, the judge as root with its own Python,
    the program's build user named agent. Returns the judge's exit code and whatever it wrote."""
    name = f"dodam-attack-{uuid.uuid4().hex[:12]}"
    script = ("mkdir -p /logs/verifier && cp -R /solution/. /app/ && exec /opt/judge/bin/python "
              "/tests/lib/dodam_judge.py --tests /tests --app /app --step build --out /logs/verifier")
    out.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0",
                        "--memory", f"{ROOM['memory_mb']}m", "--memory-swap", f"{ROOM['memory_mb']}m",
                        "--cpus", str(ROOM["cpus"]), "-e", "DODAM_APP_USER=agent", IMAGE, "sh", "-c", script],
                       check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{tests_dir}/.", f"{name}:/tests"], check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{solution}/.", f"{name}:/solution"], check=True, capture_output=True)
        done = subprocess.run(["docker", "start", "--attach", name], capture_output=True, timeout=600)
        subprocess.run(["docker", "cp", f"{name}:/logs/verifier/.", str(out)], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    result = {"exit_code": done.returncode, "reward": None, "report": {"tests": []}}
    if (out / "reward.json").is_file():
        result["reward"] = json.loads((out / "reward.json").read_text(encoding="utf-8"))
        result["report"] = json.loads((out / "details.json").read_text(encoding="utf-8"))
    return result


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
    seen = []
    for folder, dirs, files in os.walk("/tests"):
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


attempt("read the tests", read_tests)
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
    result = judge_attack(tmp_path, SNOOP, {"security/test_attack.py": REPORT}, CASE_TIMEOUT_SEC="90")
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


def test_the_program_cannot_write_replace_or_remove_the_result(snoop):
    found = snoop["found"]
    for attack in ("write the result", "replace the result folder", "remove the result", "plant a link as the result"):
        assert found[attack].startswith("refused"), (attack, found[attack])
    reward = json.loads((snoop["out"] / "reward.json").read_text())
    assert reward == snoop["result"]["reward"] and reward["security_judged"] == 2.0
    assert outcomes(snoop["result"]) == {"test_attack": "passed", "test_after_the_attack": "passed"}


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
def test_before():
    with case() as c:
        assert c.run(["plain"]).out == "plain\\n"


def test_the_attack():
    with case() as c:
        started = time.monotonic()
        r = c.run(["attack"])
        with open("/logs/verifier/attack.json", "w") as f:
            json.dump({"exit_code": r.exit_code, "limit": r.limit, "seconds": time.monotonic() - started,
                       "out": r.out[-300:], "err": r.err[-300:]}, f)
        assert r.exit_code == 0


def test_after():
    with case() as c:
        r = c.run(["count"])
        assert r.out == "1\\n", r
        assert c.run(["plain"]).out == "plain\\n"
'''

PLAIN = r'''
import os, sys
if sys.argv[1:] == ["plain"]:
    print("plain")
    sys.exit(0)
if sys.argv[1:] == ["count"]:
    mine = os.getuid()
    print(sum(1 for pid in os.listdir("/proc") if pid.isdigit() and os.stat("/proc/" + pid).st_uid == mine))
    sys.exit(0)
'''

ATTACKS = {
    "memory": ("memory", r'''
blocks = []
while True:
    blocks.append(bytearray(16 * 1024 * 1024))
    for i in range(0, len(blocks[-1]), 4096):
        blocks[-1][i] = 1
'''),
    "memory in many processes": ("memory", r'''
import time
for _ in range(8):
    if os.fork() == 0:
        block = bytearray(64 * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        time.sleep(60)
        os._exit(0)
time.sleep(60)
'''),
    "fork without end": ("processes", r'''
while True:
    try:
        os.fork()
    except OSError:
        pass
'''),
    "threads without end": (None, r'''
import threading, time
while True:
    try:
        threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
    except RuntimeError:
        time.sleep(0.01)
'''),
    "one huge file": ("files", r'''
with open("big.bin", "wb") as f:
    while True:
        f.write(b"x" * (1024 * 1024))
'''),
    "many files": ("files", r'''
i = 0
while True:
    i += 1
    with open("part-%d.bin" % i, "wb") as f:
        f.write(b"x" * (1024 * 1024))
'''),
    "files in its home and temporary folder": ("files", r'''
import tempfile
i = 0
while True:
    i += 1
    for place in (os.environ["HOME"], tempfile.gettempdir()):
        with open(os.path.join(place, "part-%d.bin" % i), "wb") as f:
            f.write(b"x" * (1024 * 1024))
'''),
    "countless empty files": ("files", r'''
i = 0
while True:
    i += 1
    open("empty-%d" % i, "w").close()
'''),
    "flood of output": ("output", r'''
while True:
    sys.stdout.write("x" * 65536)
'''),
    "ignores every signal and hangs": ("time", r'''
import signal, time
for number in range(1, 32):
    try:
        signal.signal(number, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
while True:
    time.sleep(1)
'''),
    "kills everything it can": ("survives", r'''
import signal
for target in (1, os.getppid(), -1):
    try:
        os.kill(target, signal.SIGKILL)
    except OSError:
        pass
print("still here")
'''),
    "more memory than the whole container has": ("ended by the kernel", r'''
blocks = []
while True:
    blocks.append(bytearray(16 * 1024 * 1024))
    for i in range(0, len(blocks[-1]), 4096):
        blocks[-1][i] = 1
'''),
    "leaves a daemon in a session of its own": ("left processes", r'''
import time
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


@pytest.mark.parametrize("attack", list(ATTACKS))
def test_a_program_that_breaks_a_limit_fails_that_case_only_and_never_the_judge(tmp_path, attack):
    limit, body = ATTACKS[attack]
    started = time.monotonic()
    # one attack gets a memory limit the container cannot give: then the kernel must end the program, not the judge
    changes = {"CASE_MEMORY_MB": "100000"} if limit == "ended by the kernel" else {}
    result = judge_attack(tmp_path, PLAIN + body, {"functional/test_limits.py": LIMIT_TESTS}, **changes)
    seconds = time.monotonic() - started
    seen = found_by(tmp_path / "out", "attack.json")
    if limit == "ended by the kernel":
        assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, (seen, result["report"])
        assert seen["exit_code"] is None and seen["limit"] is None, seen
        assert result["exit_code"] == 0 and seconds < 90
        return
    if limit == "survives":
        # the program could signal nobody but itself: it ends well, and so does everything around it
        assert set(outcomes(result).values()) == {"passed"} and seen["out"] == "still here\n", (seen, result["report"])
        return
    assert outcomes(result) == {"test_before": "passed", "test_the_attack": "failed", "test_after": "passed"}, (seen, result["report"])
    assert seen["limit"] == limit or (limit is None and seen["limit"] in ("time", "memory", "processes")), seen
    assert (limit or seen["limit"]) in message(result, "test_the_attack"), message(result, "test_the_attack")
    assert seen["seconds"] < 15 and seconds < 90, (seen["seconds"], seconds)
    assert result["reward"]["functional_pass_rate"] == round(2 / 3, 4)
    assert not (tmp_path / "out/judge-fault.json").exists()


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
    # the first case could write only in its own folder, and did leave kernel objects behind
    assert len(left["left"]) == 3 and left["shared memory"] and left["message queue"], left
    assert found == {"markers": [], "shared memory": False, "message queue": False, "other processes": [],
                     "folder": ["other.txt"], "siblings": ["home", "outside", "work"]}, found


def test_a_build_cannot_prepare_the_ground_for_the_program(tmp_path):
    """The build is the program's own code too: what it leaves running or writable is gone before the first case."""
    build = ("(setsid sh -c 'sleep 600' >/dev/null 2>&1 &) ; mkdir -p /tmp/open /app/open && chmod 0777 /tmp/open /app/open "
             "&& cp /bin/sh /app/as-agent && chmod 6755 /app/as-agent")
    program = r'''
import json, os, subprocess
mine = os.getuid()
found = {"sleepers": [pid for pid in os.listdir("/proc") if pid.isdigit()
                      and b"sleep" in open("/proc/%s/cmdline" % pid, "rb").read()],
         "writable": [p for p in ("/tmp/open", "/app/open", "/app", "/tmp") if os.access(p, os.W_OK)],
         "as agent": subprocess.run(["/app/as-agent", "-p", "-c", "id -u"], capture_output=True, text=True).stdout.strip(),
         "uid": mine}
print(json.dumps(found))
'''
    tests = {"security/test_ground.py": '''
def test_ground():
    with case() as c:
        r = c.run([])
        with open("/logs/verifier/ground.json", "w") as f:
            f.write(r.out)
        assert r.exit_code == 0, r
'''}
    result = judge_attack(tmp_path, program, tests, BUILD_CMD=build)
    assert outcomes(result) == {"test_ground": "passed"}, result["report"]
    found = found_by(tmp_path / "out", "ground.json")
    assert found["sleepers"] == [] and found["writable"] == [], found
    assert found["as agent"] == str(found["uid"]) and found["uid"] >= 200_000, found


def test_a_fault_of_the_judge_in_the_container_leaves_no_result(tmp_path):
    tests = {"functional/test_fault.py": '''
import shutil
from dodam_cli import config


def test_the_ground_is_gone():
    shutil.rmtree(config()["work_root"])
    with case() as c:
        assert c.run(["plain"]).exit_code == 0
'''}
    result = judge_attack(tmp_path, "print('plain')\n", tests)
    assert result["exit_code"] == 3 and result["reward"] is None
    assert not (tmp_path / "out/reward.json").exists()
    assert "judge_fault" in found_by(tmp_path / "out", "judge-fault.json")


def test_vetting_gives_the_container_the_room_the_task_gives_the_verifier(cli_task_dir, tmp_path, monkeypatch):
    """A case's memory limit is measured against the container's own, so vetting sets it as Harbor does."""
    created = []
    real = subprocess.run

    def spy(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["docker", "create"]:
            created.append(list(cmd))
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr("dodambench.vet.subprocess.run", spy)
    vet(load_task(cli_task_dir), "python", tmp_path / "out", image=IMAGE)
    assert created
    for cmd in created:
        assert cmd[cmd.index("--network") + 1] == "none"
        assert "--memory" in cmd and cmd[cmd.index("--memory") + 1] == "2048m", cmd
        assert cmd[cmd.index("--cpus") + 1] == "2"


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
