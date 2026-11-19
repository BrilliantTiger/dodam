import json
import os
import socket
import sys
from pathlib import Path

import pytest

from conftest import unseparated, write
from dodambench.judge import App, judge, restore_deps, user_pids, write_output


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
# each of its starts; told to, it ends before it opens its port. The judge's bound on removing is
# set to 10 entries in the test below.
FILLS_ITS_DATA_FOLDER = """
import http.server, os, sys

with open(sys.argv[1], "a") as f:
    f.write("started\\n")
for n in range(20):
    open(os.path.join(os.path.dirname(os.environ["DB_PATH"]), f"{n}.txt"), "w").close()
if sys.argv[2:] == ["ends"]:
    raise SystemExit(1)
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), http.server.SimpleHTTPRequestHandler).serve_forever()
"""
WHERE_IT_IS_GIVEN_BACK = {
    "at the last stop of the step": ["functional/test_one.py"],
    "at the last stop, of a program that ends before it opens its port": ["functional/test_one.py"],
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
    step, also when the program ended before it opened its port, and when a case runner that was
    ended is started again. Where the data folder holds more than the judge removes, the step fails with that reason
    and a result is written, whatever the tests said before; the program is not started again on
    what it left, and the files of tests after it are judged as for a program that does not start."""
    monkeypatch.setattr("dodambench.judge.TREE_ENTRIES", 10)
    tests, app, out, starts = tmp_path / "tests", tmp_path / "app", tmp_path / "out", tmp_path / "starts.txt"
    manifest = ""
    for file in WHERE_IT_IS_GIVEN_BACK[where]:
        kind = file.split("/")[0]
        manifest += (f'[[test]]\nfile = "{file}"\nkind = "{kind}"\nstep = "build"\n'
                     + ('weakness = "path-traversal"\n' if kind == "security" else "") + "\n")
        write(tests, file, ENDS_ITS_RUNNER if file.endswith("test_ended.py") else f"def {Path(file).stem}():\n    assert True\n")
    write(tests, "manifest.toml", manifest)
    write(app, "main.py", FILLS_ITS_DATA_FOLDER)
    env = {"STEPS": "build", "BUILD_CMD": "", "START_CMD": f"{sys.executable} main.py {starts}" + (" ends" if "ends before" in where else ""),
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
    assert starts.read_text().count("started") == 1, "the program was started again on what an earlier start left"
    if where == "when a case runner that was ended is started again":
        said = {t["name"]: (t["outcome"], t["message"]) for t in told}
        assert said["test_ended"][0] == "failed" and "signal 9" in said["test_ended"][1], said
        assert said["test_after"][0] == "failed" and "data folder" in said["test_after"][1], said
