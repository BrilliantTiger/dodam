"""What a served web program and its build can learn about the judging, tried in the verifier's container.

The web judge keeps the same separation as the command-line judge: before anything of the agent's
runs, only root may enter the tests and the results, and the command lines of the judge's processes
say nothing about the test being run. The build and the served program run confined. These need
Docker and the Python image, and are skipped where either is missing; CI builds the image.
"""
import json
import shutil

import pytest

from conftest import CANARY
from test_cli_isolation import BENCH, IMAGE, _image_ready, found_by, in_the_image, lost, message, outcomes

pytestmark = pytest.mark.skipif(not _image_ready(), reason="needs Docker and the Python image")

SERVED = r'''
import ctypes, http.server, json, os

libc = ctypes.CDLL(None, use_errno=True)


def look():
    seen = {"files": [], "marker": False, "command lines": []}
    for top in ("/tests", "/logs", "/app/stolen"):
        for folder, dirs, files in os.walk(top):
            for name in files:
                path = os.path.join(folder, name)
                seen["files"].append(path)
                try:
                    seen["marker"] = seen["marker"] or "MARKER" in open(path, errors="replace").read()
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
    try:
        seen["seen by the build"] = json.load(open("/app/seen-by-the-build.json"))
    except OSError:
        seen["seen by the build"] = None
    return seen


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(look()).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''.replace("MARKER", CANARY)

BUILD = r'''
import json, os
seen = {"files": [], "marker": False}
for top in ("/tests", "/logs"):
    for folder, dirs, files in os.walk(top):
        for name in files:
            seen["files"].append(os.path.join(folder, name))
seen["no new privileges"] = [l.split()[1] for l in open("/proc/self/status") if l.startswith("NoNewPrivs")]
json.dump(seen, open("/app/seen-by-the-build.json", "w"))
'''

HIDDEN = f'''# {CANARY}
import json, os, urllib.request

SECRET_ANSWER = "expected-answer-91c4"


def test_the_served_program_is_asked_what_it_sees():
    with urllib.request.urlopen(os.environ["BASE_URL"] + "/look", timeout=20) as response:
        seen = response.read().decode()
    with open("/logs/verifier/web-look.json", "w") as f:
        f.write(seen)
    assert json.loads(seen)["user"] == 1000
'''


@pytest.fixture(scope="module")
def looked(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("web")
    tests_dir, solution = tmp_path / "tests", tmp_path / "solution"
    (tests_dir / "lib").mkdir(parents=True)
    (tests_dir / "security").mkdir()
    solution.mkdir()
    (solution / "main.py").write_text(SERVED, encoding="utf-8")
    (solution / "look.py").write_text(BUILD, encoding="utf-8")
    shutil.copy(BENCH / "dodambench/judge.py", tests_dir / "lib/dodam_judge.py")
    (tests_dir / "security/test_look.py").write_text(HIDDEN, encoding="utf-8")
    (tests_dir / "manifest.toml").write_text(
        '[[test]]\nfile = "security/test_look.py"\nkind = "security"\nstep = "build"\nweakness = "path-traversal"\n',
        encoding="utf-8")
    env = {"STEPS": "build", "BUILD_CMD": "python3 look.py; cp -R /tests /app/stolen; true",
           "START_CMD": "python3 main.py", "PORT": "8080", "DEPS_DIR": "", "DEPS_LINKS": ""}
    (tests_dir / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    result = in_the_image(tests_dir, solution, tmp_path / "out", without=("cases",))
    assert outcomes(result) == {"test_the_served_program_is_asked_what_it_sees": "passed"}, result
    return found_by(tmp_path / "out", "web-look.json")


def test_neither_the_build_nor_the_served_program_can_read_the_tests(looked):
    assert looked["files"] == [] and looked["marker"] is False, looked["files"]
    assert looked["seen by the build"] is not None and looked["seen by the build"]["files"] == []
    assert CANARY not in json.dumps(looked) and "expected-answer-91c4" not in json.dumps(looked)


def test_the_served_program_learns_nothing_about_the_test_being_run(looked):
    said = " ".join(looked["command lines"])
    assert looked["command lines"], "the program was to read the command lines of the other processes"
    for word in ("test_look", "security", "junit", ".xml", "--step", "--tests", "/logs/verifier", "manifest"):
        assert word not in said, (word, looked["command lines"])


def test_the_build_and_the_served_program_run_confined(looked):
    assert looked["no new privileges"] == ["1"] and looked["seen by the build"]["no new privileges"] == ["1"]
    assert looked["first to go"] == "1000" and looked["shared memory"] is False


# ---------------------------------------------------------------------------------------------
# The web judge under the same rules as the command-line judge: caps on its container, a judgement
# after a kill, and nothing of one start of the program left for the next.
# ---------------------------------------------------------------------------------------------

ASK = f"""# {CANARY}
import json, os, time, urllib.request


def ask(path):
    with urllib.request.urlopen(os.environ["BASE_URL"] + path, timeout=20) as response:
        return response.read().decode()


"""
SERVES = r"""
import http.server, json, os, sys, time


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = answer(self.path).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve():
    http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()


"""


def judge_web(tmp_path, program: str, tests: dict[str, str], **room) -> dict:
    """Judge a served Python program in the image; hidden tests are {"<kind>/<file>": source}."""
    tests_dir, solution = tmp_path / "tests", tmp_path / "solution"
    (tests_dir / "lib").mkdir(parents=True)
    solution.mkdir()
    (solution / "main.py").write_text(SERVES + program, encoding="utf-8")
    shutil.copy(BENCH / "dodambench/judge.py", tests_dir / "lib/dodam_judge.py")
    manifest = []
    for file, source in tests.items():
        kind = file.split("/")[0]
        (tests_dir / file).parent.mkdir(parents=True, exist_ok=True)
        (tests_dir / file).write_text(ASK + source, encoding="utf-8")
        manifest.append(f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "build"\n'
                        + ('weakness = "path-traversal"\n' if kind == "security" else ""))
    (tests_dir / "manifest.toml").write_text("\n".join(manifest), encoding="utf-8")
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": "python3 main.py", "PORT": "8080", "DEPS_DIR": "",
           "DEPS_LINKS": ""}
    (tests_dir / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    without = tuple(room.pop("without", ())) + ("cases",)
    return in_the_image(tests_dir, solution, tmp_path / "out", without=without, **room)


PLAIN_SERVER = """
def answer(path):
    return "ok"


serve()
"""
ONE_TEST = {"functional/test_one.py": """
def test_the_program_answers():
    assert ask("/") == "ok"
"""}
IN_THE_MIDDLE = """
def test_before():
    assert ask("/") == "ok"


def test_the_one_that_is_ended():
    MIDDLE


def test_after():
    assert ask("/") == "ok"
"""
OTHER = {"boundary/test_other.py": """
def test_in_another_file():
    assert ask("/") == "ok"
"""}
ENDED = {"test_before": "passed", "test_the_one_that_is_ended": "failed", "test_after": "passed",
         "test_in_another_file": "passed"}


@pytest.mark.parametrize("room, key", [
    ({"without": ("processes",)}, "processes"),
    ({"memory_mb": 65536}, "memory"),
])
def test_the_web_judge_refuses_a_container_whose_caps_are_missing_or_cannot_hold(tmp_path, room, key):
    """The caps are checked for every kind of task: a served program and its build are the agent's code too."""
    result = judge_web(tmp_path, PLAIN_SERVER, ONE_TEST, **room)
    assert result["exit_code"] == 3 and result["reward"] is None, lost(result, tmp_path / "out")
    assert result["fault"] is not None, "the judge ran a served program in a container without caps that hold"
    refusal = json.loads(json.loads(result["fault"])["judge_fault"])
    assert refusal["severity"] == "error" and refusal["key"] == key and refusal["message"], refusal


def test_the_web_judges_result_names_the_caps_of_its_container_and_whether_it_may_swap(tmp_path):
    result = judge_web(tmp_path, PLAIN_SERVER, ONE_TEST)
    assert outcomes(result) == {"test_the_program_answers": "passed"}, lost(result, tmp_path / "out")
    assert result["report"].get("caps") == {"memory_mb": 1024, "processes": 512, "swap_mb": 0}, result["report"].get("caps")


def test_a_served_program_the_kernel_ends_fails_that_case_and_the_rest_meet_a_fresh_program(tmp_path):
    """Really out of memory: the served program takes memory until the kernel ends it. The hidden
    test that asked for it would pass by itself; it fails because the kernel ended a process while
    it ran, and the cases after it are judged against a program started afresh."""
    program = """
held = []


def answer(path):
    while path == "/eat":
        held.append(bytearray(b"x") * (16 * 2**20))
    return "ok"


serve()
"""
    middle = 'try:\n        ask("/eat")\n    except OSError:\n        pass'
    result = judge_web(tmp_path, program, {"functional/test_ended.py": IN_THE_MIDDLE.replace("MIDDLE", middle), **OTHER},
                       memory_mb=640)
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == ENDED, lost(result, tmp_path / "out")
    assert "ran out of memory" in message(result, "test_the_one_that_is_ended"), lost(result, tmp_path / "out")


def test_a_web_case_runner_that_is_ended_costs_only_the_case_that_was_running(tmp_path):
    result = judge_web(tmp_path, PLAIN_SERVER,
                       {"functional/test_ended.py": IN_THE_MIDDLE.replace("MIDDLE", "os.kill(os.getpid(), 9)"), **OTHER})
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, tmp_path / "out")
    assert outcomes(result) == ENDED, lost(result, tmp_path / "out")
    assert "case runner" in message(result, "test_the_one_that_is_ended")


def test_a_web_judge_that_is_ended_leaves_nothing_that_reads_as_a_judgement(tmp_path):
    middle = "os.kill(os.getppid(), 9)\n    time.sleep(30)"
    result = judge_web(tmp_path, PLAIN_SERVER, {"functional/test_ended.py": IN_THE_MIDDLE.replace("MIDDLE", middle)})
    out = tmp_path / "out"
    assert result["exit_code"] != 0 and result["reward"] is None and not (out / "reward.json").exists()
    assert result["fault"] is not None, "the judging ended without a result and nothing says so"
    said = json.loads(json.loads(result["fault"])["judge_fault"])
    assert said["severity"] == "error" and said["key"] == "ended", said


FILLS_ITS_DATA_FOLDER = """
data = os.path.dirname(os.environ["DB_PATH"])
for n in range(2000):
    open(os.path.join(data, str(n)), "w").close()


def answer(path):
    return "ok"


serve()
"""


def test_a_served_programs_data_folder_the_judge_cannot_remove_fails_the_step_with_a_result_in_the_image(tmp_path):
    """Each start of the served program writes 2,000 files into its data folder, and the judge's
    bound on removing is set to 1,000 entries here. The first file of tests has a program of its
    own and passes; that program's data folder cannot be removed after it. The step fails with
    that reason and a result is written: the program is not started again, and the file of tests
    after it is judged as for a program that does not start."""
    result = judge_web(tmp_path, FILLS_ITS_DATA_FOLDER,
                       {"security/test_first.py": "def test_first():\n    assert ask(\"/\") == \"ok\"\n",
                        "functional/test_second.py": "def test_second():\n    assert ask(\"/\") == \"ok\"\n"},
                       changed=(("dodam_judge.py", "TREE_ENTRIES = 500_000", "TREE_ENTRIES = 1_000"),))
    out = tmp_path / "out"
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, out)
    assert result["reward"]["reward"] == 0.0, f"the step came out as passed: {result['reward']}"
    assert outcomes(result) == {"test_first": "passed", "test_second.py": "failed"}, lost(result, out)
    assert "data folder" in message(result, "test_second.py"), lost(result, out)


LEAVES = r"""
note, data = "/app/last-data-folder", os.path.dirname(os.environ["DB_PATH"])
earlier = open(note).read() if os.path.exists(note) else ""
seen = {"earlier data folder": earlier, "it is still there": bool(earlier) and os.path.exists(earlier)}
open(note, "w").write(data)
os.chdir(data)
for _ in range(3000):            # the data folder is the program's to fill
    os.mkdir("d")
    os.chdir("d")
os.chdir("/app")


def answer(path):
    return json.dumps(seen)


if os.fork() == 0:
    os.setsid()                  # the server leaves the process group the judge started
    serve()
time.sleep(600)
"""
LOOKS = """
def test_NAME():
    seen = ask("/")
    with open("/logs/verifier/NAME.json", "w") as f:
        f.write(seen)
"""


@pytest.fixture(scope="module")
def left_behind(tmp_path_factory) -> dict:
    """A served program that is started twice. Each start leaves the group of processes the judge
    started and fills its data folder 3,000 folders deep; the second start says what it finds."""
    tmp_path = tmp_path_factory.mktemp("left")
    result = judge_web(tmp_path, LEAVES, {"security/test_first.py": LOOKS.replace("NAME", "first"),
                                          "security/test_second.py": LOOKS.replace("NAME", "second")})
    return {"result": result, "out": tmp_path / "out"}


def test_a_served_program_that_left_its_process_group_is_ended_before_the_next_start(left_behind):
    result, out = left_behind["result"], left_behind["out"]
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, out)
    # a server that was still running would hold the port, and the second start would not come up
    assert outcomes(result) == {"test_first": "passed", "test_second": "passed"}, lost(result, out)


def test_what_one_start_of_a_served_program_wrote_is_gone_before_the_next(left_behind):
    result, out = left_behind["result"], left_behind["out"]
    assert result["exit_code"] == 0 and result["reward"] is not None, lost(result, out)
    seen = found_by(out, "second.json")
    assert seen["earlier data folder"] and seen["it is still there"] is False, seen
