import json
import os
import socket
import sys
from pathlib import Path

import pytest

from conftest import write
from dodambench.judge import judge, restore_deps, user_pids, write_output


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
    result = judge(tests, app, "build", tmp_path / "out", lang_env=env, extra_env={"PATH": path})
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
    r = judge(tests, app, "build", tmp_path / "out", lang_env=env)["reward"]
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
    r = judge(tests, app, "build", tmp_path / "out", lang_env=env)["reward"]
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
    r = judge(Path("tests"), Path("app"), "build", Path("out"), lang_env=env)["reward"]
    assert r["functional_pass_rate"] == 1.0 and r["reward"] == 1.0
