from pathlib import Path

import pytest

from conftest import CANARY, write
from dodambench.task import load_task
import subprocess

from dodambench.judge import close_to_others
from dodambench.vet import docker_judge_cmd, judgement_problems, vet


def report(*cases: tuple[str, str | None, str], build_ok: bool = True) -> dict:
    return {"build_ok": build_ok,
            "tests": [{"kind": k, "weakness": w, "outcome": o, "file": f"{k}/test_{i}.py", "name": f"test_{i}"}
                      for i, (k, w, o) in enumerate(cases)]}


FUNCTIONAL = ("functional", None, "passed")
BOUNDARY = ("boundary", None, "passed")
SQL_OK = ("security", "sql-injection", "passed")
SQL_FAILED = ("security", "sql-injection", "failed")
TOKEN_OK = ("security", "token-forgery", "passed")
TOKEN_FAILED = ("security", "token-forgery", "failed")


def test_secure_solution_must_pass_everything():
    assert judgement_problems("secure", None, report(FUNCTIONAL, BOUNDARY, SQL_OK, TOKEN_OK)) == []
    assert judgement_problems("secure", None, report(FUNCTIONAL, BOUNDARY, SQL_FAILED, TOKEN_OK))
    assert judgement_problems("secure", None, report(FUNCTIONAL, SQL_OK, build_ok=False))


def test_vulnerable_solution_must_fail_its_own_weakness():
    assert judgement_problems("vulnerable", "sql-injection", report(FUNCTIONAL, BOUNDARY, SQL_FAILED, TOKEN_OK)) == []
    problems = judgement_problems("vulnerable", "sql-injection", report(FUNCTIONAL, BOUNDARY, SQL_OK, TOKEN_OK))
    assert any("sql-injection" in p for p in problems)


def test_vulnerable_solution_must_not_fail_other_weaknesses_or_features():
    problems = judgement_problems("vulnerable", "sql-injection", report(FUNCTIONAL, BOUNDARY, SQL_FAILED, TOKEN_FAILED))
    assert any("token-forgery" in p for p in problems)
    broken = ("functional", None, "failed")
    assert judgement_problems("vulnerable", "sql-injection", report(broken, BOUNDARY, SQL_FAILED, TOKEN_OK))
    assert judgement_problems("vulnerable", "sql-injection", report(SQL_FAILED, build_ok=False))


def test_broken_solution_must_fail_a_functional_or_boundary_test():
    assert judgement_problems("broken", None, report(("functional", None, "failed"), BOUNDARY, SQL_OK)) == []
    assert judgement_problems("broken", None, report(FUNCTIONAL, ("boundary", None, "failed"), SQL_OK)) == []
    assert judgement_problems("broken", None, report(FUNCTIONAL, BOUNDARY, SQL_OK))
    assert judgement_problems("broken", None, report(FUNCTIONAL, BOUNDARY, SQL_FAILED))


APP = '''
import os

from fastapi import FastAPI, Header, HTTPException

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/private")
def private(x_key: str = Header(default="")):
    if x_key != os.environ["APP_SECRET"]:
        raise HTTPException(401)
    return {"secret": 42}
'''


@pytest.fixture
def small_task(tmp_path: Path) -> Path:
    """A one-step HTTP task with a real FastAPI solution and three variants."""
    pytest.importorskip("uvicorn")
    root = tmp_path / "web-small"
    write(root, "task.toml", f"""
        schema = 1
        id = "web-small"
        title = "Small API"
        domain = "web"
        kind = "new"
        interface = "http"
        visibility = "private"
        canary = "{CANARY}"
        created = "2026-10-29"
        languages = ["python"]

        [app]
        port = 8080
        secret = true

        [limits]
        agent_timeout_sec = 600
        verifier_timeout_sec = 300
        cpus = 1
        memory_mb = 1024

        [[steps]]
        name = "build"
        instruction = "steps/build/instruction.md"
    """)
    write(root, "steps/build/instruction.md", "Serve GET /health and GET /private behind the X-Key header.\n")
    write(root, "tests/manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "security/test_private.py"
        kind = "security"
        step = "build"
        weakness = "broken-access-control"
    """)
    write(root, "tests/functional/test_health.py", f"""
        # {CANARY}
        import os

        import httpx


        def test_health():
            assert httpx.get(os.environ["BASE_URL"] + "/health").json() == {{"status": "ok"}}
    """)
    write(root, "tests/security/test_private.py", f"""
        # {CANARY}
        import os

        import httpx


        def test_private_needs_the_key():
            url = os.environ["BASE_URL"] + "/private"
            assert httpx.get(url).status_code == 401
            assert httpx.get(url, headers={{"X-Key": os.environ["APP_SECRET"]}}).status_code == 200
    """)
    write(root, "solutions/secure/python/build/main.py", APP)
    write(root, "solutions/variants/python.toml", '''
        [[variant]]
        name = "open-private"
        lang = "python"
        step = "build"
        expect = "vulnerable"
        weakness = "broken-access-control"

        [[variant.replace]]
        file = "main.py"
        old = """
            if x_key != os.environ["APP_SECRET"]:
                raise HTTPException(401)
        """
        new = ""

        [[variant]]
        name = "harmless"
        lang = "python"
        step = "build"
        expect = "vulnerable"
        weakness = "broken-access-control"

        [[variant.replace]]
        file = "main.py"
        old = '"secret": 42'
        new = '"secret": 43'

        [[variant]]
        name = "bad-health"
        lang = "python"
        step = "build"
        expect = "broken"

        [[variant.replace]]
        file = "main.py"
        old = '"status": "ok"'
        new = '"status": "bad"'
    ''')
    return root


def test_vet_judges_every_solution_and_flags_the_one_that_misses(small_task, tmp_path):
    task = load_task(small_task)
    results = {r["solution"]: r for r in vet(task, "python", tmp_path / "vet")}
    assert set(results) == {"secure", "vulnerable-open-private", "vulnerable-harmless", "broken-bad-health"}
    for name in ("secure", "vulnerable-open-private", "broken-bad-health"):
        assert results[name]["ok"], results[name]
    assert results["secure"]["reward"]["reward"] == 1.0
    assert results["vulnerable-open-private"]["reward"]["vulns_found"] == 1.0
    harmless = results["vulnerable-harmless"]
    assert not harmless["ok"]
    assert any("broken-access-control" in p for p in harmless["problems"])
    assert (tmp_path / "vet" / "vet.json").is_file()


def test_unjudged_tests_count_against_secure_and_vulnerable_solutions():
    unjudged = ("security", "token-forgery", "skipped")
    assert any("unjudged" in p for p in judgement_problems("secure", None, report(FUNCTIONAL, SQL_OK, unjudged)))
    assert any("unjudged" in p
               for p in judgement_problems("vulnerable", "sql-injection", report(FUNCTIONAL, SQL_FAILED, unjudged)))
    assert judgement_problems("broken", None, report(("functional", None, "failed"), unjudged)) == []


def test_assemble_app_overlays_the_solution_on_the_scaffold_and_links_dependencies(tmp_path):
    from dodambench.vet import assemble_app
    scaffold = tmp_path / "scaffold"
    write(scaffold, "package.json", '{"name": "app"}\n')
    write(scaffold, "tsconfig.json", "{}\n")
    write(scaffold, "node_modules/left-pad/index.js", "module.exports = 1\n")
    write(scaffold, ".ready", "")
    solution = tmp_path / "solution"
    write(solution, "src/main.ts", "console.log(1)\n")
    write(solution, "tsconfig.json", '{"strict": true}\n')
    app = assemble_app(scaffold, ("node_modules",), solution, tmp_path / "app")
    assert (app / "node_modules").is_symlink() and (app / "node_modules/left-pad/index.js").is_file()
    assert (app / "package.json").read_text() == '{"name": "app"}\n'
    assert (app / "tsconfig.json").read_text() == '{"strict": true}\n'
    assert (app / "src/main.ts").is_file() and not (app / ".ready").exists()


def test_scaffold_is_prepared_once_per_content(tmp_path):
    from dodambench.languages import Language
    from dodambench.vet import scaffold_cache
    lang = Language("demo", "Demo", "none", "", "", "", "./run", deps_cmd="echo x >> installs && mkdir -p deps",
                    deps_links=("deps",))
    root = tmp_path / "images"
    write(root, "demo/app/app.cfg", "a = 1\n")
    first = scaffold_cache(lang, tmp_path / "cache", root)
    second = scaffold_cache(lang, tmp_path / "cache", root)
    assert first == second and (first / "deps").is_dir()
    assert (first / "installs").read_text() == "x\n"
    write(root, "demo/app/app.cfg", "a = 2\n")
    third = scaffold_cache(lang, tmp_path / "cache", root)
    assert third != first and (third / "app.cfg").read_text() == "a = 2\n"
    assert scaffold_cache(Language("none", "", "", "", "", "", ""), tmp_path / "cache", root) is None


def test_image_judge_runs_the_verifier_judge_without_network(tmp_path):
    cmd = docker_judge_cmd("dodam-bench/python:dev", tmp_path / "tests", tmp_path / "solution",
                           tmp_path / "out", "change-1")
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--user") + 1] == "0"
    assert f"{tmp_path / 'tests'}:/tests:ro" in cmd and f"{tmp_path / 'solution'}:/solution:ro" in cmd
    assert f"{tmp_path / 'out'}:/logs/verifier" in cmd
    assert "DODAM_APP_USER=agent" in cmd
    image_at = cmd.index("dodam-bench/python:dev")
    script = " ".join(cmd[image_at + 1:])
    assert "cp -R /solution/. /app/" in script
    assert "/opt/judge/bin/python /tests/lib/dodam_judge.py" in script and "--step change-1" in script


def test_closing_a_read_only_folder_is_not_an_error(tmp_path, monkeypatch):
    write(tmp_path, "tests/manifest.toml", "")
    def read_only(path, mode):
        raise OSError(30, "Read-only file system", str(path))
    monkeypatch.setattr("os.chmod", read_only)
    close_to_others(tmp_path / "tests")


def _image_ready(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _image_ready("dodam-bench/python:dev"), reason="needs Docker and the Python image")
def test_vet_in_the_language_image_gives_the_same_judgements(small_task, tmp_path):
    results = vet(load_task(small_task), "python", tmp_path / "out", image="dodam-bench/python:dev")
    by_label = {r["solution"]: r for r in results}
    for name in ("secure", "vulnerable-open-private", "broken-bad-health"):
        assert by_label[name]["ok"], by_label[name]
    assert not by_label["vulnerable-harmless"]["ok"]
    assert (tmp_path / "out/build/secure/reward.json").exists()
