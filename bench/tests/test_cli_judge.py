"""Judging a command-line program from outside: arguments, standard input and files in; standard output,
the exit code and files out. These run the judge on this machine, where the program runs as the developer;
what needs the verifier's own user is in test_cli_isolation.py."""
import inspect
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

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
def test_a_program_that_hangs_fails_its_case_only_and_never_hangs_the_judge(tmp_path, how):
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
    secret = tmp_path / "expected-answer.txt"
    secret.write_text("the expected answer")
    program = f'''
import os
os.symlink({str(secret)!r}, "out.txt")
os.symlink({str(tmp_path)!r}, "folder")
'''
    result = judge_program(tmp_path, program, {"security/test_links.py": '''
def test_a_link_left_by_the_program_is_not_followed():
    with case() as c:
        assert c.run([]).exit_code == 0
        assert c.read("out.txt") is None
        assert c.read("folder/expected-answer.txt") is None
        try:
            c.write("folder/planted.txt", "x")
        except AssertionError:
            pass
        else:
            raise AssertionError("wrote through the program's link")
'''})
    assert outcomes(result) == {"test_a_link_left_by_the_program_is_not_followed": "passed"}, result["report"]
    assert not (tmp_path / "planted.txt").exists()


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


def test_the_judging_time_the_checker_demands_is_what_the_judge_allows(cli_task_dir):
    """The checker's bound and the judge's own time limits come from the same numbers."""
    import dodambench.task as task_module
    bound = getattr(task_module, "judging_seconds", None)
    assert bound is not None, "the checker cannot say how long judging may take"
    files, tests, limit = 4, 4, 2
    assert bound(load_task(cli_task_dir)) == (task_module.BUILD_SECONDS + files * judge_module.FILE_SECONDS
                                               + tests * (limit + judge_module.CASE_SECONDS))


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


def test_a_case_whose_folder_cannot_be_removed_fails_and_is_no_fault_of_the_judge(tmp_path):
    tests = {"functional/test_stuck.py": '''
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
            raise OSError(39, "Directory not empty")
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
    (group / "memory.stat").write_text(f"anon {150 * 2**20}\nfile {100 * 2**20}\nshmem {10 * 2**20}\n")
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
                                       f"slab_reclaimable {30 * 2**20}\nslab_unreclaimable {5 * 2**20}\n")
    counted = judge_module.container_memory(group=group)
    assert counted["taken"] == (300 - 100 + 10 - 30) * 2**20, f"{counted['taken'] // 2**20} MB counted as taken"
    assert counted["all"] == 300 * 2**20
    # a kernel that does not say how much it can give back: everything it holds is counted
    (group / "memory.stat").write_text(f"anon {150 * 2**20}\nfile {100 * 2**20}\nshmem {10 * 2**20}\n")
    assert judge_module.container_memory(group=group)["taken"] == (300 - 100 + 10) * 2**20


def test_a_new_high_above_the_limit_is_a_number_in_the_result():
    """How far a new high of the container's memory lies above what the case may take, counted
    from the memory that could not be given back when the run began. It is the most the program
    can have held at once; cache of files is in it, so it is recorded and fails no case."""
    from dodambench import dodam_cli as runner
    above = getattr(runner, "_above_in_a_new_high", None)
    assert above is not None, "the case runner does not say how far a new high lies above the limit"
    mb = 2**20
    assert attempt(above, 100 * mb, 500 * mb, 80 * mb, 288 * mb) == 132 * mb   # a new high, 420 MB above the start
    assert attempt(above, 100 * mb, 300 * mb, 80 * mb, 288 * mb) == 0          # a new high within what the case may take
    assert attempt(above, 600 * mb, 600 * mb, 80 * mb, 288 * mb) == 0          # no new high: an earlier one hides this run
    assert attempt(above, 0, 0, 80 * mb, 288 * mb) == 0                        # a kernel that keeps no high


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
    tool.container_memory = noting("what is held", {"taken": 0, "all": 0, "cap": 0})
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


def test_a_new_high_that_the_cache_of_files_on_disk_can_account_for_fails_no_case():
    """The kernel's highest value counts the cache of files on disk. What the container read from
    disk during the run is the most that cache can have grown by, and it is taken out first."""
    from dodambench import dodam_cli as runner
    unseen, mb = runner._passed_unseen, 2**20
    # 10 MB held, a 400 MB file read from disk: a new high of 478 MB, all of it cache
    assert attempt(unseen, 75 * mb, 478 * mb, 70 * mb, 288 * mb, 400 * mb) is False
    assert attempt(unseen, 75 * mb, 478 * mb, 70 * mb, 96 * mb, 400 * mb) is False     # under a limit of 64 MB as well
    # read and dropped from the cache three times over: more was read than the high could ever hold
    assert attempt(unseen, 75 * mb, 478 * mb, 70 * mb, 288 * mb, 1200 * mb) is False
    # 400 MB taken for a moment and 50 MB read from disk: the reading does not account for it
    assert attempt(unseen, 75 * mb, 528 * mb, 70 * mb, 288 * mb, 50 * mb) is True
    # where the kernel's count of what was read cannot be relied on, the check fails no case
    assert attempt(unseen, 100 * mb, 500 * mb, 80 * mb, 288 * mb, None) is False


def test_what_the_cache_can_account_for_is_what_was_read_and_what_folders_from_elsewhere_hold():
    from dodambench import dodam_cli as runner
    account = getattr(runner, "_cache_can_account_for", None)
    assert account is not None, "the case runner does not say what the cache of files can account for"
    assert account(1000, 5000, 0) == 4000
    assert account(1000, 5000, 300) == 4300          # a folder shared from another machine holds 300 bytes
    assert account(None, 5000, 300) is None and account(1000, None, 300) is None


def test_the_judge_tries_the_kernels_count_of_disk_reads_before_it_relies_on_it(tmp_path, monkeypatch):
    tried = getattr(judge_module, "disk_reads_are_counted", None)
    assert tried is not None, "the judge does not try the kernel's count of what its container read from disk"
    monkeypatch.setattr(judge_module.os, "posix_fadvise", lambda *args: None, raising=False)
    monkeypatch.setattr(judge_module.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    group = tmp_path / "group"
    group.mkdir()
    monkeypatch.setattr(judge_module, "GROUP", group)
    monkeypatch.setattr(judge_module.disk_read, "__defaults__", (group,))
    assert "no count" in tried(tmp_path)                       # the kernel keeps none
    (group / "io.stat").write_text("253:16 rbytes=4096 wbytes=0\n")
    assert "grew by 0 bytes" in tried(tmp_path)                # a count that does not move when a file is read
    assert not (tmp_path / "read-probe").exists()
    grows = iter([0, 8 * 2**20])
    monkeypatch.setattr(judge_module, "disk_read", lambda: next(grows))
    assert tried(tmp_path) == ""                               # a count that grew by what was read


def test_the_check_of_a_new_high_knows_what_folders_from_elsewhere_hold(tmp_path, monkeypatch):
    check = getattr(judge_module, "new_high_check", None)
    assert check is not None, "the judge does not find out whether a new high can be judged"
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared/log").write_bytes(b"x" * 5000)
    monkeypatch.setattr(judge_module, "mounted_from_elsewhere", lambda: [str(tmp_path / "shared")])
    monkeypatch.setattr(judge_module, "disk_reads_are_counted", lambda where: "")
    assert check(tmp_path) == {"available": True, "why_not": "", "elsewhere_bytes": 8192}
    monkeypatch.setattr(judge_module, "ELSEWHERE_ENTRIES", 0)
    refused = check(tmp_path)
    assert refused["available"] is False and "could not be measured" in refused["why_not"], refused
    monkeypatch.setattr(judge_module, "disk_reads_are_counted", lambda where: "the kernel keeps no count")
    assert check(tmp_path) == {"available": False, "why_not": "the kernel keeps no count", "elsewhere_bytes": 0}


def test_the_judge_reads_what_its_container_read_from_disk(tmp_path):
    read = getattr(judge_module, "disk_read", None)
    assert read is not None, "the judge does not read how much its container read from disk"
    assert read(group=tmp_path) is None                      # a kernel that keeps no such count
    (tmp_path / "io.stat").write_text("253:16 rbytes=4096 wbytes=1 rios=1 wios=1 dbytes=0 dios=0\n"
                                      "253:17 rbytes=1048576 wbytes=0 rios=2 wios=0 dbytes=0 dios=0\n")
    assert read(group=tmp_path) == 4096 + 1048576             # every disk together
    (tmp_path / "io.stat").write_text("")
    assert read(group=tmp_path) == 0                          # nothing read yet


def test_the_judge_finds_the_folders_whose_reads_the_kernel_does_not_count(tmp_path, monkeypatch):
    """A folder shared from another machine is no disk of the Docker machine: what is read there is
    cache all the same, but not in the kernel's count of what the container read from disk."""
    find, held = getattr(judge_module, "mounted_from_elsewhere", None), getattr(judge_module, "held_elsewhere", None)
    assert find is not None and held is not None, "the judge does not look for folders mounted from elsewhere"
    mounts = ("1 0 0:42 / / rw - overlay overlay rw\n"
              "2 1 0:53 / /proc rw - proc proc rw\n"
              "3 1 0:57 / /dev/shm rw - tmpfs shm rw\n"
              "4 1 253:17 /hosts /etc/hosts rw - ext4 /dev/vdb1 rw\n"
              "5 1 0:61 /trial/agent /logs/agent rw - virtiofs mount0 rw\n"
              "6 1 0:62 / /logs/artifacts rw shared:1 master:2 - fuse.sshfs host:/x rw\n")
    assert find(mounts) == ["/logs/agent", "/logs/artifacts"]
    (tmp_path / "a/deep").mkdir(parents=True)
    (tmp_path / "a/one").write_bytes(b"x" * 5000)
    (tmp_path / "a/deep/two").write_bytes(b"x")
    (tmp_path / "single").write_bytes(b"x" * 4096)
    assert held([str(tmp_path / "a"), str(tmp_path / "single")]) == 8192 + 4096 + 4096   # whole pages
    monkeypatch.setattr(judge_module, "ELSEWHERE_ENTRIES", 20)
    with pytest.raises(judge_module.TooMuchLeft):
        held([str(many_files(tmp_path / "many", 12))] * 2)     # 24 entries where 20 are the bound


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
