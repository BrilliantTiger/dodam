"""Judging a command-line program from outside: arguments, standard input and files in; standard output,
the exit code and files out. These run the judge on this machine, where the program runs as the developer;
what needs the verifier's own user is in test_cli_isolation.py."""
import inspect
import json
import os
import shutil
import stat
import struct
import subprocess
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
            "the search for places any user may write": judge_module.SEARCH_SECONDS,
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

    def taken(**stat: int) -> int:
        (group / "memory.stat").write_text("".join(f"{name} {value * mb}\n" for name, value in stat.items()))
        return judge_module.container_memory(group=group)["taken"] // mb

    # 200 MB of pages of files, all on the lists: none of them counts
    assert taken(anon=200, file=200, shmem=0, inactive_file=150, active_file=50, unevictable=0) == 200
    # 128 MB of them locked: they stand on neither list, and count
    assert taken(anon=200, file=200, shmem=0, inactive_file=50, active_file=22, unevictable=128) == 328, \
        "pages of files that stand on neither list were left out of the count"
    # locked ordinary memory and locked shared memory were counted before and are not counted twice
    assert taken(anon=200, file=200, shmem=40, inactive_file=120, active_file=40, unevictable=90) == 240
    # a kernel that does not say which pages stand on its lists: every page of a file is counted
    assert taken(anon=200, file=200, shmem=0) == 400


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
        return {"taken": figure * MB, "all": figure * MB, "cap": 0, "records": 0}

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
    result = judge_program(tmp_path, "import time\ntime.sleep(0.3)\nprint('fine')\n", tests)
    assert outcomes(result) == {"test_the_figures": "passed"}, message(result, "test_the_figures")


def test_the_kernels_records_are_left_out_only_up_to_a_bound():
    """A quarter of the case's memory limit, never more than 16 MB; what the records grew by beyond
    that counts as the case's memory. A fall of the records gives nothing back."""
    from dodambench import dodam_cli as runner
    bound, counted = getattr(runner, "_records_left_out_at_most", None), getattr(runner, "_records_counted", None)
    assert bound is not None and counted is not None, "the case runner leaves the kernel's records out without a bound"
    mb = 2**20
    assert [bound(limit) for limit in (512, 256, 64, 32, 8)] == [16 * mb, 16 * mb, 16 * mb, 8 * mb, 2 * mb]
    assert counted(10 * mb, 16 * mb) == 0
    assert counted(16 * mb, 16 * mb) == 0
    assert counted(200 * mb, 16 * mb) == 184 * mb
    assert counted(10 * mb, 2 * mb) == 8 * mb
    # the records rise by 10 MB, fall by 10 MB and rise by 10 MB again: 20 MB have been made
    grown, last = 0, None
    for now in (50 * mb, 60 * mb, 50 * mb, 60 * mb):
        grown, last = runner._records_grown(grown, last, now), now
    assert grown == 20 * mb


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
    unlink = os.unlink

    def slow(*args, **kwargs):
        time.sleep(0.02)
        return unlink(*args, **kwargs)
    monkeypatch.setattr(clock, "left", 1.0)
    monkeypatch.setattr(judge_module.os, "unlink", slow)
    judge_module.remove_tree(first)                 # about 0.4 of the 1.0 seconds
    assert not first.exists()
    with pytest.raises(much, match="seconds"):
        judge_module.remove_tree(second)            # needs about 0.8 seconds; about 0.6 are left
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
