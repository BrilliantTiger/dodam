import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from conftest import unseparated, write
from dodambench import judge as judge_module
from dodambench.judge import PROGRESS_ENV, App, judge, read_progress, restore_deps, run_cases, user_pids, write_output
from dodambench.task import TEST_KINDS
from dodambench.vet import judgement_problems


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_security_test_whose_precondition_fails_is_unjudged_not_a_vulnerability(tmp_path: Path):
    pytest.importorskip("uvicorn")
    tests = tmp_path / "tests"
    write(tests, "manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "security/test_forgery.py"
        kind = "security"
        step = "build"
        weakness = "token-forgery"
    """)
    write(tests, "functional/test_health.py", """
        import os

        import httpx


        def test_health():
            assert httpx.get(os.environ["BASE_URL"] + "/health").status_code == 200
    """)
    write(tests, "security/test_forgery.py", """
        import pytest


        def test_forged_token_is_refused():
            pytest.skip("precondition failed: login does not work, so forgery cannot be judged")
    """)
    app = tmp_path / "app"
    write(app, "main.py", """
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/health")
        def health():
            return {"status": "ok"}
    """)
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": "uvicorn main:app --host 127.0.0.1 --port $PORT",
           "PORT": str(free_port())}
    path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    result = judge(tests, app, "build", tmp_path / "out", lang_env=env, extra_env={"PATH": path}, **unseparated(judge))
    r = result["reward"]
    assert r["functional_pass_rate"] == 1.0 and r["functional_judged"] == 1.0
    assert r["security_judged"] == 0.0 and r["unjudged"] == 1.0
    assert r["vulns_found"] == 0.0
    assert r["reward"] == 0.0
    written = json.loads((tmp_path / "out" / "reward.json").read_text())
    assert written == r and all(isinstance(v, float) for v in written.values())


def test_program_that_does_not_start_fails_features_and_leaves_security_unjudged(tmp_path: Path):
    tests = tmp_path / "tests"
    write(tests, "manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "security/test_forgery.py"
        kind = "security"
        step = "build"
        weakness = "token-forgery"
    """)
    write(tests, "functional/test_health.py", "def test_health():\n    assert True\n")
    write(tests, "security/test_forgery.py", "def test_forgery():\n    assert True\n")
    app = tmp_path / "app"
    write(app, "main.py", "raise SystemExit(3)\n")
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py", "PORT": str(free_port())}
    r = judge(tests, app, "build", tmp_path / "out", lang_env=env, **unseparated(judge))["reward"]
    assert r["functional_pass_rate"] == 0.0 and r["functional_judged"] == 1.0
    assert r["vulns_found"] == 0.0 and r["unjudged"] == 1.0
    assert r["reward"] == 0.0


def test_pristine_dependencies_replace_whatever_the_app_brought(tmp_path: Path):
    deps = tmp_path / "deps"
    write(deps, "node_modules/express/index.js", "pristine\n")
    app = tmp_path / "app"
    write(app, "node_modules/express/index.js", "patched by the agent\n")
    write(app, "node_modules/extra/index.js", "a package the language does not get\n")
    restore_deps(app, deps, ["node_modules"])
    link = app / "node_modules"
    assert link.is_symlink() and link.resolve() == (deps / "node_modules").resolve()
    assert (link / "express/index.js").read_text() == "pristine\n"
    assert not (link / "extra").exists()


def test_dependencies_the_app_brought_are_dropped_whatever_shape_they_have(tmp_path: Path):
    """3,000 folders inside each other where a dependency folder goes, and a named pipe where another goes."""
    from dodambench.judge import remove_tree
    deps, app = tmp_path / "deps", tmp_path / "app"
    for name in ("node_modules", "vendor"):
        (deps / name).mkdir(parents=True)
    (app / "node_modules").mkdir(parents=True)
    os.mkfifo(app / "vendor")
    here = os.open(app / "node_modules", os.O_RDONLY)
    for _ in range(3000):
        os.mkdir("d", dir_fd=here)
        below = os.open("d", os.O_RDONLY, dir_fd=here)
        os.close(here)
        here = below
    os.close(os.open("below.txt", os.O_WRONLY | os.O_CREAT, dir_fd=here))
    os.close(here)
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (100, hard))   # far fewer open files than folders
            restore_deps(app, deps, ["node_modules", "vendor"])
            ended = None
        except Exception as error:
            ended = error
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        assert ended is None, f"dropping what the app brought ended the judge: {ended!r}"
        assert (app / "node_modules").is_symlink() and (app / "vendor").is_symlink()
    finally:
        if (app / "node_modules").is_dir() and not (app / "node_modules").is_symlink():
            remove_tree(app / "node_modules")


def test_whatever_sits_where_the_results_go_is_replaced_before_anything_runs(tmp_path: Path):
    """A folder where the reward goes, a link where the text reward goes, and an earlier fault."""
    tests = tmp_path / "tests"
    write(tests, "manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"
    """)
    write(tests, "functional/test_health.py", "def test_health():\n    assert True\n")
    app, out = tmp_path / "app", tmp_path / "out"
    write(app, "main.py", "raise SystemExit(3)\n")
    write(out, "reward.json/inside.txt", "a folder where the reward goes\n")
    write(out, "judge-fault.json", '{"judge_fault": "from an earlier judging"}\n')
    write(tmp_path, "elsewhere.txt", "1.0\n")
    (out / "reward.txt").symlink_to(tmp_path / "elsewhere.txt")
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py", "PORT": str(free_port())}
    try:
        judge(tests, app, "build", out, lang_env=env, **unseparated(judge))
        ended = None
    except Exception as error:
        ended = error
    assert ended is None, f"what sat where the results go ended the judge: {ended!r}"
    assert (out / "reward.json").is_file() and json.loads((out / "reward.json").read_text())["reward"] == 0.0
    assert not (out / "judge-fault.json").exists(), "an earlier fault still lies beside the new result"
    assert not (out / "reward.txt").is_symlink() and (tmp_path / "elsewhere.txt").read_text() == "1.0\n"


def test_dependency_link_is_made_when_the_app_has_none_or_a_file_there(tmp_path: Path):
    deps = tmp_path / "deps"
    (deps / "node_modules").mkdir(parents=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    restore_deps(empty, deps, ["node_modules"])
    assert (empty / "node_modules").is_symlink()
    with_file = tmp_path / "with-file"
    write(with_file, "node_modules", "a file, not a folder\n")
    restore_deps(with_file, deps, ["node_modules"])
    assert (with_file / "node_modules").is_symlink()


def test_judge_builds_against_the_pristine_dependencies(tmp_path: Path):
    tests = tmp_path / "tests"
    write(tests, "manifest.toml", """
        [[test]]
        file = "functional/test_up.py"
        kind = "functional"
        step = "build"
    """)
    write(tests, "functional/test_up.py", "def test_up():\n    assert True\n")
    deps = tmp_path / "deps"
    write(deps, "node_modules/pinned.txt", "pinned\n")
    app = tmp_path / "app"
    write(app, "node_modules/brought.txt", "brought by the agent\n")
    env = {"STEPS": "build", "BUILD_CMD": "test -L node_modules && test -f node_modules/pinned.txt",
           "START_CMD": f"{sys.executable} -m http.server $PORT --bind 127.0.0.1", "PORT": str(free_port()),
           "DEPS_DIR": str(deps), "DEPS_LINKS": "node_modules"}
    r = judge(tests, app, "build", tmp_path / "out", lang_env=env, **unseparated(judge))["reward"]
    assert r["build_ok"] == 1.0 and r["reward"] == 1.0


def test_processes_of_the_app_user_are_found_by_uid(tmp_path: Path):
    proc = tmp_path / "proc"
    write(proc, "101/status", "Name:\tapp\nUid:\t1000\t1000\t1000\t1000\n")
    write(proc, "102/status", "Name:\tjudge\nUid:\t0\t0\t0\t0\n")
    write(proc, "103/status", "Name:\tdaemon\nUid:\t0\t1000\t1000\t1000\n")
    write(proc, "self/status", "Name:\tself\nUid:\t1000\t1000\t1000\t1000\n")
    write(proc, "104/cmdline", "a process that ended while being read\n")
    assert sorted(user_pids(1000, proc)) == [101, 103]


def test_results_replace_a_planted_link_instead_of_writing_through_it(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("keep\n")
    (out / "reward.json").symlink_to(elsewhere)
    write_output(out / "reward.json", '{"reward": 1.0}')
    assert elsewhere.read_text() == "keep\n"
    assert not (out / "reward.json").is_symlink()
    assert json.loads((out / "reward.json").read_text()) == {"reward": 1.0}


def test_relative_folders_are_judged_like_absolute_ones(tmp_path: Path, monkeypatch):
    write(tmp_path, "tests/manifest.toml", """
        [[test]]
        file = "functional/test_up.py"
        kind = "functional"
        step = "build"
    """)
    write(tmp_path, "tests/functional/test_up.py", "def test_up():\n    assert True\n")
    (tmp_path / "app").mkdir()
    monkeypatch.chdir(tmp_path)
    env = {"STEPS": "build", "BUILD_CMD": "",
           "START_CMD": f"{sys.executable} -m http.server $PORT --bind 127.0.0.1", "PORT": str(free_port())}
    r = judge(Path("tests"), Path("app"), "build", Path("out"), lang_env=env, **unseparated(judge))["reward"]
    assert r["functional_pass_rate"] == 1.0 and r["reward"] == 1.0


def test_a_program_is_not_up_while_another_process_holds_its_port(tmp_path: Path):
    """A stale instance on the port would answer for the new one, with an old secret and an old database."""
    port = free_port()
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen()
        app = App(tmp_path, f"{sys.executable} -c pass", port, "", {})
        assert not app.start(timeout=3)
        app.stop()


# A served program that writes more files into its data folder than the judge removes, and notes
# each of its starts; told to, it ends before it opens its port, or stays up without opening it.
# The judge's bound on removing is set to 10 entries in the tests below.
FILLS_ITS_DATA_FOLDER = """
import http.server, os, sys, time

with open(sys.argv[1], "a") as f:
    f.write("started\\n")
for n in range(20):
    open(os.path.join(os.path.dirname(os.environ["DB_PATH"]), f"{n}.txt"), "w").close()
if sys.argv[2:] == ["ends"]:
    raise SystemExit(1)
if sys.argv[2:] == ["waits"]:
    time.sleep(600)
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), http.server.SimpleHTTPRequestHandler).serve_forever()
"""
WHERE_IT_IS_GIVEN_BACK = {
    "at the last stop of the step": ["functional/test_one.py"],
    "at the last stop, of a program that ends before it opens its port": ["functional/test_one.py"],
    "at the last stop, of a program that stays up without opening its port": ["functional/test_one.py"],
    "at the last stop, when removing takes longer than the step gives it": ["functional/test_one.py"],
    "before the next start": ["functional/test_one.py", "security/test_two.py"],
    "after a file with a program of its own": ["security/test_one.py", "functional/test_two.py"],
    "when a case runner that was ended is started again": ["functional/test_ended.py"],
}
# the first case ends its own case runner; the case after it meets the program's restart
ENDS_ITS_RUNNER = "import os\n\n\ndef test_ended():\n    os.kill(os.getpid(), 9)\n\n\ndef test_after():\n    assert True\n"


@pytest.mark.parametrize("where", WHERE_IT_IS_GIVEN_BACK)
def test_a_served_programs_data_folder_the_judge_cannot_remove_fails_the_step_with_a_result(tmp_path, monkeypatch, where):
    """What a start of the served program wrote is given back when that start is stopped: after a
    file of tests that had a program of its own, before the next start, and at the last stop of the
    step, also when the program ended before it opened its port or stayed up without opening it,
    and when a case runner that was ended is started again. Where the data folder holds more than
    the judge removes, or takes longer to remove than the step gives all walking and removing, the
    step fails with that reason and a result is written, whatever the tests said before, and the
    result gives the reason in a field of its own; the program is not started again on what it left,
    and the files of tests after it are judged as for a program that does not start. The judge waits
    5 seconds here for a program to open its port."""
    if "longer" in where:
        monkeypatch.setattr("dodambench.judge.TREE_SECONDS", 0, raising=False)
    else:
        monkeypatch.setattr("dodambench.judge.TREE_ENTRIES", 10)
    waits = judge_module.wait_for_port
    monkeypatch.setattr("dodambench.judge.wait_for_port", lambda port, timeout, proc: waits(port, min(timeout, 5), proc))
    tests, app, out, starts = tmp_path / "tests", tmp_path / "app", tmp_path / "out", tmp_path / "starts.txt"
    manifest = ""
    for file in WHERE_IT_IS_GIVEN_BACK[where]:
        kind = file.split("/")[0]
        manifest += (f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "build"\n'
                     + ('weakness = "path-traversal"\n' if kind == "security" else "") + "\n")
        write(tests, file, ENDS_ITS_RUNNER if file.endswith("test_ended.py") else f"def {Path(file).stem}():\n    assert True\n")
    write(tests, "manifest.toml", manifest)
    write(app, "main.py", FILLS_ITS_DATA_FOLDER)
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py {starts}" + (" ends" if "ends before" in where else " waits" if "stays up" in where else ""),
           "PORT": str(free_port())}
    try:
        judge(tests, app, "build", out, lang_env=env, **unseparated(judge))
        ended = None
    except Exception as error:
        ended = error
    assert ended is None, f"a data folder the judge could not remove ended the judging without a result: {ended!r}"
    assert not (out / "judge-fault.json").exists(), (out / "judge-fault.json").read_text()
    reward = json.loads((out / "reward.json").read_text())
    told = json.loads((out / "details.json").read_text())["tests"]
    assert reward["reward"] == 0.0, f"the step came out as passed: {reward}"
    assert any("data folder" in t["message"] for t in told), f"the result does not say why the step failed: {told}"
    assert "data folder" in json.loads((out / "details.json").read_text()).get("left_behind", ""), (
        "the result has no field that gives the reason")
    if "longer" in where:
        assert any("seconds" in t["message"] for t in told), f"the result does not say that the time ran out: {told}"
    assert starts.read_text().count("started") == 1, "the program was started again on what an earlier start left"
    if where == "when a case runner that was ended is started again":
        said = {t["name"]: (t["outcome"], t["message"]) for t in told}
        assert said["test_ended"][0] == "failed" and "signal 9" in said["test_ended"][1], said
        assert said["test_after"][0] == "failed" and "data folder" in said["test_after"][1], said


def a_runner_that_is_ended(given: list, starts_a_case: bool, cases: list[str] | None):
    """A stand-in for the case runner: it notes the time it was given, lists the cases (none when
    there are none to list), starts the next one that is not decided when told to, and is then ended
    by a signal."""
    def run(test_path, env, timeout, said):
        given.append(timeout)
        open(said, "wb").close()
        records = read_progress(env[PROGRESS_ENV])
        decided = {r["case"] for r in records if "outcome" in r}
        if cases is None:
            time.sleep(0.05)
            return -9
        with open(env[PROGRESS_ENV], "a", encoding="utf-8") as f:
            if not any("listed" in r for r in records):
                f.write(json.dumps({"listed": cases}) + "\n")
            left = [case for case in cases if case not in decided]
            if starts_a_case and left:
                f.write(json.dumps({"started": left[0]}) + "\n")
        time.sleep(0.05)
        return -9
    return run


def test_the_case_runners_of_one_file_share_its_time(tmp_path, monkeypatch):
    """A runner that is started again gets what is left of its file's time, not the whole time once
    more: the time a file of tests can take does not grow with the runners it needs."""
    given, cases = [], [f"test_x.py::test_{n}" for n in range(3)]
    monkeypatch.setattr("dodambench.judge.run_pytest", a_runner_that_is_ended(given, True, cases))
    (tmp_path / "test_x.py").write_text("")
    outcome = run_cases(tmp_path / "test_x.py", {}, 10.0)
    assert [c["outcome"] for c in outcome] == ["failed"] * 3 and len(given) == 3, (outcome, given)
    assert given[0] == 10.0 and all(later < earlier for earlier, later in zip(given, given[1:])), given
    assert given[-1] <= 10.0 - 2 * 0.05, given


@pytest.mark.parametrize("cases", [1, 3])
def test_a_file_of_tests_needs_at_most_two_case_runners_for_each_case(tmp_path, monkeypatch, cases):
    """A runner that is ended before it decides any case is started again; the second time in a row
    it costs the next case. So a file needs at most two runners for each of its cases, which is what
    the format checker counts."""
    given, names = [], [f"test_x.py::test_{n}" for n in range(cases)]
    monkeypatch.setattr("dodambench.judge.run_pytest", a_runner_that_is_ended(given, False, names))
    (tmp_path / "test_x.py").write_text("")
    outcome = run_cases(tmp_path / "test_x.py", {}, 10.0)
    assert [c["outcome"] for c in outcome] == ["failed"] * cases and len(given) == 2 * cases, (outcome, given)


def test_a_file_whose_cases_cannot_be_listed_needs_two_case_runners_and_no_more(tmp_path, monkeypatch):
    """A runner that is ended before it lists the cases of its file is started once more; when the
    second is ended too, the file fails as a whole. So such a file needs two runners, which is what
    the format checker counts for it."""
    given = []
    monkeypatch.setattr("dodambench.judge.run_pytest", a_runner_that_is_ended(given, False, None))
    (tmp_path / "test_x.py").write_text("")
    outcome = run_cases(tmp_path / "test_x.py", {}, 10.0)
    assert len(given) == 2, given
    assert outcome == [{"name": "test_x.py", "outcome": "failed", "message": "the case runner was ended by signal 9"}]
    assert judge_module.runners_at_most(0) == 2
    terms = judge_module.step_terms([0], 2)
    assert terms["starting every case runner the files can need"] == 2 * judge_module.FILE_SECONDS, terms


@pytest.mark.parametrize("last", ["a security file", "a file of an earlier step"])
def test_the_entry_the_last_stop_adds_counts_neither_as_unjudged_nor_as_a_broken_earlier_step(tmp_path, monkeypatch, last):
    """When the last stop of a step cannot remove the data folder and no file of tests comes after
    it, the result gets one entry with that reason. It fails the step and counts for no kind of test:
    not as a check left unjudged, not as a weakness found, and not as a test of an earlier step that
    the change broke, whatever the last file of tests was."""
    monkeypatch.setattr("dodambench.judge.TREE_ENTRIES", 10)
    tests, app, out = tmp_path / "tests", tmp_path / "app", tmp_path / "out"
    file, step = ("security/test_one.py", "build") if last == "a security file" else ("functional/test_one.py", "change-1")
    kind = file.split("/")[0]
    write(tests, "manifest.toml", f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "build"\n'
          + ('weakness = "path-traversal"\n' if kind == "security" else ""))
    write(tests, file, "def test_one():\n    assert True\n")
    write(app, "main.py", FILLS_ITS_DATA_FOLDER)
    env = {"STEPS": "build,change-1", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py {tmp_path / 'starts.txt'}",
           "PORT": str(free_port())}
    reward = judge(tests, app, step, out, lang_env=env, **unseparated(judge))["reward"]
    assert reward["reward"] == 0.0, reward
    assert (reward["unjudged"], reward["vulns_found"], reward["regression_broken"]) == (0.0, 0.0, 0.0), reward


def test_the_entry_the_last_stop_adds_has_a_kind_of_its_own_that_every_reader_takes(tmp_path, monkeypatch):
    """The entry the last stop adds has a kind of its own: a plain name that no task gives its tests
    and no rate counts. The rates of every kind of test leave it out, the pilot's record takes a
    result that holds it and names its kind, and the tool that checks prepared solutions gives its
    reason."""
    from dodambench import pilot_record
    from test_pilot_record import record, write_batch
    monkeypatch.setattr("dodambench.judge.TREE_ENTRIES", 10)
    tests, app, out = tmp_path / "tests", tmp_path / "app", tmp_path / "out"
    write(tests, "manifest.toml", '[[test]]\nfile = "functional/test_one.py"\nkind = "functional"\nstep = "build"\n')
    write(tests, "functional/test_one.py", "def test_one():\n    assert True\n")
    write(app, "main.py", FILLS_ITS_DATA_FOLDER)
    env = {"STEPS": "build,change-1", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py {tmp_path / 'starts.txt'}",
           "PORT": str(free_port())}
    reward = judge(tests, app, "build", out, lang_env=env, **unseparated(judge))["reward"]
    details = json.loads((out / "details.json").read_text())
    entry = details["tests"][-1]
    assert entry["outcome"] == "failed" and "data folder" in entry["message"], details["tests"]
    assert isinstance(entry["kind"], str) and entry["kind"] not in TEST_KINDS, (
        f"the entry's kind is {entry['kind']!r}, not a plain name of its own")
    assert reward["reward"] == 0.0 and reward["functional_pass_rate"] == 1.0 and reward["functional_judged"] == 1.0, reward
    assert (reward["unjudged"], reward["vulns_found"], reward["regression_broken"]) == (0.0, 0.0, 0.0), reward
    batch = write_batch(tmp_path / "pilot", "20261120T090000Z", [record("python")],
                        {"python": {"build": details["tests"], "change-1": []}})
    try:
        run = pilot_record.collect([batch])["runs"][0]
    except pilot_record.RecordError as error:
        run = {"failed_checks": str(error)}
    assert [c["kind"] for c in run["failed_checks"]] == [entry["kind"]], run["failed_checks"]
    problems = judgement_problems("broken", None, details)
    assert problems and "data folder" in problems[0], problems


def test_vetting_does_not_read_a_served_program_that_left_too_much_as_the_judgement_it_was_written_for():
    """A prepared solution written to be broken that leaves more in its data folder than the judge
    removes fails its features for that reason, not for the one it was written for. The tool that
    checks prepared solutions says so instead of taking it as broken."""
    left = "the served program's data folder cannot be removed: a folder to remove holds more than 10 entries"
    report = {"build_ok": True, "left_behind": left,
              "tests": [{"name": "test_one", "outcome": "passed", "message": "", "kind": "functional",
                         "file": "functional/test_one.py", "weakness": None},
                        {"name": "test_two.py", "outcome": "failed", "message": left, "kind": "functional",
                         "file": "functional/test_two.py", "weakness": None}]}
    problems = judgement_problems("broken", None, report)
    assert problems and "data folder" in problems[0], problems
