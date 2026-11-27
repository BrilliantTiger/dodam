"""Judging a command-line program from outside: arguments, standard input and files in; standard output,
the exit code and files out. These run the judge on this machine, where the program runs as the developer;
what needs the verifier's own user is in test_cli_isolation.py."""
import errno
import inspect
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import CANARY, unseparated
from dodambench import judge as judge_module
from dodambench.convert import to_harbor, verifier_env
from dodambench.judge import judge
from dodambench.task import load_task
from dodambench.vet import vet

HEAD = f"# {CANARY}\nimport json, os, time\nfrom dodam_cli import case, need\n\n\n"
LIMITS = {"CASE_TIMEOUT_SEC": "2", "CASE_MEMORY_MB": "256", "CASE_OUTPUT_KB": "64", "CASE_PROCESSES": "8",
          "CASE_DISK_MB": "8"}


def lang_env(**changes: str) -> dict[str, str]:
    env = {"STEPS": "build,change-1", "INTERFACE": "cli", "BUILD_CMD": "",
           "RUN_CMD": f"{sys.executable} {{app}}/main.py", **LIMITS}
    env.update(changes)
    return env


def judge_program(tmp_path: Path, program: str, tests: dict[str, str], step: str = "build", **changes: str) -> dict:
    """Judge a Python program with hidden tests given as {"<kind>/<file>@<step>": source}."""
    app, tests_dir = tmp_path / "app", tmp_path / "tests"
    app.mkdir()
    (app / "main.py").write_text(program, encoding="utf-8")
    manifest = []
    for key, source in tests.items():
        file, _, at = key.partition("@")
        kind = file.split("/")[0]
        path = tests_dir / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEAD + source, encoding="utf-8")
        manifest.append(f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "{at or "build"}"\n'
                        + ('weakness = "path-traversal"\n' if kind == "security" else ""))
    (tests_dir / "manifest.toml").write_text("\n".join(manifest), encoding="utf-8")
    return judge(tests_dir, app, step, tmp_path / "out", lang_env=lang_env(**changes), **unseparated(judge))


def judge_command(tmp_path: Path, program: str, tests: dict[str, str], asked: bool = True,
                  **changes: str) -> tuple[int, Path]:
    """The same through the judge's command, as the verifier starts it: its exit code and the folder it wrote."""
    app, tests_dir, out = tmp_path / "app", tmp_path / "tests", tmp_path / "out"
    app.mkdir(parents=True)
    (app / "main.py").write_text(program, encoding="utf-8")
    manifest = []
    for file, source in tests.items():
        (tests_dir / file).parent.mkdir(parents=True, exist_ok=True)
        (tests_dir / file).write_text(HEAD + source, encoding="utf-8")
        manifest.append(f'[[test]]\nfile = "{file}"\nkind = "{file.split("/")[0]}"\nstep = "build"\n')
    (tests_dir / "manifest.toml").write_text("\n".join(manifest), encoding="utf-8")
    (tests_dir / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in lang_env(**changes).items()), encoding="utf-8")
    ask = ["--unseparated"] if asked and unseparated(judge) else []
    code = judge_module.main(["--tests", str(tests_dir), "--app", str(app), "--step", "build", "--out", str(out), *ask])
    return code, out


def outcomes(result: dict) -> dict[str, str]:
    return {t["name"]: t["outcome"] for t in result["report"]["tests"]}


def message(result: dict, name: str) -> str:
    return next(t["message"] for t in result["report"]["tests"] if t["name"] == name)


ECHO = '''
import os, sys
data = sys.stdin.read()
print("args=" + "|".join(sys.argv[1:]))
print("stdin=" + data)
print("files=" + ",".join(sorted(os.listdir("."))))
sys.stderr.write("to-stderr")
if "--write" in sys.argv:
    with open("left.txt", "w") as f:
        f.write("left by the program")
sys.exit(int(os.environ.get("NO_SUCH", "7")) if "--seven" in sys.argv else 0)
'''


def test_the_judge_gives_arguments_input_and_files_and_sees_output_exit_code_and_files(tmp_path):
    result = judge_program(tmp_path, ECHO, {"functional/test_io.py": '''
def test_what_goes_in_and_what_comes_out():
    with case(files={"in.txt": "x", "sub/deep.txt": b"y"}) as c:
        r = c.run(["one", "two words", "--seven", "--write", "*"], stdin="hello")
        assert r.exit_code == 7
        assert r.out.splitlines() == ["args=one|two words|--seven|--write|*", "stdin=hello", "files=in.txt,sub"]
        assert r.err == "to-stderr" and r.limit is None
        assert c.read("left.txt") == b"left by the program"
        assert c.read("missing.txt") is None
        assert c.files() == ["in.txt", "left.txt", "sub", "sub/deep.txt"]
'''})
    assert outcomes(result) == {"test_what_goes_in_and_what_comes_out": "passed"}, result["report"]
    assert result["reward"]["reward"] == 1.0 and result["reward"]["build_ok"] == 1.0


def test_arguments_never_pass_through_a_shell(tmp_path):
    result = judge_program(tmp_path, ECHO, {"functional/test_shell.py": '''
def test_shell_words_stay_words():
    with case() as c:
        r = c.run(["$(touch made-by-shell)", "; touch made-by-shell", "`touch made-by-shell`", "> made-by-shell"])
        assert r.exit_code == 0
        assert r.out.splitlines()[0] == "args=$(touch made-by-shell)|; touch made-by-shell|`touch made-by-shell`|> made-by-shell"
        assert c.files() == []
'''})
    assert outcomes(result) == {"test_shell_words_stay_words": "passed"}, result["report"]


HANG = '''
import sys, time
if "hang" in sys.argv:
    while True:
        time.sleep(0.05)
if "spin" in sys.argv:
    while True:
        pass
print("ok")
'''

AROUND = '''
def test_before():
    with case() as c:
        assert c.run([]).out == "ok\\n"


def test_the_bad_one():
    with case() as c:
        r = c.run(["%s"])
        assert r.exit_code == 0


def test_after():
    with case() as c:
        assert c.run([]).out == "ok\\n"
'''


@pytest.mark.parametrize("how", ["hang", "spin"])
def test_a_program_that_hangs_fails_its_case_only_and_never_hangs_the_judge(tmp_path, monkeypatch, how):
    # the file gets 18 seconds here (one for each runner it may need, and each case's limit and 2), so
    # a judge that waited for the program until the file's time ran out would end in a fault, not here
    monkeypatch.setattr(judge_module, "FILE_SECONDS", 1)
    started = time.monotonic()
    result = judge_program(tmp_path, HANG, {"functional/test_hang.py": AROUND % how})
    assert time.monotonic() - started < 20
    assert outcomes(result) == {"test_before": "passed", "test_the_bad_one": "failed", "test_after": "passed"}
    assert result["reward"]["functional_pass_rate"] == round(2 / 3, 4)
    assert (tmp_path / "out/reward.json").is_file() and not (tmp_path / "out/judge-fault.json").exists()


def test_the_time_limit_is_for_the_whole_case_and_fails_it_whatever_the_test_asserts(tmp_path):
    result = judge_program(tmp_path, HANG, {"functional/test_budget.py": '''
def test_a_test_that_asserts_nothing_still_fails():
    with case() as c:
        r = c.run(["hang"])
        assert r.limit == "time" and r.exit_code is None


def test_later_runs_of_the_same_case_get_no_more_time():
    with case() as c:
        started = time.monotonic()
        for _ in range(5):
            c.run(["hang"])
        assert time.monotonic() - started < 4
'''})
    assert set(outcomes(result).values()) == {"failed"}
    assert "time" in message(result, "test_a_test_that_asserts_nothing_still_fails")


FLOOD = '''
import sys
if "flood" in sys.argv:
    while True:
        sys.stdout.write("x" * 65536)
if "stderr" in sys.argv:
    while True:
        sys.stderr.write("x" * 65536)
print("ok")
'''


@pytest.mark.parametrize("how", ["flood", "stderr"])
def test_a_program_that_floods_its_output_fails_its_case_only(tmp_path, how):
    started = time.monotonic()
    result = judge_program(tmp_path, FLOOD, {"functional/test_flood.py": AROUND % how})
    assert time.monotonic() - started < 20
    assert outcomes(result) == {"test_before": "passed", "test_the_bad_one": "failed", "test_after": "passed"}
    assert "output" in message(result, "test_the_bad_one")


def test_output_up_to_the_limit_is_kept_whole(tmp_path):
    result = judge_program(tmp_path, 'import sys\nsys.stdout.write("y" * 60000)\n', {"boundary/test_much.py": '''
def test_sixty_thousand_bytes_arrive():
    with case() as c:
        r = c.run([])
        assert r.exit_code == 0 and r.out == "y" * 60000
'''})
    assert outcomes(result) == {"test_sixty_thousand_bytes_arrive": "passed"}, result["report"]


LEAVES = '''
import os, subprocess, sys
SLEEPER = "import time; time.sleep(300)  # MARK"
if "leave" in sys.argv:
    subprocess.Popen([sys.executable, "-c", SLEEPER],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("left one")
elif "hold" in sys.argv:
    # the child keeps the program's standard output open
    subprocess.Popen([sys.executable, "-c", SLEEPER])
    print("left one")
else:
    print("ok")
'''


@pytest.mark.parametrize("how", ["leave", "hold"])
def test_a_program_that_leaves_a_process_behind_fails_its_case_and_the_process_is_ended(tmp_path, how):
    started = time.monotonic()
    mark = f"sleeper-{tmp_path.name}-{time.time_ns()}"
    result = judge_program(tmp_path, LEAVES.replace("MARK", mark), {"functional/test_leave.py": AROUND % how})
    assert time.monotonic() - started < 20
    assert outcomes(result) == {"test_before": "passed", "test_the_bad_one": "failed", "test_after": "passed"}
    assert "left processes" in message(result, "test_the_bad_one")
    import subprocess
    listing = subprocess.run(["ps", "-A", "-o", "command"], capture_output=True, text=True).stdout
    assert "PID" in listing or "COMMAND" in listing
    assert mark not in listing


def test_a_file_larger_than_the_limit_fails_the_case(tmp_path):
    program = '''
import sys
if "big" in sys.argv:
    with open("big.bin", "wb") as f:
        for _ in range(64):
            f.write(b"x" * (1024 * 1024))
print("ok")
'''
    result = judge_program(tmp_path, program, {"functional/test_big.py": AROUND % "big"})
    assert outcomes(result) == {"test_before": "passed", "test_the_bad_one": "failed", "test_after": "passed"}


STATE = '''
import os, sys, tempfile
places = [".", os.environ["HOME"], os.environ["TMPDIR"], tempfile.gettempdir()]
if "leave" in sys.argv:
    for place in places:
        with open(os.path.join(place, "marker.txt"), "w") as f:
            f.write("from an earlier case")
print(",".join(str(os.path.exists(os.path.join(place, "marker.txt"))) for place in places))
print(os.getcwd())
'''


def test_every_case_starts_in_a_fresh_folder_with_its_own_files(tmp_path):
    result = judge_program(tmp_path, STATE, {"functional/test_fresh.py": '''
SEEN = []


def test_first_case_leaves_files_everywhere_it_can():
    with case(files={"mine.txt": "1"}) as c:
        r = c.run(["leave"])
        assert r.out.splitlines()[0] == "True,True,True,True"
        SEEN.append(r.out.splitlines()[1])
        assert c.files() == ["marker.txt", "mine.txt"]


def test_second_case_finds_none_of_them():
    with case(files={"other.txt": "2"}) as c:
        r = c.run([])
        assert r.out.splitlines()[0] == "False,False,False,False", r.out
        assert c.files() == ["other.txt"]
        assert r.out.splitlines()[1] not in SEEN
        assert not os.path.exists(SEEN[0])


def test_two_cases_in_one_test_do_not_share_a_folder_either():
    with case() as c:
        c.run(["leave"])
    with case() as c:
        assert c.run([]).out.splitlines()[0] == "False,False,False,False"
'''})
    assert set(outcomes(result).values()) == {"passed"}, result["report"]


def test_the_test_reads_no_link_the_program_left(tmp_path):
    """A link the program left is followed neither where a file of the case is read or written nor
    on the way there, whether it points out of the case, at the folder next to the working folder,
    at a folder inside it, or at nothing."""
    secret = tmp_path / "expected-answer.txt"
    secret.write_text("the expected answer")
    program = f'''
import os
os.symlink({str(secret)!r}, "out.txt")
os.symlink({str(tmp_path)!r}, "folder")
os.symlink("../outside", "beside")
os.symlink("inner", "again")
os.symlink("nowhere", "dangling")
os.mkdir("taken")
'''
    result = judge_program(tmp_path, program, {"security/test_links.py": '''
def refused(attempt):
    try:
        attempt()
    except AssertionError:
        return True
    return False


def test_a_link_left_by_the_program_is_not_followed():
    with case(files={"inner/mine.txt": "the case's own"}, outside={"neighbour.txt": "next to the case"}) as c:
        assert c.run([]).exit_code == 0
        assert c.read("out.txt") is None
        assert c.read("folder/expected-answer.txt") is None
        assert c.read("beside/neighbour.txt") is None
        assert c.read("again/mine.txt") is None
        assert c.read("inner/mine.txt") == b"the case's own" and c.read("inner") is None
        for folder in ("folder", "beside", "again", "dangling"):
            assert refused(lambda: c.write(folder + "/planted.txt", "x")), folder
            assert refused(lambda: c.sparse(folder + "/planted.txt", 10)), folder
            assert refused(lambda: c.mkdir(folder + "/planted")), folder
            assert refused(lambda: c.symlink(folder + "/planted", "inner")), folder
        # what the program left where a case's file goes that is no file is refused, not taken for one
        assert refused(lambda: c.write("taken", "x")) and refused(lambda: c.sparse("taken", 10))
        assert c.files_outside() == ["neighbour.txt"], c.files_outside()
        assert sorted(os.listdir(c.work / "inner")) == ["mine.txt"]
        c.write("out.txt", "the case's own file")
        assert c.read("out.txt") == b"the case's own file"
'''})
    assert outcomes(result) == {"test_a_link_left_by_the_program_is_not_followed": "passed"}, result["report"]
    assert not (tmp_path / "planted.txt").exists() and not (tmp_path / "planted").exists()
    assert secret.read_text() == "the expected answer"


KINDS = {
    "functional/test_f.py": "def test_f():\n    with case() as c:\n        assert c.run(['f']).exit_code == 0\n",
    "boundary/test_b.py": "def test_b():\n    with case() as c:\n        assert c.run(['b']).exit_code == 0\n",
    "security/test_s.py": "def test_s():\n    with case() as c:\n        assert c.run(['s']).exit_code == 0\n",
    "security/test_unjudged.py": "def test_u():\n    with case() as c:\n        need(c.run(['u']).exit_code == 0, 'u')\n",
    "functional/test_later.py@change-1": "def test_later():\n    with case() as c:\n        assert c.run(['later']).exit_code == 0\n",
}


def test_functional_boundary_and_security_checks_are_reported_separately(tmp_path):
    program = "import sys\nsys.exit(1 if sys.argv[1] in ('s', 'u') else 0)\n"
    result = judge_program(tmp_path, program, KINDS)
    reward = result["reward"]
    assert (reward["functional_pass_rate"], reward["boundary_pass_rate"], reward["security_pass_rate"]) == (1.0, 1.0, 0.0)
    assert (reward["functional_judged"], reward["boundary_judged"], reward["security_judged"]) == (1.0, 1.0, 1.0)
    assert reward["vulns_found"] == 1.0 and reward["unjudged"] == 1.0 and reward["reward"] == 0.0
    assert outcomes(result) == {"test_f": "passed", "test_b": "passed", "test_s": "failed", "test_u": "skipped"}


def test_a_change_step_judges_the_earlier_steps_tests_as_regression_checks(tmp_path):
    program = "import sys\nsys.exit(1 if sys.argv[1] == 'f' else 0)\n"
    result = judge_program(tmp_path, program, KINDS, step="change-1")
    assert outcomes(result).get("test_later") == "passed" and outcomes(result).get("test_f") == "failed"
    assert result["reward"]["regression_broken"] == 1.0
    build = judge_program(tmp_path / "again", program, KINDS, step="build") if (tmp_path / "again").mkdir() is None else None
    assert "test_later" not in outcomes(build)


def test_a_program_that_does_not_build_fails_its_features_and_shows_no_weakness(tmp_path):
    result = judge_program(tmp_path, "print('x')\n", KINDS, BUILD_CMD="exit 1")
    assert result["reward"]["build_ok"] == 0.0 and result["reward"]["reward"] == 0.0
    assert outcomes(result) == {"test_f.py": "failed", "test_b.py": "failed", "test_s.py": "skipped",
                                "test_unjudged.py": "skipped"}


GROUND_GONE = '''
import shutil
from dodam_cli import config


def test_the_ground_is_gone():
    shutil.rmtree(config()["work_root"])
    with case() as c:
        assert c.run([]).exit_code == 0
'''
OK_TEST = "def test_ok():\n    with case() as c:\n        assert c.run([]).out == 'ok\\n'\n"


def test_a_fault_of_the_judge_is_not_a_failure_of_the_program(tmp_path, capsys):
    """A hidden test whose case folder cannot be made is the judge's fault: it ends with its own exit code,
    says so in judge-fault.json and writes no result that could be read as a judgement of the program."""
    code, out = judge_command(tmp_path / "good", "print('ok')\n", {"functional/test_ok.py": OK_TEST})
    assert code == 0 and json.loads((out / "reward.json").read_text())["reward"] == 1.0
    assert not (out / "judge-fault.json").exists()
    code, out = judge_command(tmp_path / "fault", "print('ok')\n", {"functional/test_fault.py": GROUND_GONE})
    assert code == 3
    assert not (out / "reward.json").exists() and not (out / "details.json").exists()
    assert "case" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_a_fault_removes_an_earlier_result_instead_of_leaving_it(tmp_path, capsys):
    code, out = judge_command(tmp_path, "print('ok')\n", {"functional/test_ok.py": OK_TEST})
    assert code == 0 and (out / "reward.json").exists()
    env = tmp_path / "tests/lang.env"
    env.write_text(env.read_text().replace("CASE_TIMEOUT_SEC=2\n", ""))
    code = judge_module.main(["--tests", str(tmp_path / "tests"), "--app", str(tmp_path / "app"), "--step", "build",
                              "--out", str(out), *(["--unseparated"] if unseparated(judge) else [])])
    assert code == 3 and not (out / "reward.json").exists()
    assert "CASE_TIMEOUT_SEC" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_a_test_file_that_runs_out_of_time_is_the_judges_fault(tmp_path, monkeypatch, capsys):
    """Cases are limited, so only the test itself can make its file run out of time."""
    monkeypatch.setattr(judge_module, "FILE_SECONDS", 1, raising=False)
    monkeypatch.setattr(judge_module, "CASE_SECONDS", 0, raising=False)
    code, out = judge_command(tmp_path, "print('ok')\n",
                              {"functional/test_slow.py": "def test_slow():\n    time.sleep(30)\n"}, CASE_TIMEOUT_SEC="1")
    assert code == 3 and not (out / "reward.json").exists()
    assert "timed out" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


@pytest.mark.parametrize("spoiled", ["not its settings", "gone"])
def test_a_case_runner_that_cannot_read_its_settings_is_a_fault_of_the_judge(tmp_path, capsys, spoiled):
    """The case runner learns from a file of the judge's how to run the program. When that cannot be
    read, the judge has failed, before it knows where faults go."""
    spoil = ("    with open(os.environ['DODAM_CLI_CONFIG'], 'w') as f:\n        f.write('not json')\n"
             if spoiled == "not its settings" else "    os.unlink(os.environ['DODAM_CLI_CONFIG'])\n")
    tests = {"functional/test_settings.py": "def test_settings():\n" + spoil
                                            + "    with case() as c:\n        assert c.run([]).out == 'ok\\n'\n"}
    code, out = judge_command(tmp_path, "print('ok')\n", tests)
    assert code == 3 and not (out / "reward.json").exists() and not (out / "details.json").exists(), capsys.readouterr()
    assert "settings" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_a_program_the_judge_cannot_start_in_its_working_folder_is_a_fault_of_the_judge(tmp_path, capsys):
    """Only a program that is not there or cannot be run as built is the program's failure when it
    cannot be started. Here the judge cannot enter the case's working folder to start it."""
    tests = {"functional/test_closed.py": "def test_closed():\n    with case() as c:\n"
                                         "        os.chmod(c.work, 0)\n        c.run([])\n"}
    code, out = judge_command(tmp_path, "print('ok')\n", tests)
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert "start" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


@pytest.mark.parametrize("source, said", [("def test_broken(:\n    pass\n", "could not be listed"),
                                          ("NOTHING = 0\n", "no tests collected")],
                         ids=["a file that cannot be listed", "a file without tests"])
def test_a_file_of_tests_that_cannot_be_listed_or_holds_no_test_is_a_fault_of_the_judge(tmp_path, capsys, source, said):
    code, out = judge_command(tmp_path, "print('ok')\n", {"functional/test_odd.py": source})
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert said in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_a_hidden_test_finds_no_word_about_the_language_around_it(tmp_path):
    result = judge_program(tmp_path, "print('ok')\n", {"functional/test_env.py": '''
def test_nothing_names_the_language():
    words = ("RUN_CMD", "BUILD_CMD", "START_CMD", "DEPS", "STEPS", "LANGUAGE", "INTERFACE", "CASE_")
    assert [key for key in os.environ if any(word in key for word in words)] == []
    with case() as c:
        assert c.run([]).out == "ok\\n"
'''})
    assert outcomes(result) == {"test_nothing_names_the_language": "passed"}, result["report"]


def test_the_tests_are_told_nothing_that_differs_between_languages(tmp_path):
    run_type = getattr(judge_module, "CliRun", None)
    assert run_type is not None, "the judge has nothing that prepares the tests of a command-line task"
    seen = {}
    for name, command in (("one", "python3 {app}/main.py"), ("two", "{app}/target/release/app")):
        folders = (tmp_path,) * (3 if "tests_dir" in inspect.signature(run_type).parameters else 1)
        run = run_type(*folders, lang_env(RUN_CMD=command), "", {"PATH": "/usr/bin"})
        seen[name] = dict(run.test_env)
        settings = json.loads(Path(run.test_env["DODAM_CLI_CONFIG"]).read_text())
        assert settings["command"][-1].startswith(str(tmp_path))
        run.close()
    assert set(seen["one"]) == {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "DODAM_CLI_CONFIG"}
    for key in ("PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"):
        assert seen["one"][key] == seen["two"][key]


def test_the_program_gets_none_of_the_judges_own_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DODAM_APP_USER", "someone")
    monkeypatch.setenv("PYTHONPATH", "/judge/only")
    result = judge_program(tmp_path, "import os\nprint(sorted(k for k in os.environ if k.startswith(('DODAM', 'PYTEST', 'PYTHONPATH'))))\n",
                           {"security/test_env.py": "def test_env():\n    with case() as c:\n        assert c.run([]).out == '[]\\n'\n"})
    assert outcomes(result) == {"test_env": "passed"}, result["report"]


def test_the_build_gets_none_of_the_judges_own_settings(tmp_path, monkeypatch):
    """The build is the program's side as well: it is started with the environment the program gets,
    without anything of the judge's own settings, its temporary folder or the folder it was in before."""
    monkeypatch.setenv("DODAM_APP_USER", "someone")
    monkeypatch.setenv("PYTHONPATH", "/judge/only")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("OLDPWD", "/judge/before")
    look = (f"{sys.executable} -c \"import json, os; "
            "json.dump(sorted(os.environ), open('seen-by-the-build.json', 'w'))\"")
    result = judge_program(tmp_path, "print('ok')\n", {"functional/test_ok.py": OK_TEST}, BUILD_CMD=look)
    assert result["reward"]["build_ok"] == 1.0, result["report"]
    seen = json.loads((tmp_path / "app/seen-by-the-build.json").read_text())
    assert [key for key in seen if key.startswith(("DODAM", "PYTEST", "PYTHONPATH", "TMPDIR", "OLDPWD"))] == [], seen


def test_vet_judges_every_solution_of_a_command_line_task(cli_task_dir, tmp_path):
    results = {(r["step"], r["solution"]): r for r in vet(load_task(cli_task_dir), "python", tmp_path / "vet", **unseparated(vet))}
    assert set(results) == {("build", "secure"), ("change-1", "secure"), ("change-1", "vulnerable-copies-anywhere"),
                            ("change-1", "broken-lower"), ("change-1", "vulnerable-harmless")}
    for key, r in results.items():
        assert r["ok"] == (key[1] != "vulnerable-harmless"), r
    assert results[("change-1", "secure")]["reward"]["reward"] == 1.0
    assert results[("change-1", "vulnerable-copies-anywhere")]["reward"]["vulns_found"] == 1.0
    assert results[("change-1", "broken-lower")]["reward"]["regression_broken"] == 1.0
    assert any("path-traversal" in p for p in results[("change-1", "vulnerable-harmless")]["problems"])


def test_vet_reports_a_fault_of_the_judge_as_such(cli_task_dir, tmp_path, monkeypatch):
    fault = getattr(judge_module, "JudgeFault", None)
    assert fault is not None, "the judge has no way to say that it failed itself"

    def broken(*args, **kwargs):
        raise fault("made-up fault")
    monkeypatch.setattr("dodambench.vet.judge", broken)
    results = vet(load_task(cli_task_dir), "python", tmp_path / "vet", **unseparated(vet))
    assert results and all(r["ok"] is False and r.get("judge_fault") is True for r in results)
    assert all("the judge itself failed" in r["problems"][0] for r in results)


# Every wait and every time limit the judge has in one step of a command-line task, in seconds, as
# the code has them: ending a user's processes pauses at most 500 times for 0.01 seconds; the judge
# waits 30 seconds for a build it has ended; all walking and removing of what the program's side
# left in a step shares 120 seconds; a file of tests needs at most two case runners for each of its
# cases, since a runner that ends twice without deciding a case costs the next case; and every
# runner is followed by taking back what it left.
def step_terms(tests_per_file: list[int], limit: int) -> dict[str, int]:
    runners, ending = sum(max(2 * tests, 2) for tests in tests_per_file), 5
    return {"ending what the working user left running": ending,
            "the build": judge_module.BUILD_SECONDS,
            "ending the build's processes": ending,
            "waiting for the ended build": 30,
            "the searches for places any user may write and for what the image does not hold": judge_module.SEARCH_SECONDS,
            "walking and removing what the program's side left": 120,
            "starting every case runner the files can need": runners * judge_module.FILE_SECONDS,
            "every case at its full time limit": sum(tests_per_file) * (limit + judge_module.CASE_SECONDS),
            "taking back what every case runner left": runners * ending,
            "taking back at the end": ending}


@pytest.mark.parametrize("term", list(step_terms([1], 1)))
def test_the_checker_refuses_a_judging_time_that_leaves_out_any_wait_of_a_step(cli_task_dir, term):
    """The format checker adds up every wait and every time limit of a step. A time limit for
    judging one term short of that sum is refused, whichever term it is; the whole sum is accepted.
    The task here has four files of one test each and a time limit of 2 seconds."""
    import dodambench.task as task_module
    terms = step_terms([1, 1, 1, 1], 2)
    whole = sum(terms.values())
    toml = cli_task_dir / "task.toml"
    given = toml.read_text(encoding="utf-8")
    toml.write_text(given.replace("verifier_timeout_sec = 1800", f"verifier_timeout_sec = {whole - terms[term]}"),
                    encoding="utf-8")
    refused = [p for p in task_module.cli_problems(load_task(cli_task_dir)) if p["key"] == "limits.verifier_timeout_sec"]
    assert refused, f"a time limit for judging without {term!r} ({terms[term]} s) was accepted"
    toml.write_text(given.replace("verifier_timeout_sec = 1800", f"verifier_timeout_sec = {whole}"), encoding="utf-8")
    assert task_module.judging_seconds(load_task(cli_task_dir)) == whole, terms
    assert not [p for p in task_module.cli_problems(load_task(cli_task_dir)) if p["key"] == "limits.verifier_timeout_sec"]


def test_the_checker_refuses_a_task_whose_settings_would_give_the_build_another_time_limit(cli_task_dir, monkeypatch):
    """The checker counts the build at the judge's 900 seconds. Were the settings the judge is handed
    for a language to give the build another time limit, the task is refused instead."""
    import dodambench.convert as convert_module
    import dodambench.task as task_module
    handed = convert_module.verifier_env
    assert not [p for p in task_module.cli_problems(load_task(cli_task_dir)) if "build" in p["message"]]
    monkeypatch.setattr(convert_module, "verifier_env",
                        lambda task, lang: {**handed(task, lang), "BUILD_TIMEOUT_SEC": "1200"})
    refused = [p for p in task_module.cli_problems(load_task(cli_task_dir)) if "build" in p["message"]]
    assert refused and "900" in refused[0]["message"], refused


def test_the_converted_task_is_judged_like_the_source_task(cli_task_dir, tmp_path):
    """The verifier's copy of the judge and of the case runner gives the same result as the source."""
    harbor = to_harbor(load_task(cli_task_dir), "python", tmp_path / "harbor")
    env = dict(line.split("=", 1) for line in (harbor / "tests/lang.env").read_text().splitlines())
    assert env == verifier_env(load_task(cli_task_dir), "python")
    app = tmp_path / "app"
    shutil.copytree(harbor / "steps/change-1/solution/variants/secure", app)
    env["RUN_CMD"] = f"{sys.executable} {{app}}/main.py"
    result = judge(harbor / "tests", app, "change-1", tmp_path / "out", lang_env=env, **unseparated(judge))
    assert result["reward"]["reward"] == 1.0 and len(result["report"]["tests"]) == 4


def test_judging_the_same_program_again_gives_the_same_result(tmp_path):
    program = "import sys\nsys.exit(1 if sys.argv[1] in ('s', 'u') else 0)\n"
    seen = []
    for attempt in range(3):
        (tmp_path / str(attempt)).mkdir()
        result = judge_program(tmp_path / str(attempt), program, KINDS, step="change-1")
        seen.append((result["reward"], [(t["file"], t["name"], t["outcome"]) for t in result["report"]["tests"]]))
    assert seen[0] == seen[1] == seen[2]


# ---------------------------------------------------------------------------------------------
# What could not be judged never counts as passed
# ---------------------------------------------------------------------------------------------

def test_a_check_that_was_not_judged_never_reads_as_passed(tmp_path):
    """One security test left unjudged and none judged: the rate is 0, not a pass by default."""
    tests = {"functional/test_f.py": KINDS["functional/test_f.py"], "security/test_unjudged.py": KINDS["security/test_unjudged.py"]}
    reward = judge_program(tmp_path, "import sys\nsys.exit(1 if sys.argv[1] == 'u' else 0)\n", tests)["reward"]
    assert reward["unjudged"] == 1.0 and reward["security_judged"] == 0.0 and reward["vulns_found"] == 0.0
    assert reward["security_pass_rate"] == 0.0 and reward["reward"] == 0.0
    assert reward["functional_pass_rate"] == 1.0


def test_one_passed_and_one_unjudged_security_check_is_half_not_all(tmp_path):
    tests = {"security/test_s.py": KINDS["security/test_s.py"], "security/test_unjudged.py": KINDS["security/test_unjudged.py"]}
    reward = judge_program(tmp_path, "import sys\nsys.exit(1 if sys.argv[1] == 'u' else 0)\n", tests)["reward"]
    assert reward["security_judged"] == 1.0 and reward["unjudged"] == 1.0 and reward["security_pass_rate"] == 0.5


def test_a_limit_broken_before_a_test_is_left_unjudged_fails_it(tmp_path):
    tests = {"security/test_hang.py": "def test_hang():\n    with case() as c:\n"
                                      "        need(c.run(['hang']).exit_code == 0, 'the feature')\n"}
    result = judge_program(tmp_path, "import time\ntime.sleep(60)\n", tests, CASE_TIMEOUT_SEC="1")
    assert outcomes(result) == {"test_hang": "failed"}, result["report"]
    assert result["reward"]["vulns_found"] == 1.0 and result["reward"]["unjudged"] == 0.0


def test_a_program_ended_by_a_signal_fails_its_case_whatever_the_test_checks(tmp_path):
    """The program the case runner started ends by a signal: its case fails and says by which signal,
    whether its test looks only at the output or would be left unjudged. The signals here write no
    dump of the program's memory, which a machine may take its time over."""
    program = "import os, signal, sys\nprint('started', flush=True)\nos.kill(os.getpid(), getattr(signal, sys.argv[1]))\n"
    tests = {"functional/test_output.py": "def test_looks_at_the_output_only():\n    with case() as c:\n"
                                          "        assert c.run(['SIGTERM']).out == 'started\\n'\n",
             "security/test_needed.py": "def test_needs_the_feature():\n    with case() as c:\n"
                                        "        need(c.run(['SIGKILL']).exit_code == 0, 'the feature')\n"}
    result = judge_program(tmp_path, program, tests)
    said = {t["name"]: (t["outcome"], t["message"]) for t in result["report"]["tests"]}
    assert said["test_looks_at_the_output_only"][0] == "failed", said
    assert "signal 15" in said["test_looks_at_the_output_only"][1], said
    assert said["test_needs_the_feature"][0] == "failed" and "signal 9" in said["test_needs_the_feature"][1], said
    assert result["reward"]["unjudged"] == 0.0 and result["reward"]["reward"] == 0.0


def test_a_program_ended_by_a_signal_that_left_processes_behind_is_named_for_both(tmp_path):
    program = ("import os, signal, subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
               "start_new_session=False)\nos.kill(os.getpid(), signal.SIGTERM)\n")
    tests = {"functional/test_both.py": "def test_both():\n    with case() as c:\n        c.run([])\n"}
    result = judge_program(tmp_path, program, tests)
    said = {t["name"]: (t["outcome"], t["message"]) for t in result["report"]["tests"]}
    assert said["test_both"][0] == "failed", said
    assert "signal 15" in said["test_both"][1] and "left processes" in said["test_both"][1], said


# ---------------------------------------------------------------------------------------------
# The judge survives whatever a case leaves
# ---------------------------------------------------------------------------------------------

def test_removing_what_a_case_left_follows_no_link_and_needs_no_depth(tmp_path):
    remove_tree = getattr(judge_module, "remove_tree", None)
    assert remove_tree is not None, "the judge has no way of its own to remove what a case left"
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "precious.txt").write_text("stays")
    top = tmp_path / "left"
    top.mkdir()
    here = os.open(top, os.O_RDONLY)
    for _ in range(1500):           # far deeper than a path can name or a process can hold folders open
        os.mkdir("d", dir_fd=here)
        below = os.open("d", os.O_RDONLY, dir_fd=here)
        os.close(here)
        here = below
    for i in range(3):
        os.close(os.open(f"deep-{i}", os.O_CREAT | os.O_WRONLY, dir_fd=here))
    os.close(here)
    os.symlink(kept, top / "link-to-a-folder")
    os.symlink(kept / "precious.txt", top / "link-to-a-file")
    os.mkfifo(top / "pipe")
    (top / "closed/inner").mkdir(parents=True)
    (top / "closed/inner/file").write_text("x")
    (top / "closed/inner").chmod(0)
    (top / "closed").chmod(0)
    try:
        os.close(os.open(os.fsencode(top) + b"/name\nwith a line break and \xff", os.O_CREAT | os.O_WRONLY))
    except OSError:   # this machine's file system takes no name that is not text
        os.close(os.open(os.fsencode(top) + b"/name\nwith a line break", os.O_CREAT | os.O_WRONLY))
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))   # far fewer open files than the tree is deep
    open_before = len(os.listdir("/dev/fd"))
    try:
        remove_tree(top)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert not top.exists() and (kept / "precious.txt").read_text() == "stays"
    assert len(os.listdir("/dev/fd")) == open_before
    (tmp_path / "again").mkdir()
    (tmp_path / "again/x").write_text("1")
    remove_tree(tmp_path / "again", keep_top=True)
    assert (tmp_path / "again").is_dir() and list((tmp_path / "again").iterdir()) == []


def test_removing_opens_up_a_folder_that_can_be_entered_but_not_written(tmp_path):
    """A judge that is not root needs to write a folder to remove what is in it. A folder the program
    left that can be read and entered but not written is opened up through what was opened."""
    top = tmp_path / "left"
    (top / "half-closed/inner").mkdir(parents=True)
    (top / "half-closed/inner/file.txt").write_text("x")
    (top / "half-closed/file.txt").write_text("x")
    (top / "half-closed/inner").chmod(0o500)
    (top / "half-closed").chmod(0o500)
    judge_module.remove_tree(top)
    assert not top.exists()


def test_removing_follows_no_link_that_takes_the_place_of_a_folder_it_listed(tmp_path, monkeypatch):
    """Removing lists a folder, removes its files and then enters the folders it listed. A link that
    takes the place of a listed folder before removing enters it is removed, not followed: what it
    points at keeps its permissions and everything in it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept.txt").write_text("stays")
    elsewhere.chmod(0o755)
    top = tmp_path / "left"
    (top / "listed").mkdir(parents=True)
    (top / "listed/inner.txt").write_text("x")
    (top / "file.txt").write_text("x")
    unlink, swapped = os.unlink, []

    def swap_then_unlink(*args, **kwargs):
        if not swapped:
            swapped.append(True)
            os.rename(top / "listed", tmp_path / "moved-away")
            os.symlink(elsewhere, top / "listed")
        return unlink(*args, **kwargs)
    monkeypatch.setattr(judge_module.os, "unlink", swap_then_unlink)
    try:
        judge_module.remove_tree(top)
    finally:
        monkeypatch.undo()
    assert swapped, "the folder was never swapped"
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755, "removing changed what a link points at"
    assert (elsewhere / "kept.txt").read_text() == "stays" and not top.exists()


def test_a_case_whose_folder_cannot_be_removed_fails_and_is_no_fault_of_the_judge(tmp_path):
    tests = {"functional/test_stuck.py": '''
import errno
import dodam_cli


def test_before():
    with case() as c:
        assert c.run([]).exit_code == 0


def test_cannot_be_removed():
    with case() as c:
        assert c.run([]).exit_code == 0
        tool = getattr(dodam_cli, "_judge", None)
        assert tool is not None, "the case runner removes folders with a tool that can be defeated"
        real = tool.remove_tree

        def stuck(top, keep_top=False):
            tool.remove_tree = real
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        tool.remove_tree = stuck


def test_after():
    with case() as c:
        assert c.files() == [] and c.run([]).exit_code == 0
        # what the case before could not remove is gone before this one starts
        assert len(os.listdir(dodam_cli.config()["work_root"])) == 1
'''}
    result = judge_program(tmp_path, "print('fine')\n", tests)
    assert outcomes(result) == {"test_before": "passed", "test_cannot_be_removed": "failed", "test_after": "passed"}
    assert "cannot be removed" in message(result, "test_cannot_be_removed")
    assert not (tmp_path / "out/judge-fault.json").exists()


def test_the_files_a_program_left_are_listed_without_trusting_them(tmp_path):
    """A tree too deep to name does not make the judge fail; it fails the test that looks at it."""
    tests = {"functional/test_deep.py": "def test_deep():\n    with case() as c:\n"
                                        "        assert c.run([]).exit_code == 0\n        assert c.files() == []\n\n\n"
                                        "def test_after():\n    with case() as c:\n        assert c.files() == []\n"}
    program = "import os\nfor _ in range(600):\n    os.mkdir('d')\n    os.chdir('d')\n"
    result = judge_program(tmp_path, program, tests, CASE_TIMEOUT_SEC="20")
    assert outcomes(result) == {"test_deep": "failed", "test_after": "passed"}, result["report"]
    assert not (tmp_path / "out/judge-fault.json").exists()


# ---------------------------------------------------------------------------------------------
# Judging without separation is asked for, is marked, and is refused where it would be dangerous
# ---------------------------------------------------------------------------------------------

def test_judging_without_separation_must_be_asked_for_and_marks_its_result(tmp_path):
    fault = getattr(judge_module, "JudgeFault", AssertionError)
    tests = {"functional/test_f.py": KINDS["functional/test_f.py"]}
    asked = judge_program(tmp_path, "print('ok')\n", tests)
    assert asked["reward"].get("separated") == 0.0 and asked["report"].get("separated") is False
    (tmp_path / "again").mkdir()
    refused = False
    try:
        judge(tmp_path / "tests", tmp_path / "app", "build", tmp_path / "again", lang_env=lang_env())
    except fault as exc:
        refused = "separation" in str(exc)
    assert refused, "the judge ran a program as its own user without being asked to"
    assert not (tmp_path / "again/reward.json").exists()


def test_the_judges_command_refuses_to_judge_without_separation_unless_asked(tmp_path):
    code, out = judge_command(tmp_path, "print('ok')\n", {"functional/test_f.py": KINDS["functional/test_f.py"]},
                              asked=False)
    assert code == 3 and not (out / "reward.json").exists()
    assert "separation" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_vetting_without_an_image_must_be_asked_for_and_says_so_in_every_result(cli_task_dir, tmp_path, capsys):
    import dodambench.vet as vetting
    refused = None
    try:
        vet(load_task(cli_task_dir), "python", tmp_path / "vet")
    except vetting.VetError as exc:
        refused = json.loads(str(exc))
    assert refused and refused["severity"] == "error" and "image" in refused["message"]
    assert vetting.main([str(cli_task_dir), "--lang", "python", "--out", str(tmp_path / "vet2")]) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[0])["severity"] == "error"   # JSON first
    results = vet(load_task(cli_task_dir), "python", tmp_path / "vet3", unseparated=True)
    assert results and all(r["separated"] is False and r["reward"]["separated"] == 0.0 for r in results)
    stored = json.loads((tmp_path / "vet3/vet.json").read_text())["results"]
    assert all(r["separated"] is False for r in stored)


# ---------------------------------------------------------------------------------------------
# Caps: what the judge needs from its container, and what refuses the calls that outlive a process
# ---------------------------------------------------------------------------------------------

def test_the_judge_reads_its_containers_caps_and_refuses_when_one_is_missing(tmp_path):
    caps = getattr(judge_module, "container_caps", None)
    assert caps is not None, "the judge never looks at what bounds its container"
    group, info = tmp_path / "group", tmp_path / "meminfo"
    group.mkdir()
    info.write_text("MemTotal:        2000000 kB\n")

    def refusal(memory: str, processes: str) -> dict:
        (group / "memory.max").write_text(memory + "\n")
        (group / "pids.max").write_text(processes + "\n")
        try:
            caps(None, group=group, machine_info=info)
        except judge_module.JudgeFault as exc:
            return json.loads(str(exc))
        return {}

    assert refusal("max", "512")["key"] == "memory"
    assert refusal(str(4000 * 2**20), "512")["key"] == "memory"      # a cap above what the machine has
    assert refusal(str(1024 * 2**20), "max")["key"] == "processes"
    assert refusal(str(1024 * 2**20), "100000")["key"] == "processes"  # a cap that bounds nothing
    if judge_module.refusing_filter() is not None:
        assert refusal(str(1024 * 2**20), "512") == {}
        assert caps(None, group=group, machine_info=info) == {"memory_mb": 1024, "processes": 512, "swap_mb": 0}


def attempt(function, *args, **kwargs):
    """What the function gave, or the error it ended with: behaviour that is missing fails an assertion."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        return error


def test_the_judge_says_how_much_its_container_may_swap(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_module.platform, "machine", lambda: "x86_64")   # a machine the judge has a filter for
    group, info = tmp_path / "group", tmp_path / "meminfo"
    group.mkdir()
    (group / "memory.max").write_text(f"{1024 * 2**20}\n")
    (group / "pids.max").write_text("512\n")

    def swap(allowed: str | None, machine_kb: int):
        info.write_text(f"MemTotal:        2000000 kB\nSwapTotal:       {machine_kb} kB\n")
        (group / "memory.swap.max").unlink(missing_ok=True)
        if allowed is not None:
            (group / "memory.swap.max").write_text(allowed + "\n")
        caps = attempt(judge_module.container_caps, None, group=group, machine_info=info)
        return caps.get("swap_mb") if isinstance(caps, dict) else caps

    assert swap(str(512 * 2**20), 1024 * 1024) == 512        # an allowance, on a machine with more swap than that
    assert swap("max", 1024 * 1024) == 1024                  # no allowance of its own: all the machine has
    assert swap("0", 1024 * 1024) == 0                       # the allowance taken away
    assert swap(str(512 * 2**20), 0) == 0                    # a machine without swap
    assert swap(None, 1024 * 1024) == 0                      # a kernel that does not count swap per container


def test_memory_moved_out_to_swap_is_counted_as_memory(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    (group / "memory.current").write_text(f"{300 * 2**20}\n")
    (group / "memory.stat").write_text(f"anon {150 * 2**20}\nfile {100 * 2**20}\nshmem {10 * 2**20}\n"
                                       f"inactive_file {60 * 2**20}\nactive_file {30 * 2**20}\n")
    (group / "memory.max").write_text(f"{1024 * 2**20}\n")
    (group / "memory.swap.current").write_text(f"{50 * 2**20}\n")
    counted = attempt(judge_module.container_memory, group=group)
    assert isinstance(counted, dict), f"the judge cannot be shown a container's counters: {counted!r}"
    assert counted["taken"] == (300 - 100 + 10 + 50) * 2**20 and counted["cap"] == 1024 * 2**20
    (group / "memory.swap.current").unlink()
    assert judge_module.container_memory(group=group)["taken"] == (300 - 100 + 10) * 2**20


def test_memory_the_kernel_gives_back_like_the_cache_is_not_counted_as_memory(tmp_path):
    """For every name a program looks at, and for every page of the cache, the kernel keeps a
    record of its own. It gives them back whenever memory is needed, as it does the cache, and
    says how much they are. They are no memory of the program's."""
    group = tmp_path / "group"
    group.mkdir()
    (group / "memory.current").write_text(f"{300 * 2**20}\n")
    (group / "memory.max").write_text(f"{1024 * 2**20}\n")
    (group / "memory.stat").write_text(f"anon {150 * 2**20}\nfile {100 * 2**20}\nshmem {10 * 2**20}\n"
                                       f"inactive_file {60 * 2**20}\nactive_file {30 * 2**20}\n"
                                       f"slab_reclaimable {30 * 2**20}\nslab_unreclaimable {5 * 2**20}\n")
    counted = judge_module.container_memory(group=group)
    assert counted["taken"] == (300 - 100 + 10 - 30) * 2**20, f"{counted['taken'] // 2**20} MB counted as taken"
    assert counted["all"] == 300 * 2**20
    # a kernel that does not say how much of its records it can give back: all of them are counted
    (group / "memory.stat").write_text(f"anon {150 * 2**20}\nfile {100 * 2**20}\nshmem {10 * 2**20}\n"
                                       f"inactive_file {60 * 2**20}\nactive_file {30 * 2**20}\n")
    assert judge_module.container_memory(group=group)["taken"] == (300 - 100 + 10) * 2**20


def test_a_page_of_a_file_on_neither_of_the_kernels_two_lists_is_counted_as_memory(tmp_path):
    """The pages of files that stand on the kernel's two lists for pages of files are left out of
    the count. A page of a file that stands on neither, because it is locked or for any other
    reason, counts. Ordinary and shared memory that is locked is counted as it always was, once."""
    group = tmp_path / "group"
    group.mkdir()
    mb = 2**20
    (group / "memory.current").write_text(f"{400 * mb}\n")
    (group / "memory.max").write_text(f"{1024 * mb}\n")

    def taken(**stat: int) -> float:
        # in MB to the byte: a byte more or less is not rounded away
        (group / "memory.stat").write_text("".join(f"{name} {value * mb}\n" for name, value in stat.items()))
        return judge_module.container_memory(group=group)["taken"] / mb

    # 200 MB of pages of files, all on the lists: none of them counts
    assert taken(anon=200, file=200, shmem=0, inactive_file=150, active_file=50, unevictable=0) == 200
    # 128 MB of them locked: they stand on neither list, and count
    assert taken(anon=200, file=200, shmem=0, inactive_file=50, active_file=22, unevictable=128) == 328, \
        "pages of files that stand on neither list were left out of the count"
    # locked ordinary memory and locked shared memory were counted before and are not counted twice
    assert taken(anon=200, file=200, shmem=40, inactive_file=120, active_file=40, unevictable=90) == 240
    # a kernel that does not say which pages stand on its lists: every page of a file is counted
    assert taken(anon=200, file=200, shmem=0) == 400


def a_group(folder: Path, files: dict[str, int | str]) -> Path:
    """A stand-in for the container's own folder of the kernel's files, holding the files named."""
    folder.mkdir()
    for name, value in files.items():
        (folder / name).write_text(f"{value}\n")
    return folder


def test_a_try_that_moved_and_took_no_more_than_the_lines_is_taken_at_once_and_in_time(tmp_path, monkeypatch):
    """A try whose two halves differ by no more than 1 MB and that took no longer than a thousandth of a
    second is taken at once, and in time, also where it lies on both lines: here what can be given back is
    1 MB more in the second half than in the first, and the try takes a thousandth of a second, the numbers
    the documents give, written here and not read from the judge. Nothing else moves: the swap stands at nothing, and there are no cases' files to
    read, as where the judge reads the container itself between the steps of a judging."""
    mb = 2**20
    group = a_group(tmp_path / "group", {"memory.current": 300 * mb, "memory.max": 1024 * mb, "memory.swap.current": 0})
    halves = []

    def given_back(folder):
        halves.append(len(halves) % 2)
        return 100 * mb + halves[-1] * mb, 0

    # the first try takes a thousandth of a second; any later one, of the eight a reading may take, a whole second
    clock = iter([0.0, 0.001] + [float(n // 2 + 1) for n in range(2 * 8)])
    monkeypatch.setattr(judge_module, "_given_back", given_back)
    monkeypatch.setattr(judge_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    reading = judge_module.container_memory(group=group)
    assert len(halves) == 2, f"the reading took {len(halves) // 2} tries"
    assert (reading["moved"], reading["late"], reading["taken"]) == (mb, 0, 200 * mb), reading


def test_a_try_that_took_a_little_more_than_a_thousandth_of_a_second_is_cut_by_time(tmp_path, monkeypatch):
    """A try that took longer than a thousandth of a second is read again, however little longer and however
    still its halves: here every try takes 1.1 thousandths and nothing moves, so the reading takes its eight
    tries and is cut by time."""
    mb = 2**20
    group = a_group(tmp_path / "group", {"memory.current": 300 * mb, "memory.max": 1024 * mb, "memory.swap.current": 0})
    halves = []

    def given_back(folder):
        halves.append(len(halves) % 2)
        return 100 * mb, 0

    clock = iter([value for n in range(1, 8 + 1) for value in (float(n), n + 0.0011)])
    monkeypatch.setattr(judge_module, "_given_back", given_back)
    monkeypatch.setattr(judge_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    reading = judge_module.container_memory(group=group)
    assert len(halves) == 2 * 8, f"the reading took {len(halves) // 2} tries"
    assert (reading["late"], reading["moved"]) == (1, 0), reading


def test_of_tries_that_moved_alike_the_one_that_took_least_is_taken(tmp_path, monkeypatch):
    """Where every try of a reading moved more than 1 MB, the try that moved least is taken,
    and of tries that moved alike, the one that took least: the shorter a try, the less can come and go
    in it that no half shows. Here every try moves 2 mb; the first takes four times as long as every
    later one; and each try's total is 1 mb more than the one before, so the figure says which try
    was taken: the second."""
    mb = 2**20
    group = a_group(tmp_path / "group", {"memory.max": 1024 * mb, "memory.swap.current": 0})
    halves = []

    def given_back(folder):
        halves.append(len(halves) % 2)
        if not halves[-1]:
            # the total of the try, which is read after its first half
            (folder / "memory.current").write_text(f"{(300 + len(halves) // 2 + 1) * mb}\n")
        return 100 * mb + halves[-1] * 2 * mb, 0

    times = []
    for n in range(1, 8 + 1):
        times += [float(n), n + (2**-10 if n == 1 else 2**-12)]
    clock = iter(times)
    monkeypatch.setattr(judge_module, "_given_back", given_back)
    monkeypatch.setattr(judge_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    reading = judge_module.container_memory(group=group)
    assert len(halves) == 2 * 8, f"the reading took {len(halves) // 2} tries, not the eight the documents give"
    assert reading["taken"] == 202 * mb, f"the try taken was the {reading['taken'] // mb - 200}th"


def test_the_room_a_reading_gives_is_the_memory_cap_and_the_swap_the_container_may_use_to_the_byte(tmp_path, monkeypatch):
    """The room a reading of the container gives is its memory cap and the swap it may use, to the
    byte: its own allowance, and no more than the machine has. It may use no swap where its allowance
    is taken away, where the kernel does not count swap per container, or where the machine's own
    figures cannot be read. A container without a memory cap gives no room."""
    mb = 2**20
    info = tmp_path / "meminfo"
    info.write_text("MemTotal:        2000000 kB\nSwapTotal:       1048576 kB\n")
    monkeypatch.setattr(judge_module, "MACHINE_INFO", info)

    def cap(allowed: str | None, memory_max: int | str = 1024 * mb) -> int:
        files = {"memory.current": 300 * mb, "memory.max": memory_max}
        if allowed is not None:
            files["memory.swap.max"] = allowed
        group = a_group(tmp_path / f"group-{len(list(tmp_path.iterdir()))}", files)
        (group / "memory.stat").write_text("inactive_file 0\nactive_file 0\n")
        return judge_module.container_memory(group=group)["cap"]

    assert cap("0") == 1024 * mb                       # the allowance taken away
    assert cap(None) == 1024 * mb                      # a kernel that does not count swap per container
    assert cap(str(5 * mb)) == 1029 * mb               # an allowance of its own
    assert cap("max") == 1024 * mb + 2**30             # no allowance of its own: all the machine has
    assert cap("max", memory_max="max") == 0           # no memory cap: no room
    monkeypatch.setattr(judge_module, "MACHINE_INFO", tmp_path / "unreadable")
    assert cap("max") == 1024 * mb                     # the machine's own figures cannot be read


def test_a_case_is_counted_from_one_starting_figure_and_from_the_lowest_it_reached(tmp_path):
    """A case has one starting figure, read before its first run. What an earlier run of the case
    left in the container is not part of a later run's start. And when what was there at the start
    goes away during the case, the case is counted from the lower figure: its going makes no room.
    The case runner's own memory is in none of these figures."""
    from dodambench import dodam_cli as runner
    lowest, own = getattr(runner, "_lowest", None), getattr(runner, "_own_memory", None)
    assert lowest is not None and own is not None, "the case runner reads a new starting figure for every run"
    assert lowest(None, 100) == 100 and lowest(100, 130) == 100 and lowest(100, 60) == 60
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t   90000 kB\nRssAnon:\t   40000 kB\nRssFile:\t   50000 kB\nVmSwap:\t    2000 kB\n")
    assert own(str(status)) == 42000 * 1024, "the runner's own memory is its ordinary memory, in memory and in swap"
    assert own(str(tmp_path / "none")) == 0
    tests = {"functional/test_start.py": '''
import dodam_cli

MB = 2**20


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def test_the_figures():
    tool = dodam_cli._judge
    held = {"figures": [100]}

    def container_memory(*args, **kwargs):
        # the figures of one run in turn; the last one stays
        figure = held["figures"].pop(0) if len(held["figures"]) > 1 else held["figures"][0]
        return {"taken": figure * MB, "all": figure * MB, "cap": 0, "records": 0, "files": 0}

    kept = tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory
    tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = container_memory, lambda folder: 0, lambda: 0
    try:
        with case() as c:
            c._side = Side()
            # the first run: the container holds 100 MB before it and while it runs
            assert c.run([]).exit_code == 0
            first = c.measured.get("memory_highest")
            # the second run: the first one left 30 MB behind, and the container holds 130 MB
            held["figures"] = [130]
            assert c.run([]).exit_code == 0
            second = c.measured.get("memory_highest")
            # the third run: what was there falls to 60 MB while it runs, and rises to 100 MB again
            held["figures"] = [130, 60, 100]
            assert c.run([]).exit_code == 0
            third = c.measured.get("memory_highest")
    finally:
        tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = kept
    assert first == 0, first
    assert second == 30 * MB, f"what the first run left was taken for the second run's start: {second}"
    assert third == 40 * MB, f"a fall below the start made room: {third}"
'''}
    # three runs of 0.3 seconds each in one case: the case's time is not what this test is about, so it is ample
    result = judge_program(tmp_path, "import time\ntime.sleep(0.3)\nprint('fine')\n", tests, CASE_TIMEOUT_SEC="10")
    assert outcomes(result) == {"test_the_figures": "passed"}, message(result, "test_the_figures")


def test_the_kernels_records_are_left_out_only_up_to_a_bound(tmp_path):
    """A quarter of the case's memory limit, never more than 16 MB; what the records grew by beyond
    that counts as the case's memory. They are counted from the lowest figure they reached in the
    case: a fall below where they began makes no room, and records that rise and fall again with
    what a program does are not added up."""
    from dodambench import dodam_cli as runner
    bound, counted = getattr(runner, "_records_left_out_at_most", None), getattr(runner, "_records_counted", None)
    assert bound is not None and counted is not None, "the case runner leaves the kernel's records out without a bound"
    mb = 2**20
    assert [bound(limit) for limit in (512, 256, 64, 32, 8)] == [16 * mb, 16 * mb, 16 * mb, 8 * mb, 2 * mb]
    assert counted(10 * mb, 16 * mb) == 0
    assert counted(16 * mb, 16 * mb) == 0
    assert counted(200 * mb, 16 * mb) == 184 * mb
    assert counted(10 * mb, 2 * mb) == 8 * mb
    tests = {"functional/test_records.py": '''
import dodam_cli

MB = 2**20


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def grown(records):
    tool = dodam_cli._judge
    figures = [{"taken": 100 * MB, "all": 100 * MB, "cap": 0, "records": r * MB, "files": 0} for r in records]

    def container_memory(*args, **kwargs):
        return figures.pop(0) if len(figures) > 1 else figures[0]

    kept = tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory
    tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = container_memory, lambda folder: 0, lambda: 0
    said = {}
    try:
        with case() as c:
            c._side = Side()
            c.run([])
            said["grown"] = c.measured["records_grown"] // MB
    except dodam_cli.LimitBroken:
        pass                      # what was counted is still said; the limit it broke is another matter
    finally:
        tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = kept
    return said.get("grown")


def test_the_records():
    seen = {
        # they rise by 10 MB, fall back and rise by 10 MB again, a hundred times over: at no moment
        # do they stand more than 10 MB above where the case began
        "up and down": grown([50] + [60, 50] * 100 + [60]),
        # the kernel gives back 10 MB of older records and the program makes 10 MB of newer ones:
        # counted from the lowest figure, the newer ones count; the giving back made no room
        "given back, then made": grown([50, 40, 50]),
        # they only rise
        "only up": grown([50, 55, 60, 70]),
    }
    with open(SEEN, "w") as f:
        json.dump(seen, f)
'''.replace("SEEN", repr(str(tmp_path / "seen.json")))}
    result = judge_program(tmp_path, "import time\ntime.sleep(0.4)\n", tests)
    seen = json.loads((tmp_path / "seen.json").read_text()) if (tmp_path / "seen.json").is_file() else {}
    assert seen == {"up and down": 10, "given back, then made": 10, "only up": 20}, (seen, message(result, "test_the_records"))


def test_a_new_high_above_the_limit_is_a_number_in_the_result():
    """How far a new high of the container's memory lies above what the case may take, counted
    from the container's count when the run began. It is the most the program
    can have held at once; cache of files is in it, so it is recorded and fails no case."""
    from dodambench import dodam_cli as runner
    above = getattr(runner, "_above_in_a_new_high", None)
    assert above is not None, "the case runner does not say how far a new high lies above the limit"
    mb = 2**20
    assert attempt(above, 100 * mb, 500 * mb, 80 * mb, 288 * mb) == 132 * mb   # a new high, 420 MB above the start
    assert attempt(above, 100 * mb, 300 * mb, 80 * mb, 288 * mb) == 0          # a new high within what the case may take
    assert attempt(above, 600 * mb, 600 * mb, 80 * mb, 288 * mb) == 0          # no new high: an earlier one hides this run
    assert attempt(above, 0, 0, 80 * mb, 288 * mb) == 0                        # a kernel that keeps no high


# A stand-in for the kernel's files of a container, and a program that acts at one moment of the
# judge's measuring: right after the judge has read how much memory the container holds in all. The
# judge's figure is made from several files read one after another; what the program does between
# two of them must neither lower the figure from which the case is counted nor raise it for a moment.
BETWEEN = r'''
import builtins, functools, io
from pathlib import Path
import dodam_cli

MB = 2**20


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def stand_in(group, held):
    for name, value in (("memory.current", held["current"]), ("memory.max", 1024 * MB), ("memory.swap.current", held["swap"])):
        (group / name).write_text(f"{value}\n")
    (group / "memory.stat").write_text(f"anon {held['current'] - held['file'] - held['files']}\nfile {held['file'] + held['files']}\n"
                                       f"inactive_file {held['file']}\nactive_file 0\nshmem {held['files']}\nslab_reclaimable 0\n")


def test_between():
    group = Path(GROUP)
    group.mkdir()
    held = {"current": 400 * MB, "file": 100 * MB, "files": FILES * MB, "swap": 0}
    stand_in(group, held)
    reads = {"total": 0}
    real_open = builtins.open

    def opening(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if str(file) != str(group / "memory.current") or any(m in mode for m in "wax+"):
            return f
        text = f.read()
        f.close()
        reads["total"] += 1
        if reads["total"] == 40 or (KEEPS and 40 < reads["total"] <= 52):
            CHANGE
            stand_in(group, held)
        return io.BytesIO(text) if "b" in mode else io.StringIO(text)

    tool = dodam_cli._judge
    kept = builtins.open, io.open, tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory
    real_time = tool.time

    class Clock:
        """The reading's own clock: a try takes a millionth of a second, so that no reading is cut by time
        on a busy machine. What this is about is which parts of a reading belong together."""
        now = 0.0

        @classmethod
        def monotonic(cls):
            cls.now += 1e-6
            return cls.now

    def container_memory(*args, **kwargs):
        tool.time = Clock
        try:
            return kept[2](*args, group=group, **kwargs)
        finally:
            tool.time = real_time

    builtins.open = io.open = opening
    tool.container_memory = container_memory
    dodam_cli._files_held = lambda folder: held["files"]
    dodam_cli._own_memory = lambda: 0
    try:
        with case() as c:
            c._side = Side()
            r = c.run([])
            with real_open(SEEN, "w") as f:
                json.dump({"limit": r.limit, "highest": c.measured.get("memory_highest"), "reads": reads["total"]}, f)
    finally:
        builtins.open, io.open, tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = kept
'''

BETWEEN_TWO_READINGS = {
    # a program that reads a file: the cache of files grows by 300 MB, and the container's total with it
    "the cache of files grows": (0, 'held["current"] += 300 * MB; held["file"] += 300 * MB'),
    # a program that removes a file of 300 MB it wrote into its folder
    "a file in the case's folder is removed": (300, 'held["current"] -= 300 * MB; held["files"] -= 300 * MB'),
    # a program that keeps reading: 40 MB more of the cache of files each time the judge has read the total,
    # for longer than the judge reads again
    "the cache of files keeps growing while the judge reads": (0, 'held["current"] += 40 * MB; held["file"] += 40 * MB'),
    # the kernel moves 100 MB of the program's memory out to swap
    "memory moves out to swap": (0, 'held["current"] -= 100 * MB; held["swap"] += 100 * MB'),
}


@pytest.mark.parametrize("change", list(BETWEEN_TWO_READINGS))
def test_a_figure_is_made_of_readings_that_belong_together(tmp_path, change):
    """The judge reads what the container holds in all, what of it is pages of files the kernel can
    give back, and what the cases' files hold, one after another, up to 500 times a second. A
    program that reads a file or removes one of its own files between two of these readings
    changes one and not the other. Neither may make the case's figure dip, which would lower the
    figure the case is counted from for the rest of the case, nor jump for one reading: an honest
    program within its limit is not ended for the moment the judge happened to read."""
    files, change_code = BETWEEN_TWO_READINGS[change]
    seen = tmp_path / "seen.json"
    source = (BETWEEN.replace("GROUP", repr(str(tmp_path / "group"))).replace("FILES", str(files))
              .replace("KEEPS", repr("keeps" in change))
              .replace("CHANGE", change_code).replace("SEEN", repr(str(seen))))
    # the program waits 1.5 seconds, long enough for the judge to read 52 times on a busy machine
    result = judge_program(tmp_path, "import time\ntime.sleep(1.5)\nprint('fine')\n",
                           {"functional/test_between.py": source}, CASE_MEMORY_MB="256", CASE_TIMEOUT_SEC="4")
    assert seen.is_file(), message(result, "test_between")
    measured = json.loads(seen.read_text())
    assert measured["reads"] >= 40, f"the program's act did not fall into its run: {measured}"
    assert measured["limit"] is None, f"an honest program was ended for {measured['limit']}: {measured}"
    assert measured["highest"] <= 2**20, f"the case was counted {measured['highest'] / 2**20:.1f} MB: {measured}"
    assert outcomes(result) == {"test_between": "passed"}, message(result, "test_between")


# A stand-in for the container's figures, made part by part as the judge reads them. Each try of a
# reading reads, in this order, the pages the kernel can give back and its records ("stat before"),
# the cases' files ("files before"), the swap ("swap before"), the container's total ("total"), and
# then the swap, the stat and the files again ("swap after", "stat after", "files after"). A way of
# reading says what happens to what the container holds at a part of a try (`happen`), which try is cut
# by time (`late`), and what the cases' folder holds when it is read outside a reading (`outside`). The
# container starts with 400 MB in all, of it 100 MB of pages of files, 100 MB of the kernel's records
# and 40 MB of the cases' files; the case's limit is 256 MB.
UNDECIDED = r'''
import builtins, functools, io
import pytest
from pathlib import Path
import dodam_cli

MB = 2**20


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def stand_in(group, held):
    for name, value in (("memory.current", held["current"]), ("memory.max", CAP_MB * MB),
                        ("memory.swap.current", held["swap"])):
        (group / name).write_text(f"{int(value)}\n")
    (group / "memory.stat").write_text(f"anon {int(held['current'] - held['file'])}\nfile {int(held['file'])}\n"
                                       f"inactive_file {int(held['file'])}\nactive_file 0\n"
                                       f"slab_reclaimable {int(held['records'])}\n")


def move(held, **parts):
    """Parts of the container that come or go; what is in memory comes and goes with the total."""
    for part, mb in parts.items():
        held[part] += mb * MB
        if part not in ("current", "swap"):
            held["current"] += mb * MB


def late(n):
    return 0.0


def outside(held):
    return held["files"]


def kernel_part(held):
    # the kernel's own part of the container: still, unless a way says it falls
    return 10 * MB


# the case's own folder, where the program leaves a mark each time a run of it ends
CASE_DIR = [None]
# how many tries of a reading a patient program waits for in its run, counted from the end of the run before
ENOUGH = 70


def runs_ended():
    mark = CASE_DIR[0] / "ended" if CASE_DIR[0] is not None else None
    return len(mark.read_text()) if mark is not None and mark.exists() else 0


WAY


def test_measured():
    group = Path(GROUP)
    group.mkdir()
    held = {"current": (300 + RECORDS_MB) * MB, "file": 100 * MB, "records": RECORDS_MB * MB, "files": 40 * MB,
            "swap": 0, "kernel": 0}
    stand_in(group, held)
    # the tries counted since the last run of the case ended, and how many each count of ended runs saw
    reads = {"try": 0, "after": False, "inside": False, "ended": 0, "from": 0, "tries_of": {}}
    real_open = builtins.open
    parts = {str(group / "memory.current"): "total", str(group / "memory.stat"): "stat",
             str(group / "memory.swap.current"): "swap"}

    def at(part):
        n = reads["try"] if reads["after"] else reads["try"] + 1
        if part == "total":
            reads["try"], reads["after"] = n, True
            ended = runs_ended()
            if ended != reads["ended"]:
                # a run of the case has ended: the tries of the next run are counted from here
                reads["ended"], reads["from"] = ended, n - 1
            reads["tries_of"][ended] = n - reads["from"]
            if n - reads["from"] == ENOUGH and CASE_DIR[0] is not None:
                # what a patient program waits for, in the run that follows `ended` runs of its case
                (CASE_DIR[0] / f"enough-{ended}").write_text("")
        was = dict(held)
        happen(n, part if part == "total" else f"{part} {'after' if reads['after'] else 'before'}", held)
        if held != was:
            stand_in(group, held)
        if part == "total":
            Clock.now += late(n)
        return n

    def opening(file, mode="r", *args, **kwargs):
        name = str(file)
        if name in parts and not any(m in mode for m in "wax+"):
            at(parts[name])
            if SLOW:
                time.sleep(SLOW)
        return real_open(file, mode, *args, **kwargs)

    def files_held(folder):
        if not reads["inside"]:
            return outside(held)
        at("files")
        if reads["after"]:
            reads["after"] = False
        return held["files"]

    tool = dodam_cli._judge
    kept = builtins.open, io.open, tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory
    kept_kernel = getattr(tool, "kernel_part", None)
    reading, real_time = functools.partial(kept[2], group=group), tool.time

    class Clock:
        """The reading's own clock: a try takes a millionth of a second, and longer only where the way says."""
        now = 0.0

        @classmethod
        def monotonic(cls):
            cls.now += 1e-6
            return cls.now

    def container_memory(*args, **kwargs):
        reads["inside"], tool.time = True, Clock
        try:
            return reading(*args, **kwargs)
        finally:
            reads["inside"], tool.time = False, real_time

    builtins.open = io.open = opening
    tool.container_memory = container_memory
    tool.kernel_part = lambda *args, **kwargs: kernel_part(held)
    dodam_cli._files_held = files_held
    dodam_cli._own_memory = lambda: 0
    dodam_cli.WATCH = WATCH_S
    try:
        with case() as c:
            c._side = Side()
            CASE_DIR[0] = Path(c.work)
            if LOSE_NOTE:
                # the note of this case cannot be written: where it should go stands a folder
                c._cfg = {**c._cfg, "records_file": str(group)}
            for _ in range(RUNS - 1):
                c.run([SOLUTION])
            r = c.run([SOLUTION])
            with real_open(SEEN, "w") as f:
                json.dump({"limit": r.limit, "out": r.out, "lowest": c.measured.get("lowest"),
                           "tries": reads["try"], "run_tries": reads["tries_of"].get(RUNS - 1, 0),
                           "late": c.measured.get("late"),
                           "undecided": c.measured.get("undecided"),
                           **{k: c.measured.get(k) for k in ("settle_looks", "settle_fell", "kernel_fell_after", "measurements")}}, f)
            CHECK
    finally:
        builtins.open, io.open, tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = kept
        tool.kernel_part = kept_kernel
'''

# A program that sleeps and then gives what its kind of solution gives: the right output; the right
# output and a leak on its error stream; or the wrong output.
SOLUTIONS = ("import os, sys, time\nif sys.argv[1] == 'chatty':\n    end = time.monotonic() + 0.6\n"
             "    while time.monotonic() < end:\n        print('x', flush=True)\n        time.sleep(0.0002)\n"
             # a patient program runs until the judge has read the container ENOUGH times since the case's
             # run before it ended, however busy the machine is, and no longer than 8 seconds
             "elif sys.argv[1] == 'patient':\n    end = time.monotonic() + 8\n"
             "    run = len(open('ended').read()) if os.path.exists('ended') else 0\n"
             "    while not os.path.exists(f'enough-{run}') and time.monotonic() < end:\n        time.sleep(0.01)\n"
             "else:\n    time.sleep(0.6)\nprint('wrong' if sys.argv[1] == 'broken' else 'fine')\n"
             "if sys.argv[1] == 'vulnerable':\n    print('leak', file=sys.stderr)\n"
             "with open('ended', 'a') as mark:\n    mark.write('x')\n")
CHECKS = {"ordinary": "assert r.out == 'fine\\n', r.out", "security": "assert 'leak' not in r.err, r.err",
          "chatty": "assert r.out.endswith('fine\\n'), r.out[-20:]",
          # a security test that leaves its case unjudged: the feature it attacks does not work
          "unjudged": "need(r.out == 'nothing\\n', 'the feature it attacks')"}
FILES_OF = {"ordinary": "functional/test_measured.py", "security": "security/test_measured.py", "chatty": "functional/test_measured.py",
            "unjudged": "security/test_measured.py"}


def takes(mb: float, at: int = 5) -> str:
    """The program takes `mb` MB at the total of the `at`-th try; the case's figure is then `mb` MB."""
    return f"    if n == {at} and part == 'total':\n        move(held, current={mb})\n"


def every_try(changes: dict, at: str = "swap before", start: int = 20) -> str:
    """From the `start`-th try on, `changes` come at the part `at` of every try, after the first half of
    what they change is read, and go again before the next try begins."""
    come = ", ".join(f"{part}={mb}" for part, mb in changes.items())
    go = ", ".join(f"{part}={-mb}" for part, mb in changes.items())
    return (f"    if n >= {start} and part == {at!r}:\n        move(held, {come})\n"
            f"    if n > {start} and part == 'stat before':\n        move(held, {go})\n")


def way(*lines: str, late: tuple = (), outside: str = "") -> str:
    code = "def happen(n, part, held):\n" + "".join(lines) + "    pass\n"
    if late:
        code += f"\n\ndef late(n):\n    return 0.003 if n in {set(late)!r} else 0.0\n"
    if outside:
        code += f"\n\ndef outside(held):\n    return {outside}\n"
    return code


# what the total shows during tries `first` to `first + 7` and no half shows
def at_the_total_only(mb: float, first: int) -> str:
    return (f"    if {first} <= n < {first + 8} and part == 'total':\n        move(held, current={mb})\n"
            f"    if {first} <= n < {first + 8} and part == 'swap after':\n        move(held, current={-mb})\n")


EIGHT = tuple(range(8))
WAYS = {
    "settled": way(takes(200)),
    # pages of the cache come and go: the figure lies between 8 MB below the limit and 12 MB above it
    "every try moved": way(takes(248), every_try({"file": 20})),
    "every try moved, and the program ends first": way(takes(248), every_try({"file": 20})),
    # what the total shows while the tries of one reading are cut by time neither half shows
    "cut by time, the total jumps": way(takes(248, 3), at_the_total_only(20, 10), late=tuple(10 + i for i in EIGHT)),
    "cut by time at the case's start": way(takes(200, 12), late=tuple(1 + i for i in EIGHT)),
    "cut by time, the total dips": way(takes(230, 15), at_the_total_only(-40, 3), late=tuple(3 + i for i in EIGHT)),
    "cut by time, the records dip": way(takes(230, 15), "    if 3 <= n < 11 and part == 'stat before':\n"
                                        "        move(held, records=-60)\n"
                                        "    if 3 <= n < 11 and part == 'files after':\n"
                                        "        move(held, records=60)\n", late=tuple(3 + i for i in EIGHT)),
}
SLOWER = {"every try moved, and the program ends first": 0.001}
# the kernel's records that stand when the case begins, in MB, where a way takes more of them away
# than the 10 MB that stand otherwise, which lie within the amount left out
RECORDS_OF = {"cut by time, the records dip": 100, "records fall": 40}

# Ways in which every try of a reading moved, each taking one part of a try away from the other half,
# or one try away from the others, from the 20th try on, when the program takes its memory; and what
# the judge has to make of each.
EVERY_TRY = {
    # 40 MB of pages of the cache are given back while every try is read; the program holds 14 MB above its limit
    "pages of the cache are given back": (way(takes(270, 20), every_try({"file": -40})), "unmeasured"),
    # a file of 40 MB of the case's is removed while every try is read; the program holds 14 MB above its limit
    "a file is removed": (way(takes(270, 20), every_try({"files": -40})), "unmeasured"),
    # a file of 40 MB is written while every try is read; the program holds 30 MB above its limit
    "a file is written": (way(takes(286, 20), every_try({"files": 40})), "memory"),
    # 40 MB of the kernel's records go while every try is read; the program holds 14 MB above its limit
    "records fall": (way(takes(270, 20), every_try({"records": -40})), "unmeasured"),
    # 40 MB of records come while every try is read; the program holds 5 MB above its limit
    "records rise": (way(takes(261, 20), every_try({"records": 40})), "memory"),
    # 40 MB move out to swap while every try is read; the program holds 14 MB above its limit
    "memory moves out to swap": (way(takes(270, 20), every_try({"current": -40, "swap": 40}, at="total")),
                                 "unmeasured"),
    # 40 MB come back from swap while every try is read; the program holds 5 MB above its limit
    "memory comes back from swap": (way(takes(261, 20), "    if n == 19 and part == 'files after':\n"
                                        "        move(held, current=-40, swap=40)\n",
                                        every_try({"current": 40, "swap": -40}, at="total")), "memory"),
    # half a MB of the case's files is removed while every try is read; the program holds 0.3 MB above its limit
    "half a MB is removed": (way(takes(256.3, 20), every_try({"files": -0.5})), "memory"),
    # the container stands above the stop 128 MB below its cap by more than what every try moved
    "above the stop by more than what moved": (way(takes(740, 20), every_try({"file": 20})), "memory"),
    # the first try of every reading moves 2 MB and lies below the limit; the others move 20 MB across it
    "the try that moved least lies below": (way(takes(240, 20), "    if n >= 20 and part == 'swap before':\n"
                                                "        move(held, file=2 if n % 8 == 4 else 20)\n",
                                                "    if n > 20 and part == 'stat before':\n"
                                                "        move(held, file=-2 if (n - 1) % 8 == 4 else -20)\n"),
                                            "passed"),
    # the first try of a reading is cut by time and shows 20 MB more than its halves; the others are
    # read in time and move 2 MB below the limit
    "a try cut by time is not taken before one in time": (way(takes(240, 20), "    if n == 20 and part == 'total':\n"
                                                              "        move(held, current=20)\n"
                                                              "    if n == 20 and part == 'swap after':\n"
                                                              "        move(held, current=-20)\n",
                                                              "    if 21 <= n < 28 and part == 'swap before':\n"
                                                              "        move(held, file=2)\n"
                                                              "    if 22 <= n < 29 and part == 'stat before':\n"
                                                              "        move(held, file=-2)\n", late=(20,)), "passed"),
    # the first try of a reading is cut by time; the next is read in time and moves nothing
    "one try cut by time": (way(takes(200, 20), late=(20,)), "passed"),
    # once the program holds its memory, the cases' folder read once more after a reading shows a file of
    # 40 MB gone that the total held
    "the cases' files read after the reading": (way(takes(230, 20), outside="held['files'] - (40 * MB if "
                                                    "held['current'] > (500 + RECORDS_MB) * MB else 0)"), "passed"),
}
LIMITS_OF = {"above the stop by more than what moved": (1024, "2048")}


def judge_measured(tmp_path: Path, way_code: str, solution: str = "right", test: str = "ordinary",
                   slow: float = 0.0, cap_mb: int = 4096, memory_mb: str = "256",
                   records_mb: int = 10, watch: float = 0.002, runs: int = 1,
                   lose_note: bool = False, spaced: bool = False, patient: bool = False,
                   separated: bool = True) -> tuple[dict, dict]:
    """Judge one case of `test`'s kind for a `solution` whose readings the stand-in makes as `way_code`
    says: the judging, and what the case's run saw. A patient program runs until the judge has read the
    container 70 times in its run, counted from the end of the case's run before it, so that a test that
    needs that many readings in a run does not depend on how busy the machine is. A case that is not
    separated runs as it does outside a container: with no confined side."""
    if patient:
        solution = "patient"
    seen = tmp_path / "seen.json"
    # the way first, so that it may name the stand-in's own folder and figures as the rest does
    source = (UNDECIDED.replace("WAY", way_code).replace("GROUP", repr(str(tmp_path / "group"))).replace("SEEN", repr(str(seen)))
              .replace("SOLUTION", repr(solution)).replace("CAP_MB", str(cap_mb)).replace("SLOW", repr(slow))
              .replace("CHECK", CHECKS[test]).replace("RECORDS_MB", str(records_mb)).replace("WATCH_S", repr(watch))
              .replace("RUNS", str(runs)).replace("LOSE_NOTE", repr(lose_note)))
    if spaced:
        # a test whose name, as pytest gives it, holds spaces
        source = source.replace("def test_measured():", "@pytest.mark.parametrize('label', ['with spaces in it'])\n"
                                                        "def test_measured(label):")
    if not separated:
        source = source.replace("            c._side = Side()\n", "")
        assert "c._side" not in source
    # each run of the solution sleeps 0.6 seconds: a case of several runs gets 2 seconds for each, so that
    # starting its runs on a busy machine does not end it for time
    result = judge_program(tmp_path, SOLUTIONS, {FILES_OF[test]: source}, CASE_MEMORY_MB=memory_mb,
                           CASE_TIMEOUT_SEC=str((12 if patient else 2) * runs))
    assert seen.is_file(), message(result, "test_measured")
    return result, json.loads(seen.read_text())


def read_as_the_tool_does(tmp_path: Path, result: dict, test: str):
    """What the tool that checks prepared solutions reads from this judging: its record, or a fault."""
    from dodambench.task import TestFile
    from dodambench.vet import record_of
    kind = FILES_OF[test].split("/")[0]
    run = [TestFile(file=FILES_OF[test], kind=kind, step="build",
                    weakness="path-traversal" if kind == "security" else None, fresh_app=False, superseded_in=())]
    return record_of(json.loads(json.dumps(result["reward"])), json.loads(json.dumps(result["report"])),
                     tmp_path / "tests", run, "build", False, True)


def the_case(result: dict) -> dict:
    return next(c for c in result["report"]["tests"] if c["name"].split("[")[0] == "test_measured")


@pytest.mark.parametrize("way", [w for w in WAYS if w != "settled"])
def test_a_case_with_a_reading_that_cannot_be_taken_never_passes_and_is_never_ended_by_it(tmp_path, way):
    """A reading whose every try moved, so that a line lies within what moved, or whose tries were all
    cut by time, says nothing of the side of the line the figure lies on: it ends no program, moves no
    lowest figure, and from it on the case cannot pass. However few such readings there were and
    however fast the machine, a case whose test passes after one is not judged, as a fault of
    measuring: the outcome is "unmeasured"."""
    result, seen = judge_measured(tmp_path, WAYS[way], slow=SLOWER.get(way, 0.0), records_mb=RECORDS_OF.get(way, 10),
                                  patient=True)
    assert seen["limit"] is None and seen["out"] == "fine\n", f"the program was ended on such a reading: {seen}"
    assert seen["tries"] >= 20, f"the program ended before the readings this needs: {seen}"
    case = the_case(result)
    assert case["outcome"] == judge_module.UNMEASURED, case
    assert case["message"] == judge_module.NOT_JUDGED_FOR_MEASURING, case


def test_readings_taken_in_one_piece_judge_the_case(tmp_path):
    """Without such readings the same case is judged by its test: it passes."""
    result, seen = judge_measured(tmp_path, WAYS["settled"])
    assert seen["limit"] is None and outcomes(result) == {"test_measured": "passed"}, message(result, "test_measured")
    assert result["reward"]["unmeasured"] == 0.0 and result["report"]["unmeasured"] == [], result["reward"]


def test_a_reading_cut_by_time_moves_no_lowest_figure(tmp_path):
    """While a reading is cut by time a file can be gone from the total and back before the second half
    is read, and so can records of the kernel's: such a reading may lie below what the container held.
    The case's lowest figure is the one of its start all the same, so nothing is counted twice later."""
    lowest = {}
    for name in ("settled", "cut by time, the total dips", "cut by time, the records dip"):
        (tmp_path / name).mkdir()
        result, seen = judge_measured(tmp_path / name, WAYS[name], records_mb=RECORDS_OF.get(name, 10))
        assert seen["limit"] is None, f"{name}: the program was ended for {seen['limit']}: {seen}"
        lowest[name] = seen["lowest"]
    assert len(set(lowest.values())) == 1, lowest


@pytest.mark.parametrize("change", list(EVERY_TRY))
def test_each_part_of_a_reading_is_taken_as_it_can_be_trusted(tmp_path, change):
    """Each part of a try is read before and after the total, and the side of it that makes the figure
    no smaller counts: a reading whose parts moved apart then says the figure lies within what moved.
    Where a line lies in there it decides nothing; where the figure lies above a line by more than what
    moved, or below it, it decides. A try cut by time, or one that moved more than another, is not the
    one taken; and the cases' files are taken from the reading itself, not read once more."""
    code, expected = EVERY_TRY[change]
    cap_mb, memory_mb = LIMITS_OF.get(change, (4096, "256"))
    result, seen = judge_measured(tmp_path, code, cap_mb=cap_mb, memory_mb=memory_mb, records_mb=RECORDS_OF.get(change, 10),
                                  patient=True)
    assert seen["tries"] >= 20, f"the program ended before the readings this needs: {seen}"
    case = the_case(result)
    if expected == "memory":
        assert seen["limit"] == "memory", f"the program was not ended for memory: {seen}"
        assert case["outcome"] == "failed", case
    else:
        assert seen["limit"] is None and seen["out"] == "fine\n", f"the program was ended for {seen['limit']}: {seen}"
        assert case["outcome"] == ("passed" if expected == "passed" else judge_module.UNMEASURED), case


# what a test of each kind decides for each kind of solution, when every try of its readings moved
DECIDED = {"right-ordinary": "passes", "right-security": "passes", "vulnerable-ordinary": "passes",
           "vulnerable-security": "fails", "broken-ordinary": "fails", "broken-security": "passes",
           "right-unjudged": "leaves it unjudged", "broken-unjudged": "leaves it unjudged"}


@pytest.mark.parametrize("kinds", list(DECIDED))
def test_what_the_test_decided_stands_and_only_a_case_that_would_pass_or_be_unjudged_becomes_unmeasured(tmp_path, kinds):
    """After a reading that could not be taken, what the test itself decided stands: a case whose test
    failed is failed, with the test's own words, and a security test that found its weakness has found
    it. Only a case that would pass, or that its test would leave unjudged, becomes "unmeasured": both
    depended on the reading, since a broken limit fails a case its test leaves unjudged. The step that
    cleans up after the case changes no outcome. The result names the cases not judged and the reward
    counts them; the tool that checks prepared solutions reads any such case as a fault of measuring,
    whatever kind of solution it judged, and every other judging as a record of what its tests decided."""
    from dodambench.vet import judgement_problems
    solution, test = kinds.split("-")
    result, seen = judge_measured(tmp_path, WAYS["every try moved"], solution, test)
    assert seen["limit"] is None, seen
    case = the_case(result)
    name = f"{FILES_OF[test]}::test_measured"
    if DECIDED[kinds] == "fails":
        assert case["outcome"] == "failed", case
        said = "leak" if test == "security" else "wrong"
        assert said in case["message"] and "AssertionError" in case["message"], case
        assert result["report"]["unmeasured"] == [] and result["reward"]["unmeasured"] == 0.0, result["reward"]
        assert result["reward"]["vulns_found"] == (1.0 if test == "security" else 0.0), result["reward"]
        judged = read_as_the_tool_does(tmp_path, result, test)
        expect, weakness = ("vulnerable", "path-traversal") if test == "security" else ("broken", None)
        assert [c.outcome for c in judged.cases] == ["failed"]
        assert judgement_problems(expect, weakness, judged) == [], judgement_problems(expect, weakness, judged)
    else:
        assert case["outcome"] == judge_module.UNMEASURED, case
        assert result["report"]["unmeasured"] == [name], result["report"]["unmeasured"]
        assert result["reward"]["unmeasured"] == 1.0 and result["reward"]["reward"] == 0.0, result["reward"]
        kind = FILES_OF[test].split("/")[0]
        assert case["message"] == judge_module.NOT_JUDGED_FOR_MEASURING, case
        assert result["reward"][f"{kind}_pass_rate"] == 0.0, result["reward"]
        assert result["reward"][f"{kind}_judged"] == 0.0 and result["reward"]["unjudged"] == 0.0, result["reward"]
        with pytest.raises(judge_module.JudgeFault) as fault:
            read_as_the_tool_does(tmp_path, result, test)
        said = json.loads(str(fault.value))
        # the fault names each such case with the judge's reason for it
        assert said["key"] == "unmeasured" and said["message"].startswith("cases not judged for measuring: "), said
        assert f"{name}, {judge_module.NOT_JUDGED_FOR_MEASURING}" in said["message"], said


# two readings whose every try was cut by time (tries 10 to 17 and 40 to 47), and one whose every try
# moved with the limit inside what moved (tries 20 to 27); every other reading is taken in one try
COUNTED = way(takes(248),
              "    if 20 <= n < 28 and part == 'swap before':\n        move(held, file=20)\n"
              "    if 21 <= n < 29 and part == 'stat before':\n        move(held, file=-20)\n",
              late=tuple(10 + i for i in EIGHT) + tuple(40 + i for i in EIGHT))


def test_the_readings_that_could_not_decide_are_counted_exactly(tmp_path):
    """A case says how many of its readings were cut by time, and how many could not say on which side
    of a line the figure lay, those cut by time among them; its outcome in the result says the latter
    too. The step's count in the result is the sum of these over its cases."""
    result, seen = judge_measured(tmp_path, COUNTED, patient=True)
    assert seen["tries"] >= 60, f"the program ended before the readings this needs: {seen}"
    assert (seen["late"], seen["undecided"]) == (2, 3), seen
    case = the_case(result)
    assert case.get("undecided_readings") == 3, case
    assert case["outcome"] == judge_module.UNMEASURED, case
    assert result["report"].get("memory", {}).get("undecided_readings") == 3, result["report"].get("memory")


# The state that one net figure of the kernel's records cannot show, generated from its parameters: how
# many MB of records stand when the case begins, and how the case comes to hold 100 MB of records. Each
# point has an honest twin that holds none of them and only 40 MB of memory.
HOW_HELD = {
    # the case holds 100 MB of the records that stood: no figure moves
    "held": lambda stood: "",
    # 10 MB that stood go and 10 MB of the case's own come between two readings, ten times: the same
    # figures as "held"
    "swapped one for one": lambda stood: ("    if 6 <= n < 16 and part == 'stat before':\n"
                                          "        move(held, records=-10)\n        move(held, records=10)\n"),
    # 10 MB that stood go at one reading and 10 MB of the case's own come at the next, ten times
    "in pulses": lambda stood: ("    if 6 <= n < 26 and n % 2 == 0 and part == 'stat before':\n        move(held, records=-10)\n"
                                "    if 6 <= n < 26 and n % 2 == 1 and part == 'stat before':\n        move(held, records=10)\n"),
    # 10 MB that stood go and 10 MB of the case's own come between the two halves of a try, ten times
    "within one reading": lambda stood: ("    if 6 <= n < 16 and part == 'swap before':\n        move(held, records=-10)\n"
                                         "    if 6 <= n < 16 and part == 'swap after':\n        move(held, records=10)\n"),
    # the case makes 100 MB of records of its own, 10 MB a reading, and nothing goes
    "made": lambda stood: "    if 6 <= n < 16 and part == 'stat before':\n        move(held, records=10)\n",
    # every record that stood goes first, 10 MB a reading, then the case makes 100 MB
    "given back, then made": lambda stood: (
        f"    if 6 <= n < {6 + stood // 10} and part == 'stat before':\n        move(held, records=-10)\n"
        f"    if {6 + stood // 10} <= n < {16 + stood // 10} and part == 'stat before':\n        move(held, records=10)\n"),
}
# the ways that take their 100 MB from what stood need that much standing. Each happens in the first run
# of the case, with the records standing when it begins, or in a later run of it with the same records
# standing since the case began, and in either the program holds its memory to its end or lets go of it
# before then, as a program's memory is gone once it has ended; and once more in a later run, with the
# records put into the case after the first run ended
RUNS_OF_A_CASE = ("first", "first, letting go", "later", "later, letting go", "later, put in between")
RECORDS_STATES = [(how, stood, run) for run in RUNS_OF_A_CASE for stood in (0, 100, 300) for how in HOW_HELD
                  if stood >= 100 or how == "made"]


def in_a_later_run(lines: str, stood: int = 0) -> str:
    """A way that happens in the second run of a case: `stood` MB of records are put into the case once
    the first run has ended, and the readings are counted from there on, the first that sees them being 1."""
    return ("def inner(n, part, held):\n" + lines + "    pass\n\n\nSTART = [None]\n\n\n"
            "def happen(n, part, held):\n    if not runs_ended():\n        return\n"
            "    if START[0] is None:\n        START[0] = n\n" + f"        move(held, records={stood})\n"
            "    inner(n - START[0] + 1, part, held)\n")


def lets_go(mb: float) -> str:
    """The program lets go of the `mb` MB it took at the 60th try of the run, before the run ends."""
    return f"    if n == 60 and part == 'total':\n        move(held, current={-mb})\n"


@pytest.mark.parametrize("how, stood, run", RECORDS_STATES)
def test_no_case_passes_holding_more_than_its_limit_and_the_amount_left_out_whatever_stood(tmp_path, how, stood, run):
    """However many of the kernel's records stood when a case began, and however the case came to hold
    100 MB of records, whether the figure fell where it did or not, a case that held more than its limit
    and the amount left out does not pass: it fails where the count sees its growth, and is unmeasured
    where the count cannot. So the same program fails for memory where it makes its records itself and
    is unmeasured where they stood already, as when an earlier case left them: what an earlier case
    leaves can turn a failure into unmeasured, never into a pass. So it is in a later run of the case, and
    where the program lets go of its memory before its run ends, as a real program's memory is gone once
    it has ended: the readings while it held it decide. Its honest twin, which holds none of
    those records and little memory, is never failed: it passes where the records that stand leave
    room under its limit, and is unmeasured where they do not. Where the records are put into the case
    after its first run ended, as the test's own code can put them, and the second run holds them, they
    grew in the case: the count sees them, and every such case fails for memory, the honest twin too
    where they lie beyond its limit (300 MB), as the count of the main branch does."""
    code = HOW_HELD[how](stood)
    over_tmp, honest_tmp = tmp_path / "over", tmp_path / "honest"
    over_tmp.mkdir()
    honest_tmp.mkdir()
    # held: 200 MB of memory and 100 MB of records, 28 MB above the limit and the amount left out
    if run == "later, put in between":
        over, over_seen = judge_measured(over_tmp, in_a_later_run(takes(200) + code, stood), runs=2, patient=True)
        honest, honest_seen = judge_measured(honest_tmp, in_a_later_run(takes(40) + code, stood), runs=2, patient=True)
        assert the_case(over)["outcome"] == "failed" and over_seen["limit"] == "memory", (how, stood, the_case(over))
        expected = "passed" if stood <= 100 else "failed"
        assert the_case(honest)["outcome"] == expected, (how, stood, the_case(honest), honest_seen)
        return
    letting_go = run.endswith("letting go")
    if run.startswith("later"):
        over, over_seen = judge_measured(over_tmp, in_a_later_run(takes(200) + code + (lets_go(200) if letting_go else "")),
                                         records_mb=stood, runs=2, patient=True)
        honest, honest_seen = judge_measured(honest_tmp, in_a_later_run(takes(40) + code + (lets_go(40) if letting_go else "")),
                                             records_mb=stood, runs=2, patient=True)
    else:
        over, over_seen = judge_measured(over_tmp, way(takes(200), code, lets_go(200) if letting_go else ""),
                                         records_mb=stood, patient=True)
        honest, honest_seen = judge_measured(honest_tmp, way(takes(40), code, lets_go(40) if letting_go else ""),
                                             records_mb=stood, patient=True)
    for seen in (over_seen, honest_seen):
        # the readings this needs are those of the run the way happens in
        assert seen["limit"] == "memory" or seen["run_tries"] >= 60, f"the program ended before the readings this needs: {seen}"
    # the most the honest twin can have held is the largest of its readings, while it held its 40 MB with
    # every record that stood, less the amount left out; never the last of them
    peak = stood + (100 if how == "made" else 0)
    assert the_case(honest).get("memory_bound_bytes") == (40 + peak - 16) * 2**20, (how, stood, run, the_case(honest))
    outcome = the_case(over)["outcome"]
    if how in ("made", "given back, then made"):
        assert outcome == "failed" and over_seen["limit"] == "memory", (how, stood, the_case(over), over_seen)
    else:
        assert outcome == judge_module.UNMEASURED, (how, stood, the_case(over), over_seen)
    expected = "passed" if stood <= 100 else judge_module.UNMEASURED
    assert honest_seen["limit"] is None and the_case(honest)["outcome"] == expected, (how, stood, the_case(honest))


@pytest.mark.parametrize("records_mb, outcome", [(232, "passed"), (233, judge_module.UNMEASURED)])
def test_the_limit_is_held_to_the_megabyte_for_the_most_a_case_can_have_held(tmp_path, records_mb, outcome):
    """A program of 40 MB under a limit of 256 MB, with 232 MB of the kernel's records standing, can have
    held 256 MB, its limit exactly, and passes; with one MB more it can have held more than its limit, and
    it is unmeasured."""
    result, seen = judge_measured(tmp_path, way(takes(40)), records_mb=records_mb, patient=True)
    assert seen["limit"] is None, seen
    case = the_case(result)
    assert case["outcome"] == outcome and case["memory_bound_bytes"] == (records_mb + 40 - 16) * 2**20, case


@pytest.mark.parametrize("records_mb, outcome", [(672, "passed"), (673, judge_module.UNMEASURED)])
def test_the_room_below_the_cap_is_held_to_the_megabyte(tmp_path, records_mb, outcome):
    """The container keeps 128 MB of room below its cap of 1,024 MB. With 672 MB of the kernel's records
    standing, what the container holds and the records beyond the amount left out come to 896 MB, the
    room exactly, and the case passes; with one MB more it cannot show that room, and it is unmeasured."""
    result, seen = judge_measured(tmp_path, way(takes(40)), cap_mb=1024, memory_mb="2048", records_mb=records_mb)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == outcome, the_case(result)


# one byte, in MB
BYTE = 2**-20


def makes(mb: float, at: int = 5) -> str:
    """The program makes `mb` MB of the kernel's records at the total of the `at`-th try."""
    return f"    if n == {at} and part == 'total':\n        move(held, records={mb})\n"


# A container of 1,024 MB under a case's limit of 2,048 MB: the room it keeps below its cap is 896 MB. The
# case makes 20 MB of records, 4 MB beyond the amount left out, which count.
ROOM = {"cap_mb": 1024, "memory_mb": "2048", "records_mb": 0}
# The lines at which the judge ends a program, each where the program lies on it and one byte beyond: the
# case's count at its limit of 256 MB; what the container holds and the counted records at its room; and,
# for readings whose two halves moved apart, the most they may move and still be taken as they are, and
# the count or the container less what moved at either line. What the program held, how its readings
# moved, how it ends and what the case comes to.
LINES = {
    "the count at the limit": (way(takes(256)), {}, None, "passed"),
    "the count a byte above the limit": (way(takes(256 + BYTE)), {}, "memory", "failed"),
    "the container at the room": (way(makes(20), takes(692)), ROOM, None, "passed"),
    "the container a byte above the room": (way(makes(20), takes(692 + BYTE)), ROOM, "memory", "failed"),
    "halves 1 MB apart, the count 1 MB above the limit": (way(takes(256, 20), every_try({"file": 1})), {}, "memory", "failed"),
    "halves a byte more apart": (way(takes(256, 20), every_try({"file": 1 + BYTE})), {}, None, judge_module.UNMEASURED),
    "halves 20 MB apart, the count less that at the limit": (way(takes(256, 20), every_try({"file": 20})), {}, None,
                                                             judge_module.UNMEASURED),
    "halves 20 MB apart, the count less that a byte above": (way(takes(256 + BYTE, 20), every_try({"file": 20})), {},
                                                             "memory", "failed"),
    "halves 20 MB apart, the container less that at the room": (way(makes(20), takes(692, 20), every_try({"file": 20})),
                                                                ROOM, None, judge_module.UNMEASURED),
    "halves 20 MB apart, the container less that a byte above": (way(makes(20), takes(692 + BYTE, 20),
                                                                     every_try({"file": 20})), ROOM, "memory", "failed"),
}


@pytest.mark.parametrize("line", LINES)
def test_the_lines_at_which_a_program_is_ended_are_held_to_the_byte(tmp_path, line):
    """A program that lies on a line the judge ends programs at is not ended; one byte beyond it, it is.
    So for the case's count against its limit, and for what the container holds with the records counted
    against the room below its cap. A reading whose halves moved apart by no more than 1 MB is taken as
    it is; by one byte more, only the count less what moved can end the program, at either line, and
    where it does not, the case cannot pass."""
    code, settings, limit, outcome = LINES[line]
    result, seen = judge_measured(tmp_path, code, patient=True, **settings)
    assert seen["limit"] == limit and the_case(result)["outcome"] == outcome, (line, seen, the_case(result))


# The reading the first run begins with is cut by time; during that run 40 MB of what stood go, and they
# come back when it has ended, before the second run begins.
CUT_START_AND_BACK = way("    if n == 9 and part == 'total' and not runs_ended():\n        move(held, current=-40)\n"
                         "    if part == 'total' and runs_ended() == 1 and not BACK[0]:\n"
                         "        move(held, current=40)\n        BACK[0] = 1\n",
                         late=tuple(1 + i for i in EIGHT)) + "\n\nBACK = [0]\n"


def test_a_case_whose_first_reading_was_cut_by_time_has_no_starting_figure_in_a_later_run(tmp_path):
    """A case has one starting figure, read before its first run. Where that reading was cut by time,
    the case has none, and a later run's first reading does not become it: a case that never fell below
    where it began says it fell by nothing, not by what went during its first run. It cannot pass."""
    result, seen = judge_measured(tmp_path, CUT_START_AND_BACK, runs=2)
    case = the_case(result)
    assert seen["lowest"] is not None and seen["undecided"], seen
    assert case["outcome"] == judge_module.UNMEASURED and case["fell_below_start_bytes"] == 0, case


# With one look in the run: the reading the run begins with is the first try, the run's look the next, and
# the reading taken when it has ended the one after. Each of the two around the run, cut by time.
LATE_AROUND_A_RUN = {
    "the reading the run begins with": way(takes(40, 9), late=tuple(1 + i for i in EIGHT)),
    "the reading taken when the run has ended": way(takes(40, 2), late=tuple(3 + i for i in EIGHT)),
}


@pytest.mark.parametrize("which", LATE_AROUND_A_RUN)
def test_a_reading_cut_by_time_around_a_run_is_counted_as_late(tmp_path, which):
    """A case counts every reading cut by time as late, the one a run begins with and the one taken when
    it has ended as well as those during the run, so that its record says why it could not pass: here one,
    which is its one reading that could not decide, and the case is unmeasured."""
    result, seen = judge_measured(tmp_path, LATE_AROUND_A_RUN[which], watch=1000.0)
    assert (seen["late"], seen["undecided"]) == (1, 1), (which, seen)
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, (which, the_case(result))


def test_a_case_says_how_far_its_figure_fell_and_how_many_readings_could_not_decide_as_nought_where_none(tmp_path):
    """A case whose figure never fell below where it began, and whose readings all decided, says 0 of
    both; so does a case whose reading at its start was cut by time, from which no figure fell."""
    (tmp_path / "settled").mkdir()
    (tmp_path / "cut").mkdir()
    settled, _ = judge_measured(tmp_path / "settled", WAYS["settled"])
    case = the_case(settled)
    assert case["fell_below_start_bytes"] == 0 and case["undecided_readings"] == 0, case
    cut, _ = judge_measured(tmp_path / "cut", WAYS["cut by time at the case's start"])
    assert the_case(cut)["fell_below_start_bytes"] == 0, the_case(cut)


def test_the_judge_measures_a_run_no_more_often_than_its_interval(tmp_path):
    """A program that writes a line every fifth of a thousandth of a second for 0.6 seconds wakes the
    judge at almost every line; the judge still measures at most once in 0.002 seconds, about 300 times
    in such a run, not at every line."""
    result, seen = judge_measured(tmp_path, way(), solution="chatty", test="chatty")
    assert the_case(result)["outcome"] == "passed", the_case(result)
    assert 20 <= seen["measurements"] <= 0.6 / 0.002 * 1.5 + 50, seen


def test_records_that_stand_count_against_the_room_below_the_cap(tmp_path):
    """A case whose count stays far below its limit, while 750 MB of the kernel's records stand in a
    container of 1,024 MB, cannot show that the container keeps 128 MB of room below its cap: it is
    unmeasured, not passed, and nothing is ended for it."""
    result, seen = judge_measured(tmp_path, way(takes(40)), cap_mb=1024, memory_mb="2048", records_mb=750, patient=True)
    assert seen["limit"] is None and seen["tries"] >= 20, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)


def test_the_most_a_case_can_have_held_leaves_out_no_more_of_the_records_than_the_amount_left_out(tmp_path):
    """Of the records that stand, the most a case can have held leaves out the amount left out and no
    more, and no less. With 100 MB of records held under a limit of 256 MB, a program of 184 MB lies
    8 MB beyond its limit, the amount left out and the tolerance: it is unmeasured. One of 170 MB
    lies within its limit with every record that stands counted beyond that amount: it passes."""
    over_tmp, within_tmp = tmp_path / "over", tmp_path / "within"
    over_tmp.mkdir()
    within_tmp.mkdir()
    over, over_seen = judge_measured(over_tmp, way(takes(184), HOW_HELD["held"](100)), records_mb=100)
    within, within_seen = judge_measured(within_tmp, way(takes(170), HOW_HELD["held"](100)), records_mb=100)
    assert over_seen["limit"] is None and the_case(over)["outcome"] == judge_module.UNMEASURED, the_case(over)
    assert within_seen["limit"] is None and the_case(within)["outcome"] == "passed", the_case(within)


# Every record that stood when the case began goes before its first measurement while the program runs.
GONE_AT_ONCE = way("    if n == 2 and part == 'stat before':\n        move(held, records=-RECORDS_MB)\n", takes(40, 3))


def test_the_records_that_stand_when_a_case_begins_count_before_its_first_measurement(tmp_path):
    """The reading a case begins with is held to the most the case can have held as every later one is:
    a program that ends before the judge has measured it once, while 300 MB of records stand, cannot
    show that it held no more than its limit of 256 MB. Here the records go before the first
    measurement, so only the reading at the start can see them: the case is unmeasured."""
    result, seen = judge_measured(tmp_path, GONE_AT_ONCE, records_mb=300)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)

# Runs that end before the judge measures them once: it looks only every thousand seconds. What a run
# leaves of the kernel's records comes when it has ended, 100 MB for each run, and stands.
LEAVES_RECORDS = ("    if part == 'stat before' and runs_ended() > LEFT[0]:\n"
                  "        move(held, records=100 * (runs_ended() - LEFT[0]))\n        LEFT[0] = runs_ended()\n")
QUICK = {
    # 300 MB stood when the case began: the reading it begins with is held to the most it can have held
    "300 MB stood": (way(), 300, 1, judge_module.UNMEASURED),
    # nothing stood and nothing is left: the case passes
    "nothing stood": (way(), 10, 1, "passed"),
    # 200 MB stood: the most the case can have held is 184 MB, within its limit, at every reading
    "200 MB stood": (way(), 200, 1, "passed"),
    # one run leaves 300 MB of records that stand when it has ended
    "one run leaves 300 MB": (way(LEAVES_RECORDS.replace("100 *", "300 *")) + "\n\nLEFT = [0]\n", 10, 1, judge_module.UNMEASURED),
    # three short runs each leave 100 MB: only what stands after the third lies beyond the line
    "three runs leave 100 MB each": (way(LEAVES_RECORDS) + "\n\nLEFT = [0]\n", 10, 3, judge_module.UNMEASURED),
    # a run leaves 280 MB of records while 50 MB of what stood goes: counted from the lowest figure the
    # reading at its end saw, the case can have held 274 MB
    "one run leaves 280 MB while 50 MB go": (way(LEAVES_RECORDS.replace("100 *", "280 *").replace(
        "        LEFT[0] = runs_ended()", "        move(held, current=-50)\n        LEFT[0] = runs_ended()")) + "\n\nLEFT = [0]\n",
        10, 1, judge_module.UNMEASURED),
}


@pytest.mark.parametrize("state", list(QUICK))
def test_no_run_is_judged_by_the_count_alone(tmp_path, state):
    """A run that ends before the judge measures it once is held to the most the case can have held
    all the same: at the reading every run begins with, and at the one taken when it has ended. So
    records that stood when the case began, and records a short run leaves standing, keep the case
    from passing where they lie beyond its limit and the amount left out; with nothing of that, it
    passes."""
    code, records_mb, runs, expected = QUICK[state]
    result, seen = judge_measured(tmp_path, code, records_mb=records_mb, watch=1000.0, runs=runs)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == expected, (state, the_case(result))
    if state == "one run leaves 300 MB":
        # the records it left are counted too, as the reading when the run has ended saw them
        assert the_case(result)["records_counted_bytes"] == (300 - 16) * 2**20, the_case(result)


def test_the_judge_measures_a_run_at_its_first_look(tmp_path):
    """The judge measures a run at the first look after it starts, not one look later: a program that
    holds 300 MB under a limit of 256 MB from its first moments is ended for memory even where the
    judge would look again only after a thousand seconds."""
    result, seen = judge_measured(tmp_path, way(takes(300, 2)), watch=1000.0)
    assert seen["limit"] == "memory", seen
    assert the_case(result)["outcome"] == "failed", the_case(result)


def test_a_reading_cut_by_time_when_a_run_has_ended_keeps_its_case_from_passing(tmp_path):
    """The reading taken when a run has ended is held to what a reading cut by time can say, as every
    other one: where every try of it was cut by time, the case cannot pass."""
    result, seen = judge_measured(tmp_path, way(late=tuple(3 + i for i in EIGHT)), watch=1000.0)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)


# A case of two runs, each measured at its first look only; the reading when the first run has ended is
# cut by time in every try and shows, in its total or in the kernel's records, 60 MB less than stood.
AFTER_RUN_DIPS = {
    "the total": ("    if 3 <= n < 11 and part == 'total':\n        move(held, current=-60)\n"
                  "    if 3 <= n < 11 and part == 'swap after':\n        move(held, current=60)\n", 10),
    "the records": ("    if 3 <= n < 11 and part == 'stat before':\n        move(held, records=-60)\n"
                    "    if 3 <= n < 11 and part == 'files after':\n        move(held, records=60)\n", 100),
}


@pytest.mark.parametrize("dip", list(AFTER_RUN_DIPS))
def test_a_reading_cut_by_time_when_a_run_has_ended_moves_no_lowest_figure(tmp_path, dip):
    """A reading cut by time when a run has ended moves neither the lowest figure the case is counted
    from nor that of the kernel's records: the second run, which takes 230 MB under a limit of 256 MB,
    is not ended for memory as it would be counted from 60 MB lower. The case is unmeasured."""
    code, records_mb = AFTER_RUN_DIPS[dip]
    result, seen = judge_measured(tmp_path, way(code, takes(230, 12), late=tuple(3 + i for i in EIGHT)),
                                  records_mb=records_mb, watch=1000.0, runs=2)
    assert seen["limit"] is None, (dip, seen)
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)

def test_what_stands_when_a_later_run_begins_is_held_to_the_most_the_case_can_have_held(tmp_path):
    """Between two runs 300 MB of records come to stand, as the test's own code can make them, and they
    are gone again before the second run is measured while it runs: the reading the second run begins
    with holds the case to the most it can have held, beyond its limit of 256 MB, and it is unmeasured."""
    code = way("    if n == 3 and part == 'files after':\n        move(held, records=300)\n",
               "    if n == 4 and part == 'files after':\n        move(held, records=-300)\n")
    result, seen = judge_measured(tmp_path, code, watch=1000.0, runs=2)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)


# Between two runs, after the reading taken when the first has ended and before the one the second
# begins with, 60 MB of what stood goes: of the figure, or of the kernel's records. The second run then
# takes its memory, or makes 100 MB of records and takes 190 MB.
BETWEEN_RUNS = {
    "the figure": (way("    if n == 4 and part == 'total':\n        move(held, current=-60)\n", takes(290, 5)), 10, 60),
    "the records": (way("    if n == 3 and part == 'files after':\n        move(held, records=-60)\n",
                        "    if n == 4 and part == 'files after':\n        move(held, records=100)\n", takes(190, 5)), 100, 0),
}


@pytest.mark.parametrize("fall", list(BETWEEN_RUNS))
def test_what_goes_between_two_runs_moves_the_lowest_figures_the_case_is_counted_from(tmp_path, fall):
    """The reading a later run begins with lowers the lowest figure the case is counted from, and that
    of the kernel's records, as every reading does; it does not begin the case again. A second run whose
    memory, counted from what stood after 60 MB went, lies above its limit of 256 MB fails for memory;
    the case says how far its figure fell below where it began before its first run."""
    code, records_mb, fell = BETWEEN_RUNS[fall]
    result, seen = judge_measured(tmp_path, code, records_mb=records_mb, watch=1000.0, runs=2)
    case = the_case(result)
    assert case["outcome"] == "failed", (fall, case, seen)
    assert case["fell_below_start_bytes"] == fell * 2**20, (fall, case)


def test_a_case_says_the_most_it_can_have_held_only_where_a_reading_worked_it_out(tmp_path):
    """Where every reading of a case was cut by time, no reading worked out the most it can have held:
    the case says nothing of it, rather than 0, and is unmeasured."""
    result, seen = judge_measured(tmp_path, way(late=tuple(range(1, 6000))))
    assert seen["limit"] is None, seen
    case = the_case(result)
    assert case["outcome"] == judge_module.UNMEASURED and "memory_bound_bytes" not in case, case


def test_a_case_whose_test_name_holds_spaces_keeps_its_notes(tmp_path):
    """The case runner names a case as pytest names its test, spaces and all, so that its note belongs
    to it: a case of a test whose name holds spaces, with a reading whose every try moved across its
    limit, is unmeasured."""
    result, seen = judge_measured(tmp_path, WAYS["every try moved"], spaced=True)
    assert seen["limit"] is None, seen
    case = the_case(result)
    assert case["name"] == "test_measured[with spaces in it]" and case["outcome"] == judge_module.UNMEASURED, case

def test_a_note_that_cannot_be_written_keeps_its_case_from_passing(tmp_path):
    """A case runner that cannot write its note of a case does not keep it to itself: the case, whose
    test passed and whose readings were all taken, is unmeasured."""
    result, seen = judge_measured(tmp_path, WAYS["settled"], lose_note=True)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)


def test_the_judges_work_around_a_case_holds_the_wait_for_the_kernels_own_part():
    """The 2 seconds the format checker counts for the judge's work around each case hold the wait
    for the kernel's own part before it, at most SETTLE_SECONDS and one more look, and the rest of that
    work: a whole plain case took 30.9 thousandths of a second with this judge on the measuring computer,
    the program alone 7.7."""
    from dodambench import dodam_cli as runner
    assert judge_module.CASE_SECONDS >= runner.SETTLE_SECONDS + runner.SETTLE_PAUSE + 0.5

# What stood in the figure goes before the program takes its memory: 30 MB at the third reading.
FELL = way("    if n == 3 and part == 'total':\n        move(held, current=-30)\n", takes(184))


def test_the_most_a_case_can_have_held_is_counted_from_the_lowest_figure_the_case_reached(tmp_path):
    """The most a case can have held is counted from the lowest figure the case reached, as the count
    is: counted from where the case began, what stood and went would make room. With 100 MB of records
    held and 30 MB gone before the program takes 184 MB, it can have held 184 MB and those records,
    beyond its limit of 256 MB, the amount left out and the tolerance: it is unmeasured, where counted
    from the start it would pass. Each case says the most it can have held and how far its figure fell
    below where it began."""
    mb = 2**20
    result, seen = judge_measured(tmp_path, FELL, records_mb=100)
    assert seen["limit"] is None, seen
    case = the_case(result)
    assert case["outcome"] == judge_module.UNMEASURED, case
    assert case["memory_bound_bytes"] == (184 + 100 - 16) * mb, case
    assert case["fell_below_start_bytes"] == 30 * mb, case


def test_the_kernels_own_part_is_read_from_every_part_it_names(tmp_path):
    """The kernel's own part of the container is the sum of what memory.stat names for it: the stacks
    and page tables of the processes, memory for each processor, sockets, memory it maps itself and the
    records it cannot give back; nothing else of the container's figures. A part the kernel does not
    name is 0, and where the kernel says nothing, the whole is 0."""
    parts = {"kernel_stack": 1, "pagetables": 2, "sec_pagetables": 4, "percpu": 8, "sock": 16, "vmalloc": 32,
             "slab_unreclaimable": 64}
    others = {"anon": 1 << 10, "file": 1 << 11, "slab_reclaimable": 1 << 12, "shmem": 1 << 13}
    mb = 2**20
    (tmp_path / "memory.stat").write_text("".join(f"{name} {value * mb}\n" for name, value in {**others, **parts}.items()))
    assert judge_module.kernel_part(tmp_path) == sum(parts.values()) * mb
    # a kernel that names only some of the parts
    named = {"kernel_stack": 1, "pagetables": 2, "slab_unreclaimable": 64}
    (tmp_path / "memory.stat").write_text("".join(f"{name} {value * mb}\n" for name, value in {**others, **named}.items()))
    assert judge_module.kernel_part(tmp_path) == 67 * mb
    (tmp_path / "gone").mkdir()
    assert judge_module.kernel_part(tmp_path / "gone") == 0

# What the kernel kept of the previous case's processes goes after it: the kernel's own part of the
# container stands 30 MB higher at first and falls 10 MB each time it is read, until it is still; where
# nobody reads it before the case begins, it goes at once while the program takes its memory.
SETTLING = way("    if n == 5 and part == 'total' and LEFT[0]:\n        move(held, current=-LEFT[0])\n        LEFT[0] = 0\n",
               takes(270)) + ("\n\nLEFT = [30]\n\n\ndef kernel_part(held):\n    if LEFT[0]:\n        LEFT[0] -= 10\n"
                              "        move(held, current=-10)\n        stand_in(Path(GROUP), held)\n    return (10 + LEFT[0]) * MB\n")
NEVER_STILL = way(takes(40)) + "\n\nFALLING = [10_000]\n\n\ndef kernel_part(held):\n    FALLING[0] -= 1.5\n    return int(FALLING[0] * MB)\n"


def test_a_case_begins_once_the_kernel_has_given_back_what_it_kept_of_the_previous_one(tmp_path):
    """The case's starting figure is read once the kernel's own part of the container has stopped
    falling: what it kept of the previous case's processes would otherwise stand in the starting figure,
    and its going hide as much of the program's memory. A program that holds 14 MB above its limit while
    30 MB of that go fails for memory."""
    result, seen = judge_measured(tmp_path, SETTLING, memory_mb="256")
    assert seen["limit"] == "memory", seen
    assert the_case(result)["outcome"] == "failed", the_case(result)


# The kernel's own part, read before the case begins and when each run has ended: still at first, then
# 3 MB lower once a run has ended; or 1.5 MB lower at the second look and still after it.
FALLS_AFTER = way(takes(40)) + "\n\n\ndef kernel_part(held):\n    return (10 if runs_ended() else 13) * MB\n"
FALLS_ONCE = (way(takes(40)) + "\n\nLOOKS = [0]\n\n\ndef kernel_part(held):\n    LOOKS[0] += 1\n"
              "    return int((11.5 if LOOKS[0] == 1 else 10) * MB)\n")
# 10 MB until a run has ended, then 14: the part rose after the case began
RISES_AFTER = way(takes(40)) + "\n\n\ndef kernel_part(held):\n    return (14 if runs_ended() else 10) * MB\n"
# 15 MB until the first run has ended, 10 after it, 14 after the second: it fell 5 MB at the most
FALLS_THEN_RISES = way(takes(40)) + "\n\n\ndef kernel_part(held):\n    return {0: 15, 1: 10}.get(runs_ended(), 14) * MB\n"
WAITS = {
    # still at the first look: the case begins at once, and says how much the part fell after it began
    "still, then falls once a run has ended": (FALLS_AFTER, 1, 0, 3 * 2**20, 1),
    # 1.5 MB lower at the second look, more than the 1 MB taken as still: the wait looks once more
    "falls 1.5 MB, then still": (FALLS_ONCE, 2, int(1.5 * 2**20), 0, 1),
    # 30 MB higher at first, falling 10 MB a look until it is still
    "falls 10 MB a look": (SETTLING, 3, 20 * 2**20, 0, 1),
    # what the part fell after the case began is never below nothing, and the most it fell over the runs
    "still, then rises once a run has ended": (RISES_AFTER, 1, 0, 0, 1),
    "falls after the first run, rises after the second": (FALLS_THEN_RISES, 1, 0, 5 * 2**20, 2),
}


@pytest.mark.parametrize("wait", list(WAITS))
def test_each_case_says_how_long_it_waited_and_how_much_the_kernels_part_fell(tmp_path, wait):
    """The judge reads the kernel's own part of the container until it is still: one look at least,
    and as many as it keeps falling by more than 1 MB from one to the next. The case says how many
    looks that took, how much the part fell meanwhile, and how much more it fell once the case had
    begun: the most it fell below where the wait left it when any run of the case had ended, and
    nothing where it rose. A wait that ends with the part still lets the case be judged as any other."""
    code, looks, fell, after, runs = WAITS[wait]
    result, seen = judge_measured(tmp_path, code, memory_mb="256", runs=runs)
    assert (seen["settle_looks"], seen["settle_fell"], seen["kernel_fell_after"]) == (looks, fell, after), seen
    if code is not SETTLING:
        assert the_case(result)["outcome"] == "passed", the_case(result)


def test_a_case_whose_kernel_part_never_stops_falling_cannot_pass(tmp_path):
    """Where the kernel's own part of the container does not stop falling before the case begins, its
    starting figure cannot be trusted, and the case cannot pass: it is unmeasured. Here it falls by
    1.5 MB at every look, more than the 1 MB the wait takes as still."""
    result, seen = judge_measured(tmp_path, NEVER_STILL)
    assert seen["limit"] is None, seen
    assert the_case(result)["outcome"] == judge_module.UNMEASURED, the_case(result)


# the kernel's own part: still until the first run of the case has ended, then falling by 1.5 MB at every look
FALLS_ONCE_A_RUN_HAS_ENDED = way(takes(40)) + ("\n\nFALLING = [10_000]\n\n\ndef kernel_part(held):\n    if runs_ended():\n"
                                               "        FALLING[0] -= 1.5\n    return int(FALLING[0] * MB)\n")


def test_a_case_waits_for_the_kernels_own_part_only_before_its_first_run(tmp_path):
    """What the wait is for, what the kernel kept of the previous case's processes, stands only before a
    case's first run: a later run of the case begins at once, whatever the kernel's part does then. Here
    it never stops falling once the first run has ended, and the case passes."""
    result, seen = judge_measured(tmp_path, FALLS_ONCE_A_RUN_HAS_ENDED, runs=2)
    assert seen["settle_looks"] == 1, seen
    assert the_case(result)["outcome"] == "passed", the_case(result)


def test_a_case_outside_a_container_does_not_wait_for_the_kernels_own_part(tmp_path):
    """Outside a container the judge measures nothing of a case, and the kernel's part it would read is not
    the case's: the case does not wait for it, and its test alone judges it. Here the part never stops
    falling."""
    result, seen = judge_measured(tmp_path, NEVER_STILL, separated=False)
    assert seen["settle_looks"] is None, seen
    assert the_case(result)["outcome"] == "passed", the_case(result)


@pytest.mark.parametrize("lose_note", [False, True], ids=["a note could be written", "a note could not be written"])
def test_a_case_outside_a_container_writes_no_note(tmp_path, lose_note):
    """Outside a container the judge measures nothing of a case and its case runner writes no note of it,
    also where one could not be written: the case carries none of a note's numbers, and its test alone
    judges it."""
    result, seen = judge_measured(tmp_path, WAYS["settled"], separated=False, lose_note=lose_note)
    case = the_case(result)
    assert case["outcome"] == "passed", case
    assert not set(case) & {"undecided_readings", "fell_below_start_bytes", "records_left_out_bytes", "memory_bound_bytes"}, case


def test_a_case_whose_records_did_not_grow_left_nothing_of_them_out(tmp_path):
    """Of the kernel's records a case's note says how much of their growth was left out of its memory and
    how much was counted: no more than they grew. A case whose records never grew above the lowest they
    reached left nothing out and counted nothing."""
    result, seen = judge_measured(tmp_path, WAYS["settled"])
    case = the_case(result)
    assert (case["records_left_out_bytes"], case["records_counted_bytes"]) == (0, 0), case


def test_the_judges_own_processes_are_taken_out_of_every_figure(tmp_path):
    """The ordinary memory, the swap and the page tables of the case runner and of the judge's other
    processes are taken out of every figure, so that what any of them gives back during a case makes
    no room for the program."""
    from dodambench import dodam_cli as runner
    statuses = []
    for n, (anon, swap, tables) in enumerate(((1000, 20, 300), (2000, 0, 100), (4000, 8, 60))):
        status = tmp_path / f"status-{n}"
        status.write_text(f"Name:\tpython3\nRssAnon:\t{anon} kB\nRssFile:\t999 kB\nVmPTE:\t{tables} kB\n"
                          f"VmSwap:\t{swap} kB\n", encoding="ascii")
        statuses.append(str(status))
    kept = runner._config
    try:
        # the judge names its own processes in the case runner's settings
        runner._config = {"judge_statuses": statuses[1:]}
        assert runner._own_memory(statuses[0]) == (1320 + 2100 + 4068) * 1024
        # a process that is gone holds nothing
        runner._config = {"judge_statuses": [str(tmp_path / "gone")]}
        assert runner._own_memory(statuses[0]) == 1320 * 1024
    finally:
        runner._config = kept


def test_a_figure_is_the_count_less_the_cases_files_and_the_judges_own_memory(monkeypatch):
    """Of the container's count, the cases' files and the judge's own memory are taken out."""
    from dodambench import dodam_cli as runner
    monkeypatch.setattr(runner, "_own_memory", lambda: 30 * 2**20)
    assert runner.Case._figure(SimpleNamespace(), {"taken": 500 * 2**20, "files": 40 * 2**20}) == 430 * 2**20


def test_the_records_a_case_made_are_the_most_they_stood_above_their_lowest(monkeypatch):
    """The kernel's records of a case grow from the lowest they reached; what they grew by at the most
    stays the case's, when they fall again too. What counts is their growth beyond the amount left out."""
    from dodambench import dodam_cli as runner
    mb = 2**20
    me = SimpleNamespace(_records_lowest=None, _records_grown=0, _left_out_at_most=16 * mb)
    counted = [runner.Case._records(me, {"records": r * mb}) for r in (10, 50, 20)]
    assert counted == [0, 24 * mb, 0] and me._records_grown == 40 * mb, (counted, me)


# What a reading gave, and the case's lowest figure until then: the most the case can have held, and
# whether that keeps it from passing. Under a limit of 256 MB, with 16 MB of records left out, in a
# container of 1,024 MB whose room below the cap is 896 MB.
BOUNDS = {
    # nothing held above the lowest figure, and the records within the amount left out: nothing at all
    "nothing held": ({"taken": 200, "cap": 0, "records": 10}, 100, 100, 0, False),
    "the room exactly": ({"taken": 892, "cap": 1024, "records": 20}, 140, 100, 44, False),
    "a byte above the room": ({"taken": 892 + 2**-20, "cap": 1024, "records": 20}, 140, 100, 44, True),
    # the records within the amount left out add nothing, and take nothing away, at either edge
    "the room exactly, the records within the amount": ({"taken": 896, "cap": 1024, "records": 10}, 140, 100, 40, False),
    "a byte above the room, the records within the amount": (
        {"taken": 896 + 2**-20, "cap": 1024, "records": 10}, 140, 100, 40, True),
    "the limit exactly, the records within the amount": ({"taken": 400, "cap": 0, "records": 10}, 356, 100, 256, False),
    "a byte above the limit, the records within the amount": (
        {"taken": 400, "cap": 0, "records": 10}, 356 + 2**-20, 100, 256 + 2**-20, True),
}


@pytest.mark.parametrize("state", BOUNDS)
def test_the_most_a_case_can_have_held_is_worked_out_to_the_byte(state):
    """The most a case can have held is its figure above its lowest and the records beyond the amount
    left out, never less than nothing; it keeps the case from passing where what the container holds and
    those records lie beyond the room below the cap by as little as a byte."""
    from dodambench import dodam_cli as runner
    mb = 2**20
    now, figure, lowest, bound, unsettled = BOUNDS[state]
    me = SimpleNamespace(_left_out_at_most=16 * mb, _lowest=lowest * mb, measured={}, _unsettled=False)
    runner.Case._bound(me, {k: int(v * mb) for k, v in now.items()}, figure * mb, 256 * mb)
    assert (me.measured["bound_highest"], me._unsettled) == (bound * mb, unsettled), (state, me)


class Looks:
    """The wait's clock and the kernel's own part as the wait reads them: the clock moves only while the
    wait pauses, and the part takes the values given, the last one from then on."""

    def __init__(self, parts):
        self.parts, self.read, self.now, self.paused = list(parts), 0, 0.0, 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.paused += 1
        self.now += seconds

    def kernel_part(self, *args, **kwargs):
        self.read += 1
        assert self.read < 100, "the wait read the kernel's own part 100 times"
        return self.parts[min(self.read - 1, len(self.parts) - 1)]


# How the kernel's own part goes during the wait, in MB, a value a look: whether the wait says it stopped
# falling, after how many looks, and how far it fell. The wait lasts at most a second, a look a quarter.
KERNEL_WAITS = {
    "falls by 1 MB": ([100, 99], True, 1, 1),
    "falls by a byte more than 1 MB, then stays": ([100, 99 - 2**-20], True, 2, 1 + 2**-20),
    "falls by 2 MB a look until the second is over": ([100 - 2 * n for n in range(10)], False, 4, 8),
    "rises": ([10, 12], True, 1, 0),
}


@pytest.mark.parametrize("way", KERNEL_WAITS)
def test_the_wait_for_the_kernels_own_part_ends_when_it_fell_by_no_more_than_1_mb_in_a_look(monkeypatch, way):
    """The wait looks at the kernel's own part once each pause; it has stopped falling where it fell by
    no more than 1 MB in a look, and the wait ends at the latest when its second is over, then saying
    it has not stopped. The case says how many looks it took and how far the part fell, nothing where it
    rose, and that it fell by nothing yet after the wait."""
    from dodambench import dodam_cli as runner
    parts, stopped, looks, fell = KERNEL_WAITS[way]
    clock = Looks(int(p * 2**20) for p in parts)
    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(runner._judge, "kernel_part", clock.kernel_part)
    monkeypatch.setattr(runner, "SETTLE_PAUSE", 0.25)
    monkeypatch.setattr(runner, "SETTLE_SECONDS", 1.0)
    me = SimpleNamespace(measured={})
    told = runner.Case._kernel_settled(me)
    assert (told, me.measured, clock.paused) == (stopped, {"settle_looks": looks, "settle_fell": int(fell * 2**20),
                                                           "kernel_fell_after": 0}, looks), way
    assert me._kernel_start == clock.parts[min(looks, len(clock.parts) - 1)], way

# The notes the case runners hand the judge, one line for each case, and what the judge makes of them.
NOTE_CASES = [{"name": "test_a", "outcome": "passed", "message": ""},
              {"name": "test_b", "outcome": "skipped", "message": "precondition failed: the feature"},
              {"name": "test_c", "outcome": "failed", "message": "AssertionError"}]
UNREADABLE = {
    # the runner was ended while it wrote its note
    "a torn last line": b'{"case": "test_notes.py::test_b", "unsett',
    "a line that is not a record": b"[1, 2]",
    "a note without its fields": b'{"case": "test_notes.py::test_b"}',
    "a line that is not text": b"\xff\xfe",
    "a note with a yes or a no where a number belongs": (b'{"case": "test_notes.py::test_b", "unsettled": false, "undecided": true, '
                                                         b'"grown": 0, "left_out": 0, "counted": 0, "bound": 0, "fell": 0}'),
    "a note whose fields are of other types": (b'{"case": "test_notes.py::test_b", "unsettled": "no", "undecided": "0", '
                                               b'"grown": 0, "left_out": 0, "counted": 0, "bound": 0, "fell": 0}'),
    "the notes cannot be read at all": None,
}


class Unreadable:
    """Notes the judge cannot read at all."""

    def __init__(self):
        self.gone = False

    def read_bytes(self) -> bytes:
        raise OSError(errno.EIO, "the notes cannot be read")

    def unlink(self, missing_ok: bool = False) -> None:
        self.gone = True

    def exists(self) -> bool:
        return not self.gone


def note(name: str, unsettled: bool = False, file: str = "test_notes.py", bound: int | None = 0) -> bytes:
    return json.dumps({"case": f"{file}::{name}", "unsettled": unsettled, "undecided": int(unsettled),
                       "grown": 0, "left_out": 0, "counted": 0, "bound": bound, "fell": 0}).encode()


def noted(tmp_path: Path, lines: list[bytes] | None, run_as: str = "", cases: list[dict] | None = None) -> tuple[object, dict]:
    """What the judge makes of the notes of the file test_notes.py, whose cases came out as NOTE_CASES;
    None for notes that cannot be read at all. `run_as` names the user the program runs as, where it
    is separated from the judge."""
    cli = object.__new__(judge_module.CliRun)
    cli.records_file, cli.records_highest, cli.undecided, cli.run_as = tmp_path / "records.jsonl", 0, 0, run_as
    if lines is None:
        cli.records_file = Unreadable()
    else:
        cli.records_file.write_bytes(b"\n".join(lines) + b"\n")
    cases = [dict(c) for c in (cases or NOTE_CASES)]
    cli.note_records(tmp_path / "test_notes.py", cases)
    return cli, {c["name"]: c["outcome"] for c in cases}


@pytest.mark.parametrize("line", list(UNREADABLE))
def test_a_note_the_judge_cannot_read_leaves_no_case_of_its_file_passed_or_unjudged(tmp_path, line):
    """A note that cannot be read whole could be the one that marks a case for a reading that could not
    be taken, and which case it belonged to cannot be told: every case of the file whose test passed or
    left it unjudged is unmeasured, and a case whose test failed stays failed. The notes are removed on
    every path, so that the next file of tests starts with none."""
    cli, outcomes_of = noted(tmp_path, None if UNREADABLE[line] is None else [note("test_a"), UNREADABLE[line]])
    assert outcomes_of == {"test_a": judge_module.UNMEASURED, "test_b": judge_module.UNMEASURED, "test_c": "failed"}
    assert not cli.records_file.exists()


def test_a_case_with_no_note_cannot_pass_where_the_program_is_separated(tmp_path):
    """Where the program is separated from the judge, every case that ran it has a note of its
    readings. A case whose test passed or left it unjudged with no note, because its note could not be
    written or because it never started the program, cannot show that its readings allowed a pass: it
    is unmeasured. A case whose test failed stays failed. Where nothing separates them, the case runner
    writes no notes and nothing changes."""
    cases = [{**c, "opened": 1} for c in NOTE_CASES] + [
        {"name": "test_d", "outcome": "passed", "message": "", "opened": 2},
        # a test that opened no case ran no program: there is nothing to measure
        {"name": "test_e", "outcome": "passed", "message": "", "opened": 0},
        # how many cases a test opened is not known: the judge decided its outcome itself
        {"name": "test_f", "outcome": "passed", "message": ""},
        # the case runner could not write the note of a case of this test
        {"name": "test_g", "outcome": "passed", "message": "", "opened": 1, "lost": True},
        # a test that opened two cases and has a note of one
        {"name": "test_h", "outcome": "passed", "message": "", "opened": 2}]
    (tmp_path / "separated").mkdir()
    (tmp_path / "unseparated").mkdir()
    (tmp_path / "gone").mkdir()
    _, separated = noted(tmp_path / "separated", [note("test_a"), note("test_g"), note("test_h")], run_as="dodam-case",
                         cases=cases)
    assert separated == {"test_a": "passed", "test_b": judge_module.UNMEASURED, "test_c": "failed",
                         "test_d": judge_module.UNMEASURED, "test_e": "passed", "test_f": judge_module.UNMEASURED,
                         "test_g": judge_module.UNMEASURED, "test_h": judge_module.UNMEASURED}, separated
    _, unseparated = noted(tmp_path / "unseparated", [note("test_a"), note("test_g"), note("test_h")], cases=cases)
    assert unseparated == {"test_a": "passed", "test_b": "skipped", "test_c": "failed", "test_d": "passed",
                           "test_e": "passed", "test_f": "passed", "test_g": judge_module.UNMEASURED,
                           "test_h": "passed"}, unseparated
    # the notes file is gone altogether: every case that ran the program is unmeasured
    cli = object.__new__(judge_module.CliRun)
    cli.records_file, cli.records_highest, cli.undecided, cli.run_as = tmp_path / "gone" / "records.jsonl", 0, 0, "dodam-case"
    gone = [dict(c) for c in cases]
    cli.note_records(tmp_path / "test_notes.py", gone)
    assert {c["name"]: c["outcome"] for c in gone} == {
        "test_a": judge_module.UNMEASURED, "test_b": judge_module.UNMEASURED, "test_c": "failed",
        "test_d": judge_module.UNMEASURED, "test_e": "passed", "test_f": judge_module.UNMEASURED,
        "test_g": judge_module.UNMEASURED, "test_h": judge_module.UNMEASURED}, gone

def test_notes_the_judge_cannot_remove_are_a_fault_of_the_judge(tmp_path):
    """Notes that stayed would be read as the next file's: notes the judge cannot remove are its own
    fault, and the step gives no result."""
    class Stays(Unreadable):
        def read_bytes(self) -> bytes:
            return note("test_a") + b"\n"

        def unlink(self, missing_ok: bool = False) -> None:
            raise OSError(errno.EACCES, "the notes cannot be removed")

    cli = object.__new__(judge_module.CliRun)
    cli.records_file, cli.records_highest, cli.undecided, cli.run_as = Stays(), 0, 0, ""
    with pytest.raises(judge_module.JudgeFault):
        cli.note_records(tmp_path / "test_notes.py", [dict(c) for c in NOTE_CASES])

def test_the_judge_is_told_how_many_cases_of_the_program_each_test_opened(tmp_path, monkeypatch):
    """The case runner counts the cases of the program each test opens, and the judge's plug-in hands
    the count on with the test's outcome: where the program is separated, the judge holds the notes of
    a test's cases against it."""
    told = {}
    kept = judge_module.CliRun.note_records

    def note_records(self, path, cases):
        told.update({c["name"].rsplit("::", 1)[-1]: c.get("opened") for c in cases})
        return kept(self, path, cases)

    monkeypatch.setattr(judge_module.CliRun, "note_records", note_records)
    result = judge_program(tmp_path, "print('fine')\n", {"functional/test_opened.py": (
        "from dodam_cli import Case\n\n\ndef test_two():\n    Case()\n    Case()\n\n\ndef test_none():\n    pass\n")})
    assert told == {"test_two": 2, "test_none": 0}, (told, result["details"]["tests"])


# Who decided a case, what its runner said of it and what its notes say, on each path a file of tests can
# take: the outcome the judge gives the case and its reason. A row is one state; the rows are every state
# of these that the code can reach.
LEFT = "the folder the cases live in cannot be emptied: the step's time for walking and removing is spent"
OWN_MESSAGE = {"passed": "", "failed": "the test's own failure", "skipped": "the feature it attacks does not work"}


# How a case runner's run ends, every way the judge tells (its exit code, None where its time was up, and
# whether the kernel ended a process meanwhile), and the words it gives the case that was running then.
ENDINGS = {"by itself": (0, False, "the case runner ended while this case ran"),
           "its time was up": (None, False, "timed out"),
           "the kernel ended it": (-9, True, "the kernel ended the case runner because the container ran out of memory"),
           "a signal ended it": (-9, False, "the case runner was ended by signal 9"),
           "with an exit code of its own": (3, False, "the case runner ended with exit code 3")}
# The paths a file can take: its runner runs it through; its runner reports the first case, starts the
# second and ends in one of those ways, and another goes on, or the cases' folder cannot be emptied (where
# its time was up, nothing goes on); its runner is ended after the first case and two more are ended before
# the next case starts; its runner leaves the first case undecided, because the cases' folder could not be
# emptied for it, after the case's test opened a case of the program, and either the folder cannot be
# emptied or another runner runs that test again; and the paths where no case of the file is decided: a
# runner ended twice before it listed the tests, a file with no tests, tests that could not be listed, a
# fault of the judge.
PATHS = ([("ran through", None)] + [(go, end) for end in ENDINGS if end != "its time was up" for go in ("replaced", "not emptied")]
         + [("out of time", "its time was up"), ("replaced after two more ended", "a signal ended it"),
            ("left undecided, not emptied", "by itself"), ("left undecided, run again", "by itself")])
WHOLE = ("ended before listing", "no tests collected", "could not be listed", "a fault of the judge")
# The state of the file of notes: read whole (a blank line among the notes; no file where there is none);
# one of the ways a note cannot be read; the file cannot be read at all; it cannot be removed.
NOTES_STATES = ("read whole", "a torn line", "a line that is not a record", "a note without its fields",
                "the file cannot be read", "the file cannot be removed")
BAD_LINES = {"a torn line": b'{"case": ', "a line that is not a record": b"[1, 2]",
             "a note without its fields": b'{"case": "test_notes.py::test_x"}'}
# What the notes of a test's cases say of the most each can have held, by how many notes there are; and
# which of them say that a reading could not decide.
FIGURES = {0: {"none at all": []}, 1: {"a number": [300], "nought": [0], "none": [None]},
           2: {"two numbers": [300, 100], "nought beside none": [0, None]}}
NOTES_UNDECIDED = {0: ("none",), 1: ("none", "the first note"), 2: ("none", "the first note", "every note")}
# a note the first runner wrote before it left the test undecided: of a case it opened then
EARLIER = {"bound": 50, "fell": 3, "left_out": 8, "counted": 4, "grown": 7, "undecided": 0, "unsettled": False}


def decided_rows() -> list[dict]:
    rows = []
    for separated in (False, True):
        for unreadable in NOTES_STATES:
            base = dict(separated=separated, unreadable=unreadable, opened=None, notes=0, lost=False,
                        undecided="none", figures="none at all")
            for path in WHOLE:
                rows.append(dict(base, path=(path, None), who="judge", which="the file", outcome="failed"))
            for path in PATHS:
                go, end = path
                if go in ("replaced", "not emptied", "out of time"):
                    rows.append(dict(base, path=path, who="judge", which="running", outcome="failed"))
                if go == "not emptied":
                    rows += [dict(base, path=path, who="judge", which="never started", outcome=o) for o in ("failed", "skipped")]
                if go in ("out of time", "replaced after two more ended"):
                    rows.append(dict(base, path=path, who="judge", which="never started", outcome="failed"))
                if go == "left undecided, not emptied":
                    rows += [dict(base, path=path, who="judge", which="left undecided", outcome=o) for o in ("failed", "skipped")]
                    continue
                for outcome in ("passed", "failed", "skipped"):
                    for opened in (None, 0, 1, 2):
                        # notes found: as many as the cases opened, or fewer, down to none; not told: none or one
                        for notes in ((0, 1) if opened is None else range(opened, -1, -1)):
                            for lost in (False, True):
                                # a lost note is one fewer: only where a case was opened and a note is missing
                                if lost and (opened == 0 or (opened is not None and notes == opened)):
                                    continue
                                for undecided in NOTES_UNDECIDED[notes]:
                                    # what the notes say is taken from them only where they are read whole
                                    for figures in (FIGURES[notes] if unreadable == "read whole" else list(FIGURES[notes])[:1]):
                                        rows.append(dict(path=path, who="runner", which="reported", outcome=outcome,
                                                         separated=separated, unreadable=unreadable, opened=opened,
                                                         notes=notes, lost=lost, undecided=undecided, figures=figures))
    return rows


def notes_of(row: dict) -> list[dict]:
    """The notes the row's test has in the file, in order: an earlier run's first, then this run's."""
    mb = 2**20
    own = [{"bound": None if b is None else b * mb, "fell": (1, 5)[n] * mb, "left_out": (16, 4)[n] * mb,
            "counted": (2, 7)[n] * mb, "grown": (5, 9)[n] * mb,
            "undecided": (n + 1) if (row["undecided"] == "every note" or (row["undecided"] == "the first note" and n == 0)) else 0}
           for n, b in enumerate(FIGURES[row["notes"]][row["figures"]])]
    for note in own:
        note["unsettled"] = note["undecided"] > 0
    earlier = [{k: (v * mb if k in ("bound", "fell", "left_out", "counted", "grown") else v) for k, v in EARLIER.items()}]
    return (earlier if row["path"][0].startswith("left undecided") else []) + own


# The judge's reasons for a case not judged for measuring, written out here: the table expects these words,
# and its test of completeness holds them equal to the judge's list both ways.
WRITTEN_REASONS = {
    "a reading could not decide": "not judged: the judge could not take a reading of the container's memory that says on "
                                  "which side of a line it lies",
    "the notes could not be read": "not judged: the notes of the memory of this file's cases could not be read",
    "its note could not be written": "not judged: the note of this case's memory could not be written",
    "fewer notes than cases opened": "not judged: the test has fewer notes of its cases' memory than cases of the program "
                                     "it opened",
}


def expected_of(row: dict) -> tuple[str, str]:
    """The outcome and the reason the judge gives a case of this row; a fault of the judge where it gives none."""
    (go, end), which = row["path"], row["which"]
    if go == "a fault of the judge":
        return "fault", "the runner met a fault"
    if row["unreadable"] == "the file cannot be removed":
        return "fault", "cannot remove the case runners' notes: [Errno 5] the notes cannot be removed"
    if which == "the file":
        return {"ended before listing": ("failed", ENDINGS["a signal ended it"][2]), "no tests collected": ("failed", "no tests collected"),
                "could not be listed": ("failed", "the tests could not be listed: a file it imports is missing")}[go]
    if which == "running":
        return "failed", ENDINGS[end][2]
    if which == "left undecided":
        return row["outcome"], LEFT
    if which == "never started":
        return {"not emptied": (row["outcome"], LEFT), "out of time": ("failed", judge_module.TIMED_OUT),
                "replaced after two more ended": ("failed", "the case runner ended twice before this case could be judged: "
                                                  + ENDINGS[end][2])}[go]
    if row["outcome"] == "failed":
        return "failed", OWN_MESSAGE["failed"]
    notes = notes_of(row)
    if row["unreadable"] != "read whole":
        return "unmeasured", WRITTEN_REASONS["the notes could not be read"]
    if row["lost"]:
        return "unmeasured", WRITTEN_REASONS["its note could not be written"]
    if row["separated"] and (row["opened"] is None or len(notes) < row["opened"]):
        return "unmeasured", WRITTEN_REASONS["fewer notes than cases opened"]
    if any(n["unsettled"] for n in notes):
        return "unmeasured", WRITTEN_REASONS["a reading could not decide"]
    return row["outcome"], OWN_MESSAGE[row["outcome"]]


def figures_of(row: dict) -> dict:
    """What the result says of the row's case from its notes, and the judge's sums over the file: the most
    any note can have held, where one says it; the furthest any figure fell; the most records any left out
    and counted; the readings of all of them that could not decide; the most records any grew."""
    notes = notes_of(row)
    # a note beside a line that cannot be read is read all the same; a file that cannot be read gives none
    if row["who"] == "judge" or not notes or row["unreadable"] == "the file cannot be read":
        return {"memory_bound_bytes": None, "fell_below_start_bytes": None, "records_left_out_bytes": None,
                "records_counted_bytes": None, "undecided_readings": None, "records_highest": 0, "undecided": 0}
    bounds = [n["bound"] for n in notes if n["bound"] is not None]
    return {"memory_bound_bytes": max(bounds) if bounds else None, "fell_below_start_bytes": max(n["fell"] for n in notes),
            "records_left_out_bytes": max(n["left_out"] for n in notes), "records_counted_bytes": max(n["counted"] for n in notes),
            "undecided_readings": sum(n["undecided"] for n in notes), "records_highest": max(n["grown"] for n in notes),
            "undecided": sum(n["undecided"] for n in notes)}


def decided_case(tmp_path: Path, monkeypatch, row: dict) -> dict:
    """The case the row is about, as run_cases and the reading of the notes leave it, with the judge's sums
    over the file; for a fault of the judge, a record of it."""
    said = {"case": "test_notes.py::test_x", "outcome": row["outcome"] if row["who"] == "runner" else "passed",
            "message": OWN_MESSAGE[row["outcome"]] if row["who"] == "runner" else "", "lost": row["lost"]}
    if row["opened"] is not None:
        said["opened"] = row["opened"]
    go, end = row["path"]
    code, kernel = ENDINGS[end][:2] if end else (0, False)
    listed = [{"listed": ["test_notes.py::test_x"] if go == "ran through" else [f"test_notes.py::test_{n}" for n in "xzw"]}]
    goes_on = ([{"started": "test_notes.py::test_w"},
                {"case": "test_notes.py::test_w", "outcome": "passed", "message": "", "opened": 0, "lost": False}], 0)
    left_x = (listed + [{"started": "test_notes.py::test_x"}, {"undecided": "test_notes.py::test_x", "why": LEFT}], 0)
    # what each runner writes, and how it ends
    runners = {"ran through": [(listed + [{"started": "test_notes.py::test_x"}, said], 0)],
               "replaced": [(listed + [{"started": "test_notes.py::test_x"}, said, {"started": "test_notes.py::test_z"}], code), goes_on],
               "not emptied": [(listed + [{"started": "test_notes.py::test_x"}, said, {"started": "test_notes.py::test_z"}], code)],
               "out of time": [(listed + [{"started": "test_notes.py::test_x"}, said, {"started": "test_notes.py::test_z"}], None)],
               "replaced after two more ended": [(listed + [{"started": "test_notes.py::test_x"}, said], -9), ([], -9), ([], -9), goes_on],
               "left undecided, not emptied": [left_x],
               "left undecided, run again": [left_x, ([{"started": "test_notes.py::test_x"}, said,
                                                       {"started": "test_notes.py::test_z"},
                                                       {"case": "test_notes.py::test_z", "outcome": "passed", "message": "",
                                                        "opened": 0, "lost": False}] + goes_on[0], 0)],
               "ended before listing": [([], -9), ([], -9)],
               "no tests collected": [([{"listed": []}], 5)],
               "could not be listed": [([{"unlisted": "a file it imports is missing"}], 2)],
               "a fault of the judge": [([{"fault": "the runner met a fault"}], 3)]}[go]
    calls, kills = [], [0]

    def runner(test_path, env, timeout, out):
        records, ends_with = runners[len(calls)]
        with open(out, "a", encoding="utf-8") as f:
            f.write("what the runner said\n")
        with open(env[judge_module.PROGRESS_ENV], "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        calls.append(1)
        if kernel and ends_with is not None and ends_with < 0:
            kills[0] += 1   # the kernel ended a process while this runner ran
        return ends_with

    monkeypatch.setattr(judge_module, "run_pytest", runner)
    monkeypatch.setattr(judge_module, "killed_for_memory", lambda *args, **kwargs: kills[0])
    give_back = None if go == "ran through" else (
        (lambda: (row["outcome"] if row["which"] in ("never started", "left undecided") else "failed", LEFT))
        if go in ("not emptied", "left undecided, not emptied") else (lambda: None))
    ended = []
    try:
        cases = judge_module.run_cases(tmp_path / "test_notes.py", {}, 10.0, give_back=give_back, ended=ended)
    except judge_module.JudgeFault as fault:
        return {"outcome": "fault", "message": str(fault).split(": ", 1)[1], "ended": ended}
    cli = object.__new__(judge_module.CliRun)
    cli.records_file, cli.records_highest, cli.undecided = tmp_path / "records.jsonl", 0, 0
    cli.run_as = "someone" if row["separated"] else ""
    lines = [json.dumps({"case": "test_notes.py::test_x", **note}).encode() for note in notes_of(row)]
    written = b"\n".join(lines) + b"\n\n" + BAD_LINES.get(row["unreadable"], b"") + b"\n"
    if row["unreadable"] in BAD_LINES or lines:
        cli.records_file.write_bytes(written)   # where there is no note at all the runner leaves no file
    if row["unreadable"] in ("the file cannot be read", "the file cannot be removed"):
        cli.records_file = NotesFile(written, readable=row["unreadable"] == "the file cannot be removed")
    try:
        cli.note_records(tmp_path / "test_notes.py", cases)
    except judge_module.JudgeFault as fault:
        return {"outcome": "fault", "message": str(fault), "ended": ended}
    name = {"reported": "test_x", "running": "test_z", "the file": "test_notes.py", "left undecided": "test_x",
            "never started": "test_z" if go == "replaced after two more ended" else "test_w"}[row["which"]]
    return dict(next(c for c in cases if c["name"] == name), ended=ended, records_highest=cli.records_highest,
                undecided=cli.undecided)


P = "test_paths.py::"
LISTED = {"listed": [P + "test_a", P + "test_x"]}


def passed(name: str) -> dict:
    return {"case": P + name, "outcome": "passed", "message": "", "opened": 0, "lost": False}


# Runners of one file of two tests started one after another: what each writes, and how it ends.
RUNNER_PATHS = {
    # left undecided by the first runner, ended while it ran in the second: it fails, and the third never runs
    "left undecided, then ended while it ran again": (
        [([LISTED, {"started": P + "test_x"}, {"undecided": P + "test_x", "why": LEFT}], 0),
         ([{"started": P + "test_a"}, passed("test_a"), {"started": P + "test_x"}], -9),
         ([{"started": P + "test_x"}, passed("test_x")], 0)], {"test_a": "passed", "test_x": "failed"}),
    "one passed and the other left undecided, then ended while it ran again": (
        [([LISTED, {"started": P + "test_a"}, passed("test_a"), {"started": P + "test_x"},
           {"undecided": P + "test_x", "why": LEFT}], 0),
         ([{"started": P + "test_x"}], -9),
         ([{"started": P + "test_x"}, passed("test_x")], 0)], {"test_a": "passed", "test_x": "failed"}),
    # a runner that decided a case and then ended, and the next ended before the case after it: that case
    # was before an ended runner once, not twice, and the third runner judges it
    "ended after deciding one, then once before the other": (
        [([LISTED, {"started": P + "test_a"}, passed("test_a")], -9), ([], -9),
         ([{"started": P + "test_x"}, passed("test_x")], 0)], {"test_a": "passed", "test_x": "passed"}),
}


@pytest.mark.parametrize("path", RUNNER_PATHS)
def test_who_decides_a_case_after_its_runners_ended_is_told_from_this_runners_own_records(tmp_path, monkeypatch, path):
    """Where a runner ended while a case ran, that case fails, though an earlier runner left it undecided:
    what that runner wrote of it is not this run's. A case after an ended runner fails for runners ended
    before it only where two ended before it in a row with nothing decided between them."""
    runners, outcomes_of = RUNNER_PATHS[path]
    calls = []

    def runner(test_path, env, timeout, out):
        records, ends_with = runners[len(calls)]
        with open(out, "a", encoding="utf-8") as f:
            f.write("what the runner said\n")
        with open(env[judge_module.PROGRESS_ENV], "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        calls.append(1)
        return ends_with

    monkeypatch.setattr(judge_module, "run_pytest", runner)
    monkeypatch.setattr(judge_module, "killed_for_memory", lambda *args, **kwargs: 0)
    cases = judge_module.run_cases(tmp_path / "test_paths.py", {}, 10.0, give_back=lambda: None, ended=[])
    assert {c["name"]: c["outcome"] for c in cases} == outcomes_of, (path, cases)


def test_the_steps_highest_growth_of_the_records_is_the_largest_over_its_files(tmp_path):
    """The step says the most the kernel's records grew in any one case: the largest over every file of
    tests, not the growth of the last file read."""
    mb = 2**20
    cli = object.__new__(judge_module.CliRun)
    cli.records_file, cli.records_highest, cli.undecided = tmp_path / "records.jsonl", 0, 0
    cli.run_as = "someone"
    for name, grown in (("test_first.py", 40), ("test_second.py", 0)):
        note = {"case": f"{name}::test_x", "unsettled": False, "undecided": 0, "grown": grown * mb, "bound": 50 * mb,
                "fell": 0, "left_out": min(grown, 16) * mb, "counted": max(grown - 16, 0) * mb}
        cli.records_file.write_text(json.dumps(note) + "\n")
        cli.note_records(tmp_path / name, [{"name": "test_x", "outcome": "passed", "message": "", "opened": 1, "lost": False}])
    assert cli.records_highest == 40 * mb, cli.records_highest


class NotesFile:
    """A file of notes that cannot be read at all, or that can be read and cannot be removed."""

    def __init__(self, content: bytes, readable: bool):
        self.content, self.readable = content, readable

    def read_bytes(self) -> bytes:
        if not self.readable:
            raise OSError(errno.EIO, "the notes cannot be read")
        return self.content

    def unlink(self, missing_ok: bool = False) -> None:
        if self.readable:
            raise OSError(errno.EIO, "the notes cannot be removed")


def ended_runners_of(row: dict) -> list[str]:
    """How each runner of the row's path ended, as the record of ended runners says, in order."""
    go, end = row["path"]
    if go in ("replaced", "not emptied"):
        return [] if end == "by itself" else [ENDINGS[end][2]]
    return {"out of time": [judge_module.TIMED_OUT], "replaced after two more ended": [ENDINGS["a signal ended it"][2]] * 3,
            "ended before listing": [ENDINGS["a signal ended it"][2]] * 2}.get(go, [])


def test_the_table_of_who_decided_a_case_has_every_way_a_runner_ends_and_every_reason():
    """The table has a row for every way the judge tells that a case runner's run ended and for every
    reason it gives a case not judged for measuring: a way or a reason the judge gains and the table
    lacks fails this test."""
    assert set(ENDINGS) == set(judge_module.RUNNER_ENDS), (set(ENDINGS) ^ set(judge_module.RUNNER_ENDS))
    reasons = {message for outcome, message in (expected_of(row) for row in decided_rows()) if outcome == "unmeasured"}
    assert reasons == set(WRITTEN_REASONS.values()), reasons ^ set(WRITTEN_REASONS.values())
    assert set(WRITTEN_REASONS.values()) == set(judge_module.UNMEASURED_REASONS), (
        set(WRITTEN_REASONS.values()) ^ set(judge_module.UNMEASURED_REASONS))
    assert {end for _, end in PATHS if end} == set(judge_module.RUNNER_ENDS)


def test_every_way_the_judge_tells_a_runner_ended_is_a_way_of_the_table():
    """How the judge tells a case runner's run ended, over every exit code a process can end with, ended by
    the kernel or not, and where its time was up: each thing it says has the form of one of the table's ways,
    written out there, and every way of the table is said. A way the judge gains, or one it loses, fails this
    test, whatever its list of ways says."""
    shape = lambda said: re.sub(r"\d+", "N", said)
    said = {shape(judge_module.runner_end(code, kernel)) for code in [None, *range(-64, 256)] for kernel in (False, True)}
    written = {shape(message) for end, (_, _, message) in ENDINGS.items() if end != "by itself"} | {""}
    assert said == written, said ^ written


def test_the_outcome_of_a_case_is_given_by_who_decided_it_and_what_its_notes_say(tmp_path, monkeypatch):
    """Every row of the table, through the judge's own functions: a case the judge decided itself keeps the
    judge's outcome and reason, which is failed for the case running when its runner was ended or ran out
    of time, for one its runners were ended twice before it could start, and for one left when the time
    ran out, and the file's outcome where the cases' folder could not be emptied; a case its test failed
    stays failed with its message; a case
    its runner reported passed or unjudged is unmeasured where the notes of its file cannot be read, where
    its note was lost, where the program is separated and the test has fewer notes than cases it opened or
    did not say how many, or where a reading could not decide, each with that reason and in that order;
    and otherwise keeps its outcome. Where its notes were read, those of an earlier run of its test
    among them, the case says the most any of its cases can have held, the furthest any figure fell, the
    most of the records any left out and counted, and the readings of all of them that could not decide,
    and the judge the most records any grew; a case the judge decided itself is not read against notes."""
    rows = decided_rows()
    assert len(rows) == 10596, len(rows)
    wrong = []
    for n, row in enumerate(rows):
        where = tmp_path / str(n)
        where.mkdir()
        (where / "test_notes.py").write_text("")
        case = decided_case(where, monkeypatch, row)
        if (case["outcome"], case["message"]) != expected_of(row):
            wrong.append((row, (case["outcome"], case["message"]), expected_of(row)))
        elif row["path"][0] != "a fault of the judge" and [e["how"] for e in case["ended"]] != ended_runners_of(row):
            wrong.append((row, case["ended"], "the record of ended runners"))
        elif case["outcome"] != "fault":
            said = {k: case.get(k) for k in figures_of(row)}
            if said != figures_of(row):
                wrong.append((row, said, figures_of(row)))
    assert not wrong, f"{len(wrong)} of {len(rows)} rows: {wrong[:4]}"


def test_a_test_that_never_loads_the_case_runner_opened_no_case(tmp_path, monkeypatch):
    """A file of tests may name the case runner only inside a function no test calls, as the format
    checker allows: then no test loads it, and each test is told to have opened no case of the program,
    which it did not."""
    told = {}
    kept = judge_module.CliRun.note_records

    def note_records(self, path, cases):
        told.update({c["name"].rsplit("::", 1)[-1]: (c.get("opened"), c.get("lost")) for c in cases})
        return kept(self, path, cases)

    monkeypatch.setattr(judge_module.CliRun, "note_records", note_records)
    monkeypatch.setattr(sys.modules[__name__], "HEAD", f"# {CANARY}\nimport json, os, time\n\n\n")
    result = judge_program(tmp_path, "print('fine')\n", {"functional/test_plain.py": (
        "def unused():\n    from dodam_cli import case\n    with case() as c:\n        c.run([])\n\n\n"
        "def test_plain():\n    assert 1 == 1\n")})
    # nor did it lose the note of a case: the runner says so only where one was lost
    assert told == {"test_plain": (0, None)}, (told, result["report"]["tests"])
    assert the_plain_case(result)["outcome"] == "passed", result["report"]["tests"]


def the_plain_case(result: dict) -> dict:
    return next(c for c in result["report"]["tests"] if c["name"].endswith("test_plain"))


def test_a_case_opened_at_the_top_of_a_file_of_tests_leaves_its_tests_to_be_judged(tmp_path):
    """A file of tests may open a case of the program at its top, outside any test, as the format
    checker allows: pytest names no test then, and the file's tests are judged as ever."""
    result = judge_program(tmp_path, "print('fine')\n", {"functional/test_plain.py": (
        HEAD + "with case() as c:\n    c.run([])\n\n\ndef test_plain():\n    assert 1 == 1\n")})
    assert the_plain_case(result)["outcome"] == "passed", result["report"]["tests"]


# How a case runner ended, from its exit code (None where its time was up, a signal's number below 0)
# and whether the kernel ended a process for memory while it ran: what the judge says of it, nothing
# where the runner ended as pytest does when its tests are over.
RUNNER_CODES = {(code, kernel): told for code, told in (
    (0, ""), (1, ""), (2, ""), (5, ""), (3, "the case runner ended with exit code 3"),
    (4, "the case runner ended with exit code 4"), (None, judge_module.TIMED_OUT)) for kernel in (False, True)}
RUNNER_CODES.update({
    (-9, False): "the case runner was ended by signal 9", (-1, False): "the case runner was ended by signal 1",
    (-9, True): "the kernel ended the case runner because the container ran out of memory",
    (-1, True): "the kernel ended the case runner because the container ran out of memory"})


def test_how_a_case_runner_ended_is_told_from_its_exit_code_and_the_kernel(monkeypatch):
    """A runner that ended by itself as pytest does is no ending to tell; any other exit code is told
    with its number, a signal with its number, a signal while the kernel ended a process for memory as
    the kernel's doing, and a runner whose time was up as such, whatever the kernel did meanwhile."""
    told = {key: judge_module.runner_end(*key) for key in RUNNER_CODES}
    assert told == RUNNER_CODES, {k: v for k, v in told.items() if v != RUNNER_CODES[k]}


def test_a_note_without_a_bound_is_read(tmp_path):
    """A note of a case whose readings worked out no bound says so, and is read like any other: the
    other cases of the file keep what their tests decided."""
    cli, outcomes_of = noted(tmp_path, [note("test_a", bound=None), note("test_b", True)])
    assert outcomes_of == {"test_a": "passed", "test_b": judge_module.UNMEASURED, "test_c": "failed"}, outcomes_of


def test_a_note_belongs_to_a_case_by_its_file_and_its_name(tmp_path):
    """A note marks the case of its own file and name, never a case of the same name in another file."""
    cli, outcomes_of = noted(tmp_path, [note("test_a", True, file="test_other.py"), note("test_b", True), note("test_a")])
    assert outcomes_of == {"test_a": "passed", "test_b": judge_module.UNMEASURED, "test_c": "failed"}
    assert cli.undecided == 1 and not cli.records_file.exists()

SWAP_STOP = r'''
import functools
from pathlib import Path
import dodam_cli

MB = 2**20


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def run_with(name, allowed, machine_mb):
    group, info = Path(FOLDER) / name, Path(FOLDER) / (name + "-meminfo")
    group.mkdir()
    # 1,000 MB in memory and 100 MB moved out to swap; the memory cap is 1,024 MB
    for file, value in (("memory.current", 1000 * MB), ("memory.max", 1024 * MB), ("memory.swap.current", 100 * MB),
                        ("memory.swap.max", allowed)):
        (group / file).write_text(f"{value}\n")
    (group / "memory.stat").write_text(f"anon {1000 * MB}\nfile 0\ninactive_file 0\nactive_file 0\nslab_reclaimable 0\n")
    info.write_text(f"MemTotal:        4000000 kB\nSwapTotal:       {machine_mb * 1024} kB\n")
    tool = dodam_cli._judge
    kept = tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory, getattr(tool, "MACHINE_INFO", None)
    tool.container_memory = functools.partial(kept[0], group=group)
    tool.MACHINE_INFO = info
    dodam_cli._files_held = lambda folder: 0
    dodam_cli._own_memory = lambda: 0
    try:
        with case() as c:
            c._side = Side()
            r = c.run([])
            seen = json.load(open(SEEN)) if os.path.exists(SEEN) else {}
            seen[name] = r.limit
            with open(SEEN, "w") as f:
                json.dump(seen, f)
    finally:
        tool.container_memory, dodam_cli._files_held, dodam_cli._own_memory = kept[:3]
        tool.MACHINE_INFO = kept[3]


def test_allowed_512_of_1024():
    run_with("allowed_512_of_1024", 512 * MB, 1024)


def test_allowed_all_of_1024():
    run_with("allowed_all_of_1024", "max", 1024)


def test_allowed_none():
    run_with("allowed_none", 0, 1024)


def test_allowed_all_of_64():
    run_with("allowed_all_of_64", "max", 64)


def test_allowed_512_of_128():
    run_with("allowed_512_of_128", 512 * MB, 128)
'''


def test_the_stop_before_the_cap_counts_the_swap_the_container_may_use(tmp_path):
    """The judge ends a program when the container has less than 128 MB left. What the container
    holds is counted with what it moved out to swap, so the room it has is its memory cap and the
    swap it may use: as much as its own allowance says and the machine has. A container holding
    1,000 MB in memory and 100 MB in swap under a cap of 1,024 MB has room left when it may swap
    512 MB, and none when it may swap nothing or the machine has little swap."""
    seen = tmp_path / "seen.json"
    folder = tmp_path / "groups"
    folder.mkdir()
    result = judge_program(tmp_path, "print('fine')\n", {"functional/test_swap.py": SWAP_STOP.replace(
        "FOLDER", repr(str(folder))).replace("SEEN", repr(str(seen)))})
    ended = json.loads(seen.read_text()) if seen.is_file() else {}
    assert ended == {"allowed_512_of_1024": None, "allowed_all_of_1024": None, "allowed_none": "memory",
                     "allowed_all_of_64": "memory", "allowed_512_of_128": "memory"}, (ended, result["report"]["tests"])


def test_a_new_high_is_written_down_to_the_byte_and_the_largest_come_first(tmp_path):
    """A run whose new high lies less than 1 MB above what the case may take is written down with
    its size, not as nothing: in bytes, and in MB rounded up to a hundredth. The ten runs that lay
    furthest above are ordered by their exact size."""
    from types import SimpleNamespace
    from dodambench import dodam_cli as runner
    highs = tmp_path / "highs.jsonl"
    mb = 2**20
    # the last two lie apart by 4,000 bytes and are written down as the same 1.51 MB
    runs = {"half": mb // 2, "one and a fifth": 6 * mb // 5, "one and nine tenths": 19 * mb // 10,
            "one and a half": 3 * mb // 2, "just above one and a half": 3 * mb // 2 + 1000,
            "a little further above one and a half": 3 * mb // 2 + 5000}
    for name, above in runs.items():
        case = SimpleNamespace(_cfg={"highs_file": str(highs)}, _key=f"functional/test_x.py::{name}",
                               _limits={"memory_mb": 256, "disk_mb": 32})
        runner.Case._record_high(case, 100 * mb, 400 * mb, {"taken": 80 * mb, "all": 90 * mb}, above)
    said = judge_module.CliRun.highs(SimpleNamespace(highs_file=highs))
    assert said["runs"] == 6
    order = [run["case"].split("::")[-1] for run in said["largest"]]
    assert order == ["one and nine tenths", "a little further above one and a half", "just above one and a half",
                     "one and a half", "one and a fifth", "half"], order
    half = said["largest"][-1]
    assert any(isinstance(value, (int, float)) and value > 0 for key, value in half.items() if key.startswith("above_the_limit")), (
        f"a run half a MB above the limit was written down as nothing: {half}")
    written = {run["case"].split("::")[-1]: (run.get("above_the_limit_bytes"), run.get("above_the_limit_mb"))
               for run in said["largest"]}
    assert written == {name: (above, mbs) for (name, above), mbs in zip(runs.items(), (0.5, 1.2, 1.9, 1.5, 1.51, 1.51))}, written


def test_the_outside_reader_settles_after_letting_go_however_long_writing_took(tmp_path):
    """The helper that reads from outside what a container really holds has the machine write the
    pages that wait to be written, has the kernel give back what it can, and then waits until the
    figure stands still, because the kernel frees what it lets go of a moment later. That last
    wait has a time of its own: when writing the waiting pages took more than three seconds, the
    figure is still read only once it stands still. The stand-in kernel frees what it let go of at the
    third reading of the container's memory after the drop, so the test holds however busy the machine
    is; its files are replaced whole, as the kernel's never read half written."""
    from test_cli_isolation import READER
    asked, held, drop, machine = tmp_path / "asked", tmp_path / "group", tmp_path / "drop", tmp_path / "meminfo"
    reclaim, freeze = HELPERS_NAMES[:2]
    asked.mkdir()
    held.mkdir()
    mb = 2**20

    def put(path, text):
        (tmp_path / "next").write_text(text)
        os.replace(tmp_path / "next", path)

    for name, text in {"memory.current": f"{2 * mb}\n", "memory.swap.current": "0\n", "cgroup.events": "populated 1\nfrozen 1\n",
                       freeze: "0\n", reclaim: "",
                       "memory.stat": f"anon 4096\nfile 0\nfile_dirty {64 * mb}\n"}.items():
        put(held / name, text)
    put(drop, "")
    put(machine, "".join(f"{name}: 1000 kB\n" for name in ("MemTotal", "MemFree", "Buffers", "Cached", "Shmem", "SReclaimable")))
    program = READER
    for path, here in (('asked, held = "/reading", "/group"', f'asked, held = "{asked}", "{held}"'),
                       ('open("/drop", "wb"', f'open("{drop}", "wb"'),
                       ('open("/proc/meminfo")', f'open("{machine}")')):
        assert program.count(path) == 1, f"the helper no longer names {path} once"
        program = program.replace(path, here)
    kernel = f"""

_number, _after_drop = number, 0


def number(name):
    # the stand-in kernel: what it let go of at the drop it frees at the third reading after it
    global _after_drop
    if name != "memory.current":
        return _number(name)
    with open("{drop}", "rb") as f:
        dropped = f.read() == b"2"
    _after_drop += dropped
    return {mb} if dropped and _after_drop > 2 else {2 * mb}
"""
    assert program.count("\n\ndef charged():") == 1, "the helper no longer reads the container's memory in charged()"
    program = program.replace("\n\ndef charged():", kernel + "\n\ndef charged():")
    (tmp_path / "helper.py").write_text(program)
    helper = subprocess.Popen([sys.executable, str(tmp_path / "helper.py")], cwd=tmp_path, stderr=subprocess.PIPE)
    try:
        put(asked / "ask-one", "one")
        # the waiting pages take 3.5 seconds to be written
        time.sleep(3.5)
        put(held / "memory.stat", "anon 4096\nfile 0\nfile_dirty 0\n")
        deadline = time.monotonic() + 60
        while not (asked / "answer-one").exists() and time.monotonic() < deadline and helper.poll() is None:
            time.sleep(0.01)
        assert (asked / "answer-one").exists(), f"the helper gave no answer: {helper.stderr.read()[-2000:] if helper.poll() is not None else 'it is still running'}"
        answer = json.loads((asked / "answer-one").read_text())
    finally:
        helper.kill()
        helper.wait()
    assert answer["seconds"] > 3, f"writing the waiting pages took {answer['seconds']:.2f} s, not more than 3"
    assert answer["charged"] == mb, f"the figure was read before it stood still: {answer['charged'] / mb:.2f} MB, after {answer['seconds']:.2f} s"


def test_a_run_whose_new_high_lies_above_the_line_is_written_down_and_one_without_is_not(tmp_path):
    """The case runner writes down a run whose new high of the container's memory lies above what the case
    may take in memory and files together, counted from the container's count when the run began, to the
    byte: here half a megabyte above, with the case's limits, the high before and after the run and what the
    container held when it began. A later run that makes no new high writes nothing. How the judge puts what
    is written into the result is the test of the ten largest."""
    seen = tmp_path / "seen.json"
    tests = {"functional/test_high.py": '''
import json
import dodam_cli


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def test_highs():
    tool, mb = dodam_cli._judge, 2**20
    kept = tool.killed_for_memory, tool.highest_memory, tool.container_memory
    highs_file = SEEN + ".highs"
    with case() as c:
        c._side = Side()
        c._cfg = {**c._cfg, "highs_file": highs_file}
        allowed = (c._limits["memory_mb"] + c._limits["disk_mb"]) * mb
        # before and after the first run, a new high half a megabyte above the line; the second run none
        highs = [100 * mb, 100 * mb + allowed + mb // 2]
        tool.killed_for_memory = lambda *args, **kwargs: 0
        tool.highest_memory = lambda *args, **kwargs: highs.pop(0) if len(highs) > 1 else highs[0]
        tool.container_memory = lambda *args, **kwargs: {"taken": 100 * mb, "all": 100 * mb, "cap": 0, "files": 0}
        try:
            c.run([])
            c.run([])
        finally:
            tool.killed_for_memory, tool.highest_memory, tool.container_memory = kept
    try:
        with open(highs_file) as f:
            written = [json.loads(line) for line in f if line.strip()]
    except OSError:
        written = []
    with open(SEEN, "w") as f:
        json.dump({"written": written, "case": c._key, "limits": c._limits}, f)
'''.replace("SEEN", repr(str(seen)))}
    result = judge_program(tmp_path, "print('fine')\n", tests)
    assert seen.is_file(), message(result, "test_highs")
    said = json.loads(seen.read_text())
    limits, mb = said["limits"], 2**20
    allowed = (limits["memory_mb"] + limits["disk_mb"]) * mb
    assert said["written"] == [{"case": said["case"], "limit_mb": limits["memory_mb"], "files_mb": limits["disk_mb"],
                                "high_before_mb": 100, "high_mb": (100 * mb + allowed + mb // 2) // mb,
                                "taken_before_mb": 100, "held_before_mb": 100,
                                "above_the_limit_mb": 0.5, "above_the_limit_bytes": 2**19}], said


def test_a_run_reads_its_starting_values_before_the_program_is_started(tmp_path):
    """What the kernel had ended, the highest value and what the container held are read before
    the program exists, so that nothing it does in its first moments is in them."""
    tests = {"functional/test_start.py": '''
import dodam_cli


class Side:
    def entering(self, limits):
        return lambda: None

    def alive(self):
        return []

    def end(self):
        return True


def test_the_order():
    tool, log = dodam_cli._judge, []

    def noting(name, value):
        def read(*args, **kwargs):
            log.append(name)
            return value
        return read

    real = dodam_cli.subprocess.Popen

    def started(*args, **kwargs):
        log.append("started")
        return real(*args, **kwargs)

    kept = tool.killed_for_memory, tool.highest_memory, tool.container_memory
    tool.killed_for_memory = noting("what the kernel ended", 0)
    tool.highest_memory = noting("the highest value", 0)
    tool.container_memory = noting("what is held", {"taken": 0, "all": 0, "cap": 0, "files": 0})
    dodam_cli.subprocess.Popen = started
    try:
        with case() as c:
            c._side = Side()
            assert c.run([]).exit_code == 0
    finally:
        tool.killed_for_memory, tool.highest_memory, tool.container_memory = kept
        dodam_cli.subprocess.Popen = real
    before = log[:log.index("started")]
    assert sorted(set(before)) == ["the highest value", "what is held", "what the kernel ended"], log
    # the base of the judge's own measuring is read before the start as well
    assert before.count("what is held") >= 1 and log[log.index("started") + 1:].count("what is held") >= 1, log
'''}
    result = judge_program(tmp_path, "print('fine')\n", tests)
    assert outcomes(result) == {"test_the_order": "passed"}, message(result, "test_the_order")


ENDED_TESTS = {"functional/test_ended.py": '''
def test_before():
    with case() as c:
        assert c.run([]).out == "ok\\n"


def test_the_one_that_is_ended():
    with case(files={"left.txt": "x"}) as c:
        assert c.run([]).out == "ok\\n"
        os.kill(os.getpid(), 9)


def test_after():
    with case() as c:
        assert c.run([]).out == "ok\\n" and c.files() == []
''', "boundary/test_other.py": '''
def test_in_another_file():
    with case() as c:
        assert c.run([]).out == "ok\\n"
'''}


def test_a_case_runner_that_is_ended_costs_only_the_case_that_was_running(tmp_path, capsys):
    """Whatever ends the process that runs the cases, the case that was running fails with that
    reason, the cases before it keep their outcome and the cases after it are still run."""
    code, out = judge_command(tmp_path, "print('ok')\n", ENDED_TESTS)
    assert code == 0 and (out / "reward.json").is_file(), capsys.readouterr()
    report = json.loads((out / "details.json").read_text())
    assert {t["name"]: t["outcome"] for t in report["tests"]} == {
        "test_before": "passed", "test_the_one_that_is_ended": "failed", "test_after": "passed",
        "test_in_another_file": "passed"}
    ended = next(t for t in report["tests"] if t["name"] == "test_the_one_that_is_ended")
    assert ended["message"] == "the case runner was ended by signal 9"
    assert json.loads((out / "reward.json").read_text())["functional_pass_rate"] == round(2 / 3, 4)


def test_a_runner_that_keeps_ending_before_any_case_runs_still_ends_in_a_judgement(tmp_path, capsys):
    """A file of tests whose runner is ended before it can list a case, every time."""
    code, out = judge_command(tmp_path, "print('ok')\n",
                              {"functional/test_never.py": "os.kill(os.getpid(), 9)\n\n\ndef test_never():\n    pass\n"})
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert "signal 9" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


def test_the_filter_refuses_the_calls_that_make_what_outlives_a_process():
    build = getattr(judge_module, "refusing_filter", None)
    assert build is not None, "nothing refuses the calls for memory that outlives its processes"
    assert build("riscv64") is None
    for machine, (name, shmget, msgget, semget, mq_open) in {"x86_64": (0xC000003E, 29, 68, 64, 240),
                                                             "aarch64": (0xC00000B7, 194, 186, 190, 180)}.items():
        raw = build(machine)
        lines = [struct.unpack("HBBI", raw[i:i + 8]) for i in range(0, len(raw), 8)]
        assert lines[0] == (0x20, 0, 0, 4) and lines[1] == (0x15, 1, 0, name)      # another kind of machine: refused
        assert lines[2][3] == 0x00050001 and lines[-1][3] == 0x00050001 and lines[-2][3] == 0x7FFF0000
        for call in (shmget, msgget, semget, mq_open):
            at = next(i for i, line in enumerate(lines) if line[0] == 0x15 and line[3] == call and i > 3)
            assert at + 1 + lines[at][1] == len(lines) - 1, "the check must jump to the refusal"
        if machine == "x86_64":
            # the other way of calling this kernel knows, whose numbers mean something else: refused too
            other = [i for i, line in enumerate(lines) if line[0] == 0x35 and line[3] == 0x40000000]
            assert other and other[0] + 1 + lines[other[0]][1] == len(lines) - 1


def answer_of(raw: bytes, machine_name: int, call: int, first_argument: int = 0) -> str:
    """What the filter says to one call: run its program as the kernel does, on the kind of
    machine, the number of the call and the low half of its first argument."""
    lines = [struct.unpack("HBBI", raw[i:i + 8]) for i in range(0, len(raw), 8)]
    data = {0: call, 4: machine_name, 16: first_argument}
    at, held = 0, 0
    for _ in range(len(lines) + 1):
        code, yes, no, value = lines[at]
        if code == 0x20:                                   # load a word of what describes the call
            held, at = data[value], at + 1
        elif code == 0x15:                                 # equal?
            at += 1 + (yes if held == value else no)
        elif code == 0x35:                                 # at least?
            at += 1 + (yes if held >= value else no)
        elif code == 0x05:                                 # go on further down
            at += 1 + value
        elif code == 0x06:                                 # the answer
            return {0x7FFF0000: "goes on", 0x00050001: "not permitted", 0x00050016: "invalid argument", 0x00050026: "no such call"}.get(value, hex(value))
        else:
            raise AssertionError(f"the filter holds a step this test does not know: {lines[at]}")
    raise AssertionError("the filter gives no answer")


def test_the_filter_answers_every_call_that_locks_in_or_hands_over_pages():
    """The four calls that move pages into a pipe or a socket without copying answer "invalid
    argument"; the calls for asynchronous work and the making of a cryptography socket are not
    permitted; the making of a ring of the older asynchronous work answers that there is no such
    call; every other socket and every ordinary call goes on. A call made the way another
    kind of machine makes it is not permitted whatever it is, one of the four included."""
    build = judge_module.refusing_filter
    numbers = {"x86_64": {"name": 0xC000003E, "moving": (40, 275, 276, 278), "socket": 41, "read": 0, "outliving": 29, "rings": 206},
               "aarch64": {"name": 0xC00000B7, "moving": (71, 76, 77, 75), "socket": 198, "read": 63, "outliving": 194, "rings": 0}}
    for machine, known in numbers.items():
        raw = build(machine)
        for call in known["moving"]:
            assert answer_of(raw, known["name"], call) == "invalid argument", (machine, call)
        for call in (425, 426, 427, known["outliving"]):
            assert answer_of(raw, known["name"], call) == "not permitted", (machine, call)
        assert answer_of(raw, known["name"], known["rings"]) == "no such call", "the rings of the older asynchronous work"
        assert answer_of(raw, known["name"], known["socket"], 38) == "not permitted", "a cryptography socket"
        for family in (1, 2, 10):
            assert answer_of(raw, known["name"], known["socket"], family) == "goes on", (machine, family)
        assert answer_of(raw, known["name"], known["read"]) == "goes on"
        # the same numbers on another kind of machine mean other calls: all of it is refused
        other = next(n["name"] for m, n in numbers.items() if m != machine)
        for call in (*known["moving"], known["read"], known["socket"]):
            assert answer_of(raw, other, call) == "not permitted", (machine, call)
        for call in (187, 239):                             # sendfile as a 32-bit program calls it
            assert answer_of(raw, 0x40000003, call) == "not permitted"
    # the second way of calling a 64-bit kernel of this kind, whose numbers lie higher
    raw = build("x86_64")
    for call in numbers["x86_64"]["moving"]:
        assert answer_of(raw, 0xC000003E, 0x40000000 + call) == "not permitted"


def test_no_user_but_root_can_make_a_pair_of_terminals(tmp_path):
    """What a pair of terminals buffers is memory the kernel charges to no container. Before
    anything of the program's side runs, the judge closes the device that makes such pairs to
    every user; a link to it is left alone, and so is a container that has none."""
    close = getattr(judge_module, "close_terminals", None)
    assert close is not None, "the judge leaves the device that makes pairs of terminals open to every user"
    device, link = tmp_path / "ptmx", tmp_path / "link"
    device.write_text("")
    device.chmod(0o666)
    link.symlink_to(device)
    close(devices=(link, tmp_path / "none"))
    assert stat.S_IMODE(device.stat().st_mode) == 0o666, "a link is not followed"
    close(devices=(device, link))
    assert stat.S_IMODE(device.stat().st_mode) == 0


def test_the_judge_refuses_a_machine_it_has_no_call_filter_for(tmp_path, monkeypatch):
    group, info = tmp_path / "group", tmp_path / "meminfo"
    group.mkdir()
    info.write_text("MemTotal:        2000000 kB\n")
    (group / "memory.max").write_text(f"{1024 * 2**20}\n")
    (group / "pids.max").write_text("512\n")
    monkeypatch.setattr(judge_module.platform, "machine", lambda: "riscv64")
    with pytest.raises(judge_module.JudgeFault) as refused:
        judge_module.container_caps(None, group=group, machine_info=info)
    assert json.loads(str(refused.value))["key"] == "machine"


def test_processes_are_ended_even_when_no_new_process_can_be_made(monkeypatch):
    """The container is at its cap on processes, so the judge cannot make one to end the others with."""
    import signal
    import subprocess
    running = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"]) for _ in range(3)]
    side = judge_module.Side(os.getuid(), os.getgid())
    monkeypatch.setattr(side, "alive", lambda: [p.pid for p in running if p.poll() is None])

    def no_process(*args):
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(judge_module.os, "fork", no_process)
    try:
        assert side.end() is True
        assert all(p.poll() == -signal.SIGKILL for p in running)
    finally:
        monkeypatch.undo()
        for p in running:
            p.kill()
            p.wait()


def test_when_no_new_process_can_be_made_every_process_is_stopped_before_any_is_ended(monkeypatch):
    """A stopped process makes no new ones. Ending them one by one while the others run lets each
    place that comes free be taken again; so all are stopped first and ended after."""
    import signal
    import subprocess
    running = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"]) for _ in range(3)]
    mine = {p.pid for p in running}
    side = judge_module.Side(os.getuid(), os.getgid())
    monkeypatch.setattr(side, "alive", lambda: [p.pid for p in running if p.poll() is None])

    def no_process(*args):
        raise OSError(11, "Resource temporarily unavailable")

    sent, really = [], os.kill

    def note(pid, sig):
        if pid in mine:
            sent.append((pid, sig))
        really(pid, sig)

    monkeypatch.setattr(judge_module.os, "fork", no_process)
    monkeypatch.setattr(judge_module.os, "kill", note)
    try:
        assert side.end() is True
    finally:
        monkeypatch.undo()
        for p in running:
            p.kill()
            p.wait()
    first_end = next(i for i, (pid, sig) in enumerate(sent) if sig == signal.SIGKILL)
    assert {pid for pid, sig in sent[:first_end] if sig == signal.SIGSTOP} == mine, sent


def test_each_judging_draws_a_user_number_of_its_own():
    drawn = {judge_module.free_user_id() for _ in range(20)}
    assert len(drawn) == 20 and all(number >= 200_000 for number in drawn)


def many_files(folder: Path, count: int) -> Path:
    folder.mkdir(parents=True)
    for i in range(count):
        (folder / f"file-{i}").write_text("x")
    return folder


def test_a_tree_with_more_entries_than_the_judge_handles_is_given_up_on(tmp_path):
    much = getattr(judge_module, "TooMuchLeft", None)
    assert much is not None, "the judge handles a tree of any size, for as long as it takes"
    with pytest.raises(much):
        judge_module.remove_tree(many_files(tmp_path / "remove", 10), most=5)
    with pytest.raises(much):
        judge_module.walk_tree(many_files(tmp_path / "walk", 10), lambda folder, name, info: None, most=5)
    assert judge_module.walk_tree(tmp_path / "walk", lambda folder, name, info: None, most=10) == 10


LAYOUTS = ("files in one folder", "empty folders in one folder", "files and empty folders in one folder",
           "folders inside each other")


def lay_out(top: Path, layout: str) -> None:
    """A tree as a program's side may leave it, in one of four layouts."""
    if layout == "folders inside each other":
        here = top
        for _ in range(60):
            (here / "f").write_text("")
            here = here / "d"
            here.mkdir()
        return
    for n in range(300):
        if layout == "files in one folder" or (layout == "files and empty folders in one folder" and n % 2):
            (top / f"f{n}").write_text("")
        else:
            (top / f"d{n}").mkdir()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_removing_reads_each_folder_at_most_twice_whatever_the_layout(tmp_path, monkeypatch, layout):
    """Removing reads a folder once, and once more when the folders in it are gone, so its time grows
    with the entries and no faster: it never reads a folder again from its start for every folder
    in it."""
    top = tmp_path / "left"
    top.mkdir()
    lay_out(top, layout)
    reads: dict[int, int] = {}
    listing = os.scandir

    def counted(folder):
        number = os.fstat(folder).st_ino if isinstance(folder, int) else os.stat(folder).st_ino
        reads[number] = reads.get(number, 0) + 1
        return listing(folder)
    monkeypatch.setattr(judge_module.os, "scandir", counted)
    judge_module.remove_tree(top)
    monkeypatch.undo()
    assert not top.exists()
    assert max(reads.values()) <= 2, f"a folder was read {max(reads.values())} times"


def test_walking_and_removing_what_the_program_left_share_one_time_in_a_step(tmp_path, monkeypatch):
    """Every walk and removal of what the program's side left in a step draws on one time: what one
    takes, the next has no more. When it is spent, a walk or a removal gives up with that reason."""
    clock = getattr(judge_module, "TREE_CLOCK", None)
    assert clock is not None, "walking and removing what the program's side left have no time limit"
    much = judge_module.TooMuchLeft
    first, second = many_files(tmp_path / "first", 20), many_files(tmp_path / "second", 40)
    third = many_files(tmp_path / "third", 5)
    unlink, real = os.unlink, judge_module.time

    class Ahead:
        """The judge's clock, which each removal of a file moves on by 0.2 seconds instead of taking them."""
        seconds = 0.0

        def __getattr__(self, name):
            return getattr(real, name)

        def monotonic(self):
            return real.monotonic() + self.seconds
    ahead = Ahead()

    def slow(*args, **kwargs):
        ahead.seconds += 0.2
        return unlink(*args, **kwargs)
    monkeypatch.setattr(clock, "left", 10.0)
    monkeypatch.setattr(judge_module, "time", ahead)
    monkeypatch.setattr(judge_module.os, "unlink", slow)
    judge_module.remove_tree(first)                 # 4 of the 10 seconds
    assert not first.exists()
    with pytest.raises(much, match="seconds"):
        judge_module.remove_tree(second)            # needs 8 seconds, 6 are left; alone it would have had 10
    with pytest.raises(much, match="seconds"):
        judge_module.walk_tree(third, lambda folder, name, info: None)


@pytest.mark.parametrize("routine", ["walking", "removing"])
def test_walking_and_removing_give_up_between_folders_once_the_time_is_spent(tmp_path, monkeypatch, routine):
    """The time is looked at before each folder is entered, not only at each entry read: a tree of
    empty folders whose time runs out while they are entered one after another is given up too."""
    clock = getattr(judge_module, "TREE_CLOCK", None)
    assert clock is not None, "walking and removing what the program's side left have no time limit"
    top = tmp_path / "left"
    top.mkdir()
    for n in range(100):
        (top / f"d{n}").mkdir()
    name = "rmdir" if routine == "removing" else "open"
    real = getattr(os, name)

    def slow(*args, **kwargs):
        time.sleep(0.005)
        return real(*args, **kwargs)
    monkeypatch.setattr(clock, "left", 0.2)
    monkeypatch.setattr(judge_module.os, name, slow)
    with pytest.raises(judge_module.TooMuchLeft, match="seconds"):
        if routine == "removing":
            judge_module.remove_tree(top)
        else:
            judge_module.walk_tree(top, lambda folder, name, info: None)


# a test that leaves files in the cases' folder itself, as a case runner that was ended can
LEAVES_FILES = ('def test_leaves():\n    root = json.load(open(os.environ["DODAM_CLI_CONFIG"]))["work_root"]\n'
                '    for n in range(20):\n        open(os.path.join(root, f"left-{n}"), "w").close()\n')
RUNS_OK = "def test_ok():\n    with case() as c:\n        assert c.run([]).out == 'ok\\n'\n"
WHERE_TIME_RUNS_OUT = {
    "where a dependency folder goes": {"functional/test_ok.py": RUNS_OK},
    "in the cases' folder before the next file of tests": {"functional/test_a.py": LEAVES_FILES,
                                                            "functional/test_ok.py": RUNS_OK},
    "in the cases' folder after the last file of tests": {"functional/test_a.py": LEAVES_FILES},
}


@pytest.mark.parametrize("where", WHERE_TIME_RUNS_OUT)
def test_a_step_whose_walking_and_removing_runs_out_of_time_fails_with_a_result(tmp_path, monkeypatch, where):
    """When walking and removing what the program's side left has taken all the time a step gives
    it, the step fails with that reason and a result is written: before the build, where a
    dependency folder goes, and when the cases' folder is emptied after a file of tests. No file
    of tests runs after that, the result gives the reason in a field of its own, and nothing is
    timed once the step is judged. Here the step gives no time at all."""
    monkeypatch.setattr(judge_module, "TREE_SECONDS", 0, raising=False)
    deps = tmp_path / "deps"
    (deps / "node_modules").mkdir(parents=True)
    if where == "where a dependency folder goes":
        many_files(tmp_path / "app/node_modules", 10)
    else:
        (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    manifest = ""
    for file, source in WHERE_TIME_RUNS_OUT[where].items():
        (tests_dir / file).parent.mkdir(parents=True, exist_ok=True)
        (tests_dir / file).write_text(HEAD + source)
        manifest += f'[[test]]\nfile = "{file}"\nkind = "functional"\nstep = "build"\n\n'
    (tests_dir / "manifest.toml").write_text(manifest)
    env = lang_env(DEPS_DIR=str(deps), DEPS_LINKS="node_modules")
    try:
        result = judge(tests_dir, tmp_path / "app", "build", tmp_path / "out", lang_env=env, **unseparated(judge))
    except Exception as error:
        result = error
    assert isinstance(result, dict), f"the time running out ended the judge: {result!r}"
    assert (tmp_path / "out/reward.json").is_file()
    assert result["reward"]["reward"] == 0.0, f"the step came out as passed: {result['reward']}"
    if where == "where a dependency folder goes":
        assert result["reward"]["build_ok"] == 0.0 and "seconds" in result["report"]["build_ended"], result["report"]
    else:
        told = result["report"]["tests"]
        assert "seconds" in result["report"].get("left_behind", ""), "the result has no field that gives the reason"
        assert any("seconds" in t["message"] for t in told), f"the result does not say why the step failed: {told}"
        assert not any(t["name"] == "test_ok" for t in told), f"a file of tests ran after the time ran out: {told}"
    clock = getattr(judge_module, "TREE_CLOCK", None)
    assert clock is None or clock.left is None, "walking and removing are still timed after the step"


# a program that leaves twenty files in its working folder
LEAVES_IN_ITS_FOLDER = "import os\nfor n in range(20):\n    open(f'left-{n}', 'w').close()\nprint('ok')\n"
# a file of tests in whose case runner every file is removed slowly, really: the time it takes is the time this test
# is about
SLOW_REMOVAL = ("import dodam_cli\n_unlink = dodam_cli._judge.os.unlink\n\n\n"
                "def _slowly(*args, **kwargs):\n    time.sleep(0.03)\n    return _unlink(*args, **kwargs)\n\n\n"
                "dodam_cli._judge.os.unlink = _slowly\n\n\n")


def removal_moves_the_clock(seconds: float) -> str:
    """Test code for a case runner in which every file the judge removes moves the judge's clock on by `seconds`
    instead of taking them. How much of the step's time for walking and removing a removal uses is then that,
    however busy the machine is; only the few milliseconds of real work come on top. The notes the runner leaves
    the judge keep the real time at which a removal began, so that the judge, which reads them on its own clock
    while the runner removes, never sees a removal that began later than now."""
    return ("import dodam_cli\n_judge = dodam_cli._judge\n_unlink, _real, _note = _judge.os.unlink, _judge.time, "
            "_judge._note_progress\n\n\n"
            "class _Ahead:\n    seconds = 0.0\n\n    def __getattr__(self, name):\n        return getattr(_real, name)\n\n"
            "    def monotonic(self):\n        return _real.monotonic() + self.seconds\n\n\n"
            "_clock = _Ahead()\n\n\n"
            f"def _slowly(*args, **kwargs):\n    _clock.seconds += {seconds!r}\n    return _unlink(*args, **kwargs)\n\n\n"
            "def _noted(record):\n    if 'at' in record:\n        record = {**record, 'at': record['at'] - _clock.seconds}\n"
            "    return _note(record)\n\n\n"
            "_judge.time, _judge.os.unlink, _judge._note_progress = _clock, _slowly, _noted\n\n\n")


def said_of(result: dict) -> dict[str, tuple[str, str]]:
    return {t["name"]: (t["outcome"], t["message"]) for t in result["report"]["tests"]}


def test_the_case_runners_remove_on_the_time_the_step_gives_all_walking_and_removing(tmp_path, monkeypatch):
    """A case runner removes each case's folder and empties the cases' folder before each case, and
    it does so on the one time the step gives all walking and removing. Here the step gives none: the
    first case whose folder holds what the program left fails, the cases after it are no pass, no
    later file of tests runs, and a result is written."""
    monkeypatch.setattr(judge_module, "TREE_SECONDS", 0, raising=False)
    tests = {"functional/test_a.py": RUNS_OK.replace("test_ok", "test_first") + "\n\n" + RUNS_OK.replace("test_ok", "test_second"),
             "functional/test_b.py": RUNS_OK.replace("test_ok", "test_in_a_later_file")}
    result = judge_program(tmp_path, LEAVES_IN_ITS_FOLDER, tests)
    said = said_of(result)
    for name in ("test_first", "test_second", "test_b.py"):     # a file of tests that does not run is named by its file
        assert said[name][0] == "failed" and "seconds" in said[name][1], said
    assert "seconds" in result["report"].get("left_behind", ""), result["report"]
    assert (tmp_path / "out/reward.json").is_file() and result["reward"]["reward"] == 0.0


def test_what_a_case_runner_used_of_that_time_is_taken_back(tmp_path, monkeypatch):
    """Each case runner is given what is left of the step's time for walking and removing, and what it
    used is taken back. Here the step gives six seconds, and removing what one case left takes four of
    them, on the judge's clock: the case of the first file passes with two to spare, and the case of the
    second finds two left where it needs four. A judge that took nothing back would give it six."""
    monkeypatch.setattr(judge_module, "TREE_SECONDS", 6, raising=False)
    slow = removal_moves_the_clock(0.2)
    tests = {"functional/test_a.py": slow + RUNS_OK.replace("test_ok", "test_in_the_first_file"),
             "functional/test_b.py": slow + RUNS_OK.replace("test_ok", "test_in_the_second_file")}
    result = judge_program(tmp_path, LEAVES_IN_ITS_FOLDER, tests)
    said = said_of(result)
    assert said["test_in_the_first_file"] == ("passed", ""), said
    assert said["test_in_the_second_file"][0] == "failed" and "seconds" in said["test_in_the_second_file"][1], said


def test_the_time_a_case_runner_spends_after_its_last_removal_is_not_taken_from_the_steps_time(tmp_path, monkeypatch):
    """A case runner notes what is left of the step's time for walking and removing after every
    removal as well as before it. Here the step gives three seconds; the first file's runner removes
    little and then waits four and a half, longer than the whole time; the second file's case needs one
    second of it, on the judge's clock. A judge that counted the wait would leave it nothing."""
    monkeypatch.setattr(judge_module, "TREE_SECONDS", 3, raising=False)
    waits = "def test_waits_after_it():\n    time.sleep(4.5)\n"
    tests = {"functional/test_a.py": RUNS_OK.replace("test_ok", "test_removes_little") + "\n\n" + waits,
             "functional/test_b.py": removal_moves_the_clock(0.05) + RUNS_OK.replace("test_ok", "test_needs_some")}
    result = judge_program(tmp_path, LEAVES_IN_ITS_FOLDER, tests)
    said = said_of(result)
    assert said["test_removes_little"] == ("passed", "") and said["test_waits_after_it"] == ("passed", ""), said
    assert said["test_needs_some"] == ("passed", ""), said


def test_a_case_runners_walking_and_removing_is_not_taken_from_its_files_time(tmp_path, monkeypatch, capsys):
    """What a case runner spends removing what the program left is drawn from the step's time for
    walking and removing, not from the time of its file of tests, also for the runner started after
    it. Here the file has twelve seconds, removing what its first case left takes twenty, really, and
    the runner is ended after it, so a second runner judges the last case. A judge that drew the removal
    from the file's time would end the runner eight seconds before the removal is done."""
    monkeypatch.setattr(judge_module, "FILE_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(judge_module, "CASE_SECONDS", 0, raising=False)
    program = ("import sys\nif sys.argv[1:] == ['leave']:\n    for n in range(100):\n"
               "        open(f'left-{n}', 'w').close()\nprint('ok')\n")
    slow = SLOW_REMOVAL.replace("time.sleep(0.03)", "time.sleep(0.2)")
    tests = slow + ("def test_leaves():\n    with case() as c:\n        assert c.run(['leave']).out == 'ok\\n'\n\n\n"
                    "def test_ends_its_runner():\n    os.kill(os.getpid(), 9)\n\n\n") + RUNS_OK
    code, out = judge_command(tmp_path, program, {"functional/test_slow.py": tests}, CASE_TIMEOUT_SEC="3")
    assert code == 0 and (out / "reward.json").is_file(), capsys.readouterr()
    report = json.loads((out / "details.json").read_text())
    assert [(t["name"], t["outcome"]) for t in report["tests"]] == [
        ("test_leaves", "passed"), ("test_ends_its_runner", "failed"), ("test_ok", "passed")], report["tests"]


def test_no_case_runner_starts_on_a_cases_folder_the_judge_could_not_empty(tmp_path, monkeypatch):
    """A case runner is ended while what a case left lies in the cases' folder, and the step's time for
    walking and removing is spent, so the judge cannot empty it. No new runner starts on it: the
    cases that are left are no pass with that reason."""
    monkeypatch.setattr(judge_module, "TREE_SECONDS", 0, raising=False)
    ran = tmp_path / "ran"
    ran.mkdir()
    tests = {"functional/test_ended.py": f'''
def test_leaves_and_ends_its_runner():
    root = json.load(open(os.environ["DODAM_CLI_CONFIG"]))["work_root"]
    for n in range(20):
        open(os.path.join(root, f"left-{{n}}"), "w").close()
    os.kill(os.getpid(), 9)


def test_second():
    open(os.path.join({str(ran)!r}, "second"), "w").close()
    with case() as c:
        assert c.run([]).out == "ok\\n"


def test_third():
    open(os.path.join({str(ran)!r}, "third"), "w").close()
    with case() as c:
        assert c.run([]).out == "ok\\n"
'''}
    result = judge_program(tmp_path, "print('ok')\n", tests)
    said = said_of(result)
    assert said["test_leaves_and_ends_its_runner"][0] == "failed", said
    for name in ("test_second", "test_third"):
        assert said[name][0] == "failed" and "seconds" in said[name][1], said
    assert sorted(path.name for path in ran.iterdir()) == [], "a case runner started on what the judge could not empty"


def test_a_removal_that_fails_for_a_reason_no_program_causes_is_a_fault_of_the_judge(tmp_path, monkeypatch, capsys):
    """Only what cannot be removed whatever the judge does, or the step's time for removing running
    out, is the program's doing. A removal that fails otherwise is the judge's fault, in the case
    runner as in the judge, and never fails a case or the step."""
    in_the_runner = ("import errno\nimport dodam_cli\n\n\ndef _fails(*args, **kwargs):\n"
                     "    raise OSError(errno.EIO, 'Input/output error')\n\n\n" + RUNS_OK.replace(
                         "    with case() as c:\n", "    with case() as c:\n        dodam_cli._judge.remove_tree = _fails\n"))
    code, out = judge_command(tmp_path / "runner", "print('ok')\n", {"functional/test_removal.py": in_the_runner})
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert "remove" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]

    before_a_case = ("import errno\nimport dodam_cli\n\n\ndef _fails(*args, **kwargs):\n"
                     "    raise OSError(errno.EIO, 'Input/output error')\n\n\n" + RUNS_OK.replace(
                         "    with case() as c:\n", "    dodam_cli._judge.remove_tree = _fails\n    with case() as c:\n"))
    code, out = judge_command(tmp_path / "before", "print('ok')\n", {"functional/test_before.py": before_a_case})
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert "case" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]

    def fails(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(judge_module, "remove_tree", fails)
    code, out = judge_command(tmp_path / "judge", "print('ok')\n", {"functional/test_ok.py": RUNS_OK})
    assert code == 3 and not (out / "reward.json").exists(), capsys.readouterr()
    assert "empty" in json.loads((out / "judge-fault.json").read_text())["judge_fault"]


@pytest.mark.parametrize("refused, fault", [(errno.EPERM, False), (errno.EIO, True)],
                         ids=["refused whatever the judge does", "an error no program causes"])
def test_what_lies_where_a_dependency_folder_goes_and_cannot_be_removed(tmp_path, monkeypatch, refused, fault):
    """What the working phase left where a pinned dependency folder goes and what cannot be removed
    whatever the judge does fails the step, with a result. A removal that fails for a reason no
    program can cause is a fault of the judge."""
    remove = judge_module.remove_tree

    def refusing(top, *args, **kwargs):
        if Path(top).name == "node_modules":
            raise OSError(refused, os.strerror(refused))
        return remove(top, *args, **kwargs)
    monkeypatch.setattr(judge_module, "remove_tree", refusing)
    deps = tmp_path / "deps"
    (deps / "node_modules").mkdir(parents=True)
    many_files(tmp_path / "app/node_modules", 3)
    (tmp_path / "app/main.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    (tests_dir / "functional").mkdir(parents=True)
    (tests_dir / "functional/test_ok.py").write_text(HEAD + RUNS_OK)
    (tests_dir / "manifest.toml").write_text('[[test]]\nfile = "functional/test_ok.py"\nkind = "functional"\nstep = "build"\n')
    env = lang_env(DEPS_DIR=str(deps), DEPS_LINKS="node_modules")
    if fault:
        with pytest.raises(judge_module.JudgeFault):
            judge(tests_dir, tmp_path / "app", "build", tmp_path / "out", lang_env=env, **unseparated(judge))
        return
    result = judge(tests_dir, tmp_path / "app", "build", tmp_path / "out", lang_env=env, **unseparated(judge))
    assert result["reward"]["build_ok"] == 0.0 and "cannot be removed" in result["report"]["build_ended"], result["report"]
    assert (tmp_path / "out/reward.json").is_file()


@pytest.mark.parametrize("refused, fault", [(errno.EPERM, False), (errno.EIO, True)],
                         ids=["refused whatever the judge does", "an error no program causes"])
def test_what_the_build_left_in_the_memory_folder_and_cannot_be_removed(tmp_path, monkeypatch, refused, fault):
    """What the build left in the container's memory-backed folder and what cannot be removed whatever
    the judge does fails the step: no file of tests runs, and the reason is given. A removal that
    fails for a reason no program can cause is a fault of the judge."""
    app, cases, memory = tmp_path / "app", tmp_path / "cases", tmp_path / "memory"
    for folder in (app, cases, memory):
        folder.mkdir()
    (memory / "left-by-the-build").write_text("x")
    monkeypatch.setattr(judge_module, "close_writable_places", lambda *args, **kwargs: 0)
    monkeypatch.setattr(judge_module, "settle_access_times", lambda *args, **kwargs: 0, raising=False)
    monkeypatch.setattr(judge_module, "MEMORY_FOLDER", memory)
    monkeypatch.setattr(judge_module, "CASES_DIR", cases)
    remove = judge_module.remove_tree

    def refusing(top, *args, **kwargs):
        if Path(top) == memory:
            raise OSError(refused, os.strerror(refused))
        return remove(top, *args, **kwargs)
    monkeypatch.setattr(judge_module, "remove_tree", refusing)
    if fault:
        with pytest.raises(judge_module.JudgeFault):
            judge_module.CliRun(app, lang_env(), "someone", {"PATH": "/usr/bin"})
        return
    run = judge_module.CliRun(app, lang_env(), "someone", {"PATH": "/usr/bin"})
    try:
        assert "cannot be emptied" in run.left and os.strerror(refused) in run.left, run.left
    finally:
        run.close()


def test_a_cases_folder_the_judge_cannot_empty_before_the_first_case_is_a_fault_of_the_judge(tmp_path, monkeypatch):
    """Before the first case nothing of the program's side can have written the cases' folder: when
    it cannot be emptied then, the judge has failed."""
    app, cases = tmp_path / "app", tmp_path / "cases"
    app.mkdir()
    cases.mkdir()
    monkeypatch.setattr(judge_module, "close_writable_places", lambda *args, **kwargs: 0)
    monkeypatch.setattr(judge_module, "MEMORY_FOLDER", tmp_path / "no-memory-folder")
    monkeypatch.setattr(judge_module, "CASES_DIR", cases)
    remove = judge_module.remove_tree

    def failing(top, *args, **kwargs):
        if Path(top) == cases:
            raise OSError(errno.EIO, "Input/output error")
        return remove(top, *args, **kwargs)
    monkeypatch.setattr(judge_module, "remove_tree", failing)
    # the fault is that one, and no other the judge would meet later on the way to the first case
    with pytest.raises(judge_module.JudgeFault, match="cannot empty the folder the cases live in before the first case"):
        judge_module.CliRun(app, lang_env(), "someone", {"PATH": "/usr/bin"})


def test_the_case_runner_is_told_the_judges_own_processes_where_the_cases_run_as_a_working_user(tmp_path, monkeypatch):
    """Where the cases run as a working user, in a container, the case runner's settings name the judge's
    own processes, the container's first and the judge itself, whose memory comes out of every figure of
    a case; elsewhere they name none."""
    app, cases = tmp_path / "app", tmp_path / "cases"
    app.mkdir()
    cases.mkdir()
    monkeypatch.setattr(judge_module, "close_writable_places", lambda *args, **kwargs: 0)
    monkeypatch.setattr(judge_module, "settle_access_times", lambda *args, **kwargs: 0, raising=False)
    monkeypatch.setattr(judge_module, "MEMORY_FOLDER", tmp_path / "no-memory-folder")
    monkeypatch.setattr(judge_module, "CASES_DIR", cases)
    for run_as, named in (("someone", ["/proc/1/status", f"/proc/{os.getpid()}/status"]), ("", None)):
        run = judge_module.CliRun(app, lang_env(), run_as, {"PATH": "/usr/bin"})
        try:
            settings = json.loads(run.settings_file.read_text(encoding="utf-8"))
        finally:
            run.close()
        assert settings.get("judge_statuses") == named, (run_as, settings)


def test_a_program_folder_with_too_many_entries_fails_the_step_and_leaves_a_result(tmp_path, monkeypatch):
    """What the working phase left where the pinned dependencies go holds more than the judge handles."""
    monkeypatch.setattr(judge_module, "TREE_ENTRIES", 5, raising=False)
    deps = tmp_path / "deps"
    (deps / "node_modules").mkdir(parents=True)
    many_files(tmp_path / "app/node_modules", 10)
    (tmp_path / "app/main.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    (tests_dir / "functional").mkdir(parents=True)
    (tests_dir / "functional/test_ok.py").write_text(HEAD + "def test_ok():\n    with case() as c:\n        assert c.run([]).out == 'ok\\n'\n")
    (tests_dir / "manifest.toml").write_text('[[test]]\nfile = "functional/test_ok.py"\nkind = "functional"\nstep = "build"\n')
    env = lang_env(DEPS_DIR=str(deps), DEPS_LINKS="node_modules")
    try:
        result = judge(tests_dir, tmp_path / "app", "build", tmp_path / "out", lang_env=env, **unseparated(judge))
    except Exception as error:
        result = error
    assert isinstance(result, dict), f"too many entries ended the judge: {result!r}"
    assert result["reward"]["build_ok"] == 0.0 and result["reward"]["reward"] == 0.0
    assert "more than 5 entries" in result["report"]["build_ended"]
    assert (tmp_path / "out/reward.json").is_file()


def test_the_search_for_places_any_user_may_write_ends_at_its_time_while_it_still_finds_some(tmp_path, monkeypatch):
    """The search is given its time whether or not what it found so far waits to be read: one that
    still finds places when its time is up gives up all the same. Here it has one second, and the
    search, a stand-in, finds places without a pause for three, which always wait to be read."""
    much = judge_module.TooMuchLeft
    monkeypatch.setattr(judge_module, "SEARCH_SECONDS", 1)
    monkeypatch.setattr(judge_module, "WRITABLE_PLACES", 10**12)
    start = subprocess.Popen
    finds = ("import sys, time\nend = time.monotonic() + 3\nwhile time.monotonic() < end:\n"
             "    sys.stdout.buffer.write(b'/nowhere\\0' * 1000)\n")
    monkeypatch.setattr(judge_module.subprocess, "Popen", lambda args, **kwargs: start([sys.executable, "-c", finds], **kwargs))
    # what the search found waits to be read whenever the judge asks, however fast this machine reads
    monkeypatch.setattr(judge_module.select, "select", lambda read, write, special, timeout=None: (list(read), [], []))
    mounts = tmp_path / "mounts"
    mounts.write_text(f"none {tmp_path} tmpfs rw 0 0\n")
    began = time.monotonic()
    with pytest.raises(much):
        for _ in judge_module.writable_by_others(mounts):
            pass
    assert time.monotonic() - began < 2.5, "the search went on past its time"


def test_opening_the_built_program_for_reading_follows_no_link_out_of_it(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not the program's")
    outside.chmod(0o600)
    app = tmp_path / "app"
    app.mkdir()
    (app / "closed.py").write_text("print('ok')\n")
    (app / "closed.py").chmod(0o600)
    (app / "link").symlink_to(outside)
    if hasattr(os, "lchmod"):
        os.lchmod(app / "link", 0o777)      # as a link is on the machines that judge
    judge_module.open_for_reading(app)
    assert (app / "closed.py").stat().st_mode & 0o777 == 0o604
    assert outside.stat().st_mode & 0o777 == 0o600, "a link in the program's folder was followed out of it"


def test_a_build_that_leaves_too_many_places_any_user_may_write_is_given_up_on(tmp_path, monkeypatch):
    much = getattr(judge_module, "TooMuchLeft", None)
    assert much is not None, "the judge lists every place any user may write, however many"
    monkeypatch.setattr(judge_module, "WRITABLE_PLACES", 3, raising=False)
    for i in range(6):
        (tmp_path / f"open-{i}").write_text("x")
        (tmp_path / f"open-{i}").chmod(0o666)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"none {tmp_path} tmpfs rw 0 0\n")
    with pytest.raises(much):
        list(judge_module.writable_by_others(mounts))


def test_a_run_during_which_the_kernel_ended_a_process_for_memory_is_told():
    from dodambench import dodam_cli as runner
    told = getattr(runner, "_ended_by_the_kernel", None)
    assert told is not None, "the case runner does not look whether the kernel ended something"
    assert told(3, 4) is True and told(3, 3) is False


# The tests of the judging in a container read what a container really holds through a helper
# that stands outside it and holds what a judged container must never get. The three tests below
# pin where that helper is and what it touches.
HELPERS_NAMES = ("memory" + ".reclaim", "cgroup" + ".freeze", "drop" + "_caches", "/logs/" + "reading")


def test_nothing_that_judges_names_the_tests_helper_or_the_files_it_writes(cli_task_dir, tmp_path):
    """The helper is in the file of the container tests and nowhere else: not in the judge, the
    case runner, the vetting tool or the converter, not in an image, a task or a workflow, and not
    in a task as it is handed to Harbor. So no judging that counts starts it or mounts what it
    needs."""
    repository = Path(__file__).resolve().parents[2]
    bench = repository / "bench"
    harbor = to_harbor(load_task(cli_task_dir), "python", tmp_path / "harbor")
    places = [bench / "dodambench", bench / "images", bench / "tasks", bench / "harbor", bench / "experiments",
              bench / "tests", repository / ".github", harbor]
    the_tests_own = {bench / "tests/test_cli_isolation.py"}
    looked_at, named = 0, []
    for place in places:
        for path in sorted(place.rglob("*")):
            if not path.is_file() or path in the_tests_own or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            looked_at += 1
            named += [f"{path.relative_to(repository) if repository in path.parents else path.name}: {name}"
                      for name in HELPERS_NAMES if name in text]
    assert looked_at > 100, f"only {looked_at} files were looked at"
    assert named == [], f"the tests' helper, or a file of the kernel's that it writes, is named outside the container tests: {named}"
    from test_cli_isolation import READER
    assert all(name in READER for name in HELPERS_NAMES[:2]) and '"/drop"' in READER, (
        "the helper no longer writes the files this test looks for")


def test_the_tests_helper_is_given_the_judged_containers_folder_and_nothing_else_of_the_machine():
    """The command that starts the helper, word for word, with the image's name written out; what
    the program it is given does is pinned by
    test_the_tests_helper_changes_nothing_outside_the_three_folders_it_is_given, which runs that
    program and watches it. Every word of the command is compared, so that nothing can be added to it under any spelling:
    it has a name, no network, a memory cap and a process cap, and three folders, which are the
    judged container's folder of the kernel's, the machine's file for letting go of unused
    records, and the folder it is asked through."""
    from test_cli_isolation import READER, reader_command
    assert reader_command("judged", "docker/0123abcd") == [
        "docker", "run", "--detach", "--name", "judged-reader", "--network", "none", "--user", "0",
        "--memory", "256m", "--pids-limit", "64",
        "--volume", "/sys/fs/cgroup/docker/0123abcd:/group",
        "--volume", "/proc/sys/vm/drop" + "_caches:/drop",
        "--volume", "judged-asked:/reading",
        "dodam-bench/python:dev", "python3", "-c", READER], "the helper is started with other words than its caps and its three folders"


WATCHED = r'''
import os, sys
log = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)
CHANGES = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def watch(event, args):
    if event == "open":
        if isinstance(args[2], int) and args[2] & CHANGES:
            os.write(log, f"writes\t{args[0]}\n".encode())
    elif event.split(".")[0] in ("os", "shutil", "subprocess", "socket", "ctypes") and event not in (
            "os.listdir", "os.scandir"):
        os.write(log, f"{event}\t{args[0] if args else ''}\n".encode())


sys.addaudithook(watch)
exec(compile(open(sys.argv[2]).read(), "helper", "exec"))
'''


def test_the_tests_helper_changes_nothing_outside_the_three_folders_it_is_given(tmp_path):
    """The helper's program is run here, outside any container, on a folder made up to look like
    a container's folder of the kernel's, and everything it does is watched: every file it opens
    for writing, and every act on files, processes or the network. It writes the two files of the
    kernel's in the folder it was given, the file for letting go, and its answers in the folder it
    is asked through. Nothing else."""
    from test_cli_isolation import READER
    asked, held, drop, machine = tmp_path / "asked", tmp_path / "group", tmp_path / "drop", tmp_path / "meminfo"
    reclaim, freeze = HELPERS_NAMES[:2]
    asked.mkdir()
    held.mkdir()
    for name, text in {"memory.current": "1048576\n", "memory.swap.current": "0\n", "cgroup.events": "populated 1\nfrozen 1\n",
                       freeze: "0\n", reclaim: "", "memory.stat": "anon 4096\nfile 0\nfile_dirty 0\n"}.items():
        (held / name).write_text(text)
    drop.write_text("")
    machine.write_text("".join(f"{name}: 1000 kB\n" for name in ("MemTotal", "MemFree", "Buffers", "Cached", "Shmem", "SReclaimable")))
    program = READER
    for path, here in (('asked, held = "/reading", "/group"', f'asked, held = "{asked}", "{held}"'),
                       ('open("/drop", "wb"', f'open("{drop}", "wb"'),
                       ('open("/proc/meminfo")', f'open("{machine}")')):
        assert program.count(path) == 1, f"the helper no longer names {path} once"
        program = program.replace(path, here)
    (tmp_path / "helper.py").write_text(program)
    (tmp_path / "watched.py").write_text(WATCHED)
    (tmp_path / "log").write_text("")
    helper = subprocess.Popen([sys.executable, str(tmp_path / "watched.py"), str(tmp_path / "log"), str(tmp_path / "helper.py")],
                              cwd=tmp_path, stderr=subprocess.PIPE)
    try:
        (asked / "ask-one").write_text("one")
        deadline = time.monotonic() + 30
        while not (asked / "answer-one").exists() and time.monotonic() < deadline and helper.poll() is None:
            time.sleep(0.01)
        assert (asked / "answer-one").exists(), f"the helper gave no answer: {helper.stderr.read()[-2000:] if helper.poll() is not None else 'it is still running'}"
        answer = json.loads((asked / "answer-one").read_text())
        # the answer is written before the container goes on: the helper is stopped only once it has let it go
        while (held / freeze).read_text() != "0" and time.monotonic() < deadline and helper.poll() is None:
            time.sleep(0.01)
    finally:
        helper.kill()
        helper.wait()
    assert answer["charged"] == 1048576 and answer["dropped"] is True
    assert (held / freeze).read_text() == "0", "the helper left the container it read standing still"
    done = [line.split("\t") for line in (tmp_path / "log").read_text().splitlines()]
    written = {path for event, path in done if event == "writes"}
    assert written == {str(held / freeze), str(held / reclaim), str(drop), str(asked / "answer.tmp")}, (
        f"the helper opened other files for writing than the two of the kernel's in its folder, the file for letting go and its answer: {sorted(written)}")
    other = sorted({(event, path) for event, path in done if event != "writes"})
    assert other == [("os.chmod", str(asked)), ("os.rename", str(asked / "answer.tmp"))], (
        f"the helper did more than close its asking folder to others and hand over its answer: {other}")


FLOODS_BOTH = '''
import os, sys
block = b"x" * 65536
if "floods" in sys.argv:
    while True:
        os.write(1, block)
        os.write(2, block)
print("ok")
'''


def test_what_a_program_can_make_the_case_runner_hold_is_bounded_by_the_limit_on_output(tmp_path):
    """The case runner's own memory is left out of a case's memory. What a program can put into
    that memory is its output, and the runner keeps no more of it than the limit on output. A
    program floods both of its outputs until it is ended; the most memory the runner's Python had
    in use during that run, above what it had before, stays within four times the limit of 64 KB
    and 1 MB."""
    seen = tmp_path / "seen.json"
    result = judge_program(tmp_path, FLOODS_BOTH, {"functional/test_flood.py": f'''
import tracemalloc


def test_the_flood():
    with case() as c:
        tracemalloc.start()
        before, _ = tracemalloc.get_traced_memory()
        r = c.run(["floods"])
        _, highest = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        with open({str(seen)!r}, "w") as f:
            json.dump({{"limit": r.limit, "kept": len(r.out) + len(r.err), "grew": highest - before}}, f)
'''})
    assert outcomes(result) == {"test_the_flood": "failed"} and "output" in message(result, "test_the_flood"), result["report"]
    measured = json.loads(seen.read_text())
    limit = int(LIMITS["CASE_OUTPUT_KB"]) * 1024
    assert measured["limit"] == "output" and measured["kept"] <= limit, measured
    assert measured["grew"] <= 4 * limit + 2**20, (
        f"the case runner's memory grew by {measured['grew']} bytes while a program flooded its output, "
        f"with a limit of {limit} bytes on output")


def test_a_file_whose_cases_are_more_than_the_judge_counted_is_the_judges_fault(tmp_path):
    """The time a file of tests gets is counted from its tests. Should pytest list more cases than
    the judge counted, the file would be judged with less time than the document promises: that is
    a fault of the judge, and no result is written."""
    tests = {"functional/test_more.py": (
        "def test_counted():\n    with case() as c:\n        assert c.run(['x'], stdin='a').exit_code == 0\n\n\n"
        "class TestNotCounted:\n"
        "    def test_one(self):\n        with case() as c:\n            assert c.run(['x'], stdin='a').exit_code == 0\n\n"
        "    def test_two(self):\n        with case() as c:\n            assert c.run(['x'], stdin='a').exit_code == 0\n")}
    with pytest.raises(judge_module.JudgeFault) as fault:
        judge_program(tmp_path, ECHO, tests)
    assert "test_more.py" in str(fault.value)


def test_the_judge_counts_a_files_tests_as_pytest_collects_them(tmp_path):
    """A test whose name begins with `test` without an underscore is a case like any other, and the
    judge's count holds it: the file is judged, not refused."""
    tests = {"functional/test_names.py": (
        "def test_with_underscore():\n    with case() as c:\n        assert c.run(['x'], stdin='a').exit_code == 0\n\n\n"
        "def testwithout():\n    with case() as c:\n        assert c.run(['x'], stdin='a').exit_code == 0\n")}
    result = judge_program(tmp_path, ECHO, tests)
    assert outcomes(result) == {"test_with_underscore": "passed", "testwithout": "passed"}


def test_javas_temporary_folder_is_the_cases_own(tmp_path):
    """The Java runtime takes its temporary folder from its own setting, not from the environment
    the other six runtimes read it from. Its run command sets it to the case's own temporary folder,
    which is where the environment sends the other six."""
    from dodambench.languages import LANGUAGES
    import shlex
    parts = shlex.split(LANGUAGES["java"].run_cmd)
    given = [p.split("=", 1)[1] for p in parts if p.startswith("-Djava.io.tmpdir=")]
    assert len(given) == 1, LANGUAGES["java"].run_cmd
    program = ("import os, sys\n"
               "print(os.path.realpath(sys.argv[1]) == os.path.realpath(os.environ['TMPDIR']))\n")
    tests = {"functional/test_temp.py": (
        f"def test_the_same_folder():\n    with case() as c:\n        r = c.run([{given[0]!r}])\n"
        "        assert r.out == 'True\\n', r\n")}
    assert outcomes(judge_program(tmp_path, program, tests)) == {"test_the_same_folder": "passed"}


def test_a_test_gives_no_more_standard_input_than_the_cases_file_limit(tmp_path):
    """What the case runner keeps of the standard input a test gives is one term of the container's
    memory, bounded by the case's file limit. Up to it the input reaches the program. More is not
    given: the program does not run and the case fails, as for a broken limit, whoever made the
    input, since a test may hand a program's own output back. The judging goes on."""
    program = "import sys\nprint(len(sys.stdin.buffer.read()))\n"
    limit = 8 * 1024 * 1024
    (tmp_path / "within").mkdir()
    (tmp_path / "above").mkdir()
    tests = {"functional/test_input.py": (
        f"def test_up_to_the_limit():\n    with case() as c:\n        r = c.run([], stdin=b'x' * {limit})\n"
        f"        assert r.out == '{limit}\\n', r\n")}
    assert outcomes(judge_program(tmp_path / "within", program, tests)) == {"test_up_to_the_limit": "passed"}
    tests = {"functional/test_input.py": (
        f"def test_above_the_limit():\n    with case() as c:\n        c.run([], stdin=b'x' * {limit + 1})\n\n\n"
        "def test_after():\n    with case() as c:\n        assert c.run([], stdin=b'x').out == '1\\n'\n")}
    result = judge_program(tmp_path / "above", program, tests)
    assert outcomes(result) == {"test_above_the_limit": "failed", "test_after": "passed"}, result["report"]
    assert "input" in message(result, "test_above_the_limit")


def test_a_file_whose_cases_are_fewer_than_the_judge_counted_is_the_judges_fault(tmp_path):
    """The other way round: a test the judge counted that pytest does not collect."""
    tests = {"functional/test_fewer.py": (
        "def test_kept():\n    with case() as c:\n        assert c.run(['x'], stdin='a').exit_code == 0\n\n\n"
        "def test_gone():\n    with case() as c:\n        assert c.run(['x'], stdin='a').exit_code == 0\n\n\n"
        "test_gone = None\n")}
    with pytest.raises(judge_module.JudgeFault) as fault:
        judge_program(tmp_path, ECHO, tests)
    assert "test_fewer.py" in str(fault.value)


@pytest.mark.parametrize("top", ["raise SystemExit\n", "X = 1 // 0\n"])
def test_a_file_of_tests_whose_import_fails_is_the_judges_fault(tmp_path, top):
    """The one way left for the cases pytest lists to differ from the count: the file cannot be
    imported. pytest then lists nothing, and the judge makes it a fault."""
    tests = {"functional/test_broken.py": top + "\n\ndef test_one():\n    with case() as c:\n        c.run([])\n"}
    with pytest.raises(judge_module.JudgeFault) as fault:
        judge_program(tmp_path, ECHO, tests)
    assert "test_broken.py" in str(fault.value)
