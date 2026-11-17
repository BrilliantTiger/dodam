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
        assert caps(None, group=group, machine_info=info) == {"memory_mb": 1024, "processes": 512}


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
