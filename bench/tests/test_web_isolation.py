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
from test_cli_isolation import BENCH, IMAGE, _image_ready, found_by, in_the_image, outcomes

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
