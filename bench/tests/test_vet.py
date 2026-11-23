import json
import os
import sys
from pathlib import Path

import pytest

from conftest import CANARY, edit, unseparated, write
from dodambench.task import load_task
import subprocess

from dodambench.vet import docker_create_cmd, judgement_problems, vet


def report(*cases: tuple[str, str | None, str], build_ok: bool = True, step: str = "build"):
    """The record of a judging, as the judging code takes it."""
    from dodambench.vet import Case, Judged
    return Judged(step=step, build_ok=build_ok, left_behind="", reward={},
                  cases=tuple(Case(name=f"test_{i}", outcome=o, file=f"{k}/test_{i}.py", kind=k, step=step,
                                   weakness=w, message="") for i, (k, w, o) in enumerate(cases)))


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
    results = {r["solution"]: r for r in vet(task, "python", tmp_path / "vet", **unseparated(vet))}
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


def test_image_judge_runs_the_verifier_judge_without_network():
    cmd = docker_create_cmd("dodam-vet-1", "dodam-bench/python:dev", "change-1",
                            {"memory_mb": 1024, "cpus": 2, "processes": 512})
    assert cmd[:4] == ["docker", "create", "--name", "dodam-vet-1"]
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--user") + 1] == "0"
    assert "DODAM_APP_USER=agent" in cmd
    script = " ".join(cmd[cmd.index("dodam-bench/python:dev") + 1:])
    assert "cp -R /solution/. /app/" in script
    # the judge is started by the verifier's own script, told which step to judge
    assert "DODAM_STEP=%s\\n' change-1 > /tests/step.env" in script and script.endswith("exec sh /tests/test.sh")


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


# --- the tool that checks prepared solutions, around the container it judges in ---

FAKE_DOCKER = r'''#!/bin/sh
# Stands in for Docker: a container that "judges" by leaving what the folder named by FAKE_LEAVES
# holds, and ends with the exit code FAKE_EXIT.
case "$1" in
  info) echo 4294967296 ;;
  create) shift; echo "$@" >> "$FAKE_LOG"; echo made ;;
  start) exit "${FAKE_EXIT:-0}" ;;
  cp)
    case "$2" in
      *:/logs/verifier/.) if [ -n "$FAKE_LEAVES" ]; then cp -R "$FAKE_LEAVES/." "$3"; fi ;;
    esac ;;
  rm) ;;
esac
exit 0
'''


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A Docker on PATH that runs no container; what the container leaves is set per test."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    leaves = tmp_path / "leaves"
    leaves.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LEAVES", str(leaves))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "docker.log"))
    return leaves


SAMPLE = Path(__file__).resolve().parents[1] / "tasks" / "cli-csv-json"


def judges_result(task, step: str) -> tuple[dict, dict]:
    """What the judge leaves after judging, at `step`, a solution that passes every test it runs there:
    reward.json and details.json as the judge writes them."""
    from dodambench.task import TEST_KINDS
    steps = [s.name for s in task.steps]
    run = [t for t in task.tests if steps.index(t.step) <= steps.index(step) and step not in t.superseded_in]
    cases = [{"name": f"test_{i}", "outcome": "passed", "message": "", "file": t.file, "kind": t.kind,
              "step": t.step, "weakness": t.weakness} for i, t in enumerate(run)]
    reward = {"build_ok": 1.0}
    for kind in TEST_KINDS:
        reward[f"{kind}_pass_rate"] = 1.0
        reward[f"{kind}_judged"] = float(sum(c["kind"] == kind for c in cases))
    reward.update({"unjudged": 0.0, "vulns_found": 0.0, "regression_broken": 0.0, "reward": 1.0, "separated": 1.0})
    return reward, {"step": step, "build_ok": True, "separated": True, "tests": cases}


def leave_result(leaves: Path, reward: float = 1.0) -> None:
    given, details = judges_result(load_task(SAMPLE), "build")
    (leaves / "reward.json").write_text(json.dumps({**given, "reward": reward}), encoding="utf-8")
    (leaves / "details.json").write_text(json.dumps(details), encoding="utf-8")


def judged(tmp_path: Path, out: Path):
    from dodambench.vet import judge_in_image
    tests, solution = tmp_path / "t", tmp_path / "s"
    tests.mkdir(exist_ok=True)
    solution.mkdir(exist_ok=True)
    return judge_in_image("dodam-bench/python:dev", tests, solution, out, "build",
                          {"memory_mb": 1024, "cpus": 2, "processes": 512}, 600, load_task(SAMPLE))


def test_an_earlier_runs_result_is_never_read_as_this_runs(tmp_path, fake_docker, monkeypatch):
    """The place for results is emptied before each judging: a container that ends without a result
    leaves the result of an earlier judging in that place unread, and is a fault."""
    from dodambench.judge import JudgeFault
    out = tmp_path / "out"
    out.mkdir()
    leave_result(out, 0.25)                       # what an earlier judging left there
    monkeypatch.setenv("FAKE_EXIT", "137")        # this container is ended and leaves nothing
    with pytest.raises(JudgeFault):
        judged(tmp_path, out)
    assert not (out / "reward.json").exists()


def test_an_earlier_runs_fault_is_never_read_as_this_runs(tmp_path, fake_docker, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "judge-fault.json").write_text(json.dumps({"judge_fault": "an earlier fault"}), encoding="utf-8")
    leave_result(fake_docker)
    monkeypatch.setenv("FAKE_EXIT", "0")
    assert judged(tmp_path, out).reward["reward"] == 1.0


def test_a_container_that_ended_without_a_result_is_a_fault_whatever_it_left(tmp_path, fake_docker, monkeypatch):
    """The container's exit status is read: a result file from a container that did not end well is
    no result."""
    from dodambench.judge import JudgeFault
    leave_result(fake_docker)
    monkeypatch.setenv("FAKE_EXIT", "137")
    with pytest.raises(JudgeFault) as fault:
        judged(tmp_path, tmp_path / "out")
    assert "137" in str(fault.value)


def test_a_fault_of_the_judge_in_the_image_is_read_as_one(tmp_path, fake_docker, monkeypatch, cli_task_dir):
    from dodambench.vet import vet_in_image
    (fake_docker / "judge-fault.json").write_text(json.dumps({"judge_fault": "made-up fault"}), encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "3")
    results = vet_in_image(load_task(cli_task_dir), "python", tmp_path / "out", "dodam-bench/python:dev")
    assert results and all(r["ok"] is False and r.get("judge_fault") is True for r in results), results
    assert all("the judge itself failed: made-up fault" in r["problems"][0] for r in results)


def test_the_container_gets_the_tasks_own_judging_time(tmp_path, cli_task_dir, monkeypatch):
    import dodambench.vet as vetting
    edit(cli_task_dir / "task.toml", "verifier_timeout_sec = 1800", "verifier_timeout_sec = 2400")
    given = []
    real = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:2] == ["docker", "start"]:
            given.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 3)
        if cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, "4294967296\n", "")
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr(vetting.subprocess, "run", run)
    vetting.vet_in_image(load_task(cli_task_dir), "python", tmp_path / "out", "dodam-bench/python:dev")
    assert given and set(given) == {2400}, given


def run_vet(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "dodambench.vet", *args], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1])


def test_the_tools_errors_are_json_first_and_no_trace(tmp_path, cli_task_dir):
    """A task that cannot be read, a language that does not exist and a language the task does not
    have: each is refused with a JSON diagnostic and exit code 2, never with a trace of an error."""
    write(tmp_path, "unreadable/task.toml", "schema = = 1\n")
    for args, key in (([str(tmp_path / "unreadable"), "--unseparated"], "task.toml"),
                      ([str(cli_task_dir), "--lang", "cobol", "--unseparated"], "lang"),
                      ([str(cli_task_dir), "--lang", "rust", "--unseparated"], "lang")):
        done = run_vet(*args, "--out", str(tmp_path / "vet"))
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert done.returncode == 2, (args, done.returncode, done.stdout[-500:])
        first = json.loads(done.stdout.splitlines()[0])
        assert first["severity"] == "error" and key in (first["file"], first["key"]) and first["message"], first


def test_a_step_without_a_secure_solution_is_reported(tmp_path, cli_task_dir):
    import shutil
    shutil.rmtree(cli_task_dir / "solutions/secure/python/change-1")
    done = run_vet(str(cli_task_dir), "--lang", "python", "--unseparated", "--out", str(tmp_path / "vet"))
    assert done.returncode == 2 and "Traceback" not in done.stderr
    assert "change-1" in done.stdout and "secure" in done.stdout, done.stdout


def test_a_run_that_checked_nothing_is_no_success_and_says_so(tmp_path, cli_task_dir, monkeypatch, capsys):
    import dodambench.vet as vetting
    monkeypatch.setattr(vetting, "_missing_tools", lambda *args, **kwargs: ["go"])
    code = vetting.main([str(cli_task_dir), "--lang", "python", "--unseparated", "--out", str(tmp_path / "vet")])
    said = capsys.readouterr().out
    assert code != 0 and "not checked" in said, (code, said)


def test_a_vulnerable_solution_fails_a_security_test_of_its_weakness_at_its_own_step():
    """At a change step the earlier step's tests run again. A vulnerable solution written for the
    change step that fails only the earlier step's test of its weakness is not what it was written for."""
    from dataclasses import replace
    from dodambench.vet import Case, Judged
    earlier = Case(kind="security", weakness="path-traversal", outcome="failed", file="security/test_a.py",
                   name="test_a", step="build", message="")
    own = Case(kind="security", weakness="path-traversal", outcome="passed", file="security/test_b.py",
               name="test_b", step="change-1", message="")
    of_step = Judged(step="change-1", build_ok=True, left_behind="", reward={}, cases=(earlier, own))
    problems = judgement_problems("vulnerable", "path-traversal", of_step)
    assert problems and "change-1" in problems[0], problems
    of_step = replace(of_step, cases=(earlier, replace(own, outcome="failed")))
    assert judgement_problems("vulnerable", "path-traversal", of_step) == []


def test_the_lowered_memory_cap_says_what_the_code_does():
    """A cap above three quarters of the Docker machine's memory is lowered to three quarters; the
    message says so and does not say that the machine cannot hold what it can."""
    from dodambench.vet import container_room
    task = load_task(Path(__file__).resolve().parents[1] / "tasks/web-signup")
    task.meta["limits"] = {**task.limits, "memory_mb": 1600}
    room = container_room(task, 1959)
    said = room["adjusted"]["message"]
    assert room["memory_mb"] == 1469 and "1600" in said and "1959" in said and "1469" in said, said
    assert "three quarters" in said, said


def test_a_container_that_left_only_part_of_a_result_is_a_fault(tmp_path, fake_docker, monkeypatch):
    from dodambench.judge import JudgeFault
    (fake_docker / "reward.json").write_text(json.dumps({"reward": 1.0}), encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "0")
    with pytest.raises(JudgeFault):
        judged(tmp_path, tmp_path / "out")


def test_a_run_that_left_some_solutions_unchecked_is_no_success_and_says_how_many(tmp_path, cli_task_dir, monkeypatch, capsys):
    import dodambench.vet as vetting
    monkeypatch.setattr(vetting, "_missing_tools", lambda lang_env, *args, **kwargs:
                        ["go"] if lang_env.get("BUILD_CMD", "").startswith("go ") else [])
    code = vetting.main([str(cli_task_dir), "--unseparated", "--out", str(tmp_path / "vet")])
    said = capsys.readouterr().out
    checked = [line for line in said.splitlines() if line.startswith("ok ")]
    assert checked and code != 0 and "not checked" in said and "nothing was checked" not in said, (code, said)


def test_a_step_that_judged_no_test_gets_no_judgement():
    """A step whose report holds no test judged nothing: no solution gets the judgement it was
    written for there."""
    empty = report()
    for expect, weakness in (("secure", None), ("vulnerable", "path-traversal"), ("broken", None)):
        problems = judgement_problems(expect, weakness, empty)
        assert problems and any("no test" in p for p in problems), (expect, problems)


@pytest.mark.parametrize("said", ["[]", "null", "\"fault\"", "{"])
def test_a_fault_file_of_any_shape_is_read_as_a_fault(tmp_path, fake_docker, monkeypatch, said):
    from dodambench.judge import JudgeFault
    (fake_docker / "judge-fault.json").write_text(said, encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "3")
    with pytest.raises(JudgeFault):
        judged(tmp_path, tmp_path / "out")



@pytest.mark.parametrize("reward, details", [
    ("[]", '{"build_ok": true, "step": "build", "tests": []}'),
    ('{"reward": "high"}', '{"build_ok": true, "step": "build", "tests": []}'),
    ('{"reward": 1.0}', '{"build_ok": true, "step": "build", "tests": "none"}'),
    ('{"reward": 1.0}', '{"build_ok": true, "step": "build", "tests": [{"name": "test_a"}]}'),
    ('{"reward": 1.0}', '[]'),
])
def test_a_result_of_another_shape_is_a_fault(tmp_path, fake_docker, monkeypatch, reward, details):
    """Every file the tool reads is checked for its shape before it is used."""
    from dodambench.judge import JudgeFault
    (fake_docker / "reward.json").write_text(reward, encoding="utf-8")
    (fake_docker / "details.json").write_text(details, encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "0")
    with pytest.raises(JudgeFault):
        judged(tmp_path, tmp_path / "out")


# --- what the tool reads from a judging: one record, made in one place ---

MISSING = "<missing>"
# Every part of a judging's files the judging code reads, with values that are not of it: missing, of
# another kind, outside its set of values, or naming another step than the one judged or than its
# file's. The demo task is judged at change-1, where every one of its four files of tests runs; the
# first case is of the functional file of the build step.
NOT_OF_IT = [
    ("details", None, []), ("details", None, "details"),
    ("details", "step", MISSING), ("details", "step", 1), ("details", "step", "build"), ("details", "step", "change-9"),
    ("details", "build_ok", MISSING), ("details", "build_ok", "false"), ("details", "build_ok", 1),
    ("details", "build_ok", None),
    ("details", "left_behind", 1), ("details", "left_behind", None), ("details", "left_behind", ""),
    ("details", "tests", MISSING), ("details", "tests", {}), ("details", "tests", "none"), ("details", "tests", []),
    ("left out", "security", None),
    ("case", "name", MISSING), ("case", "name", 1), ("case", "name", ""),
    ("case", "outcome", MISSING), ("case", "outcome", 1), ("case", "outcome", "error"), ("case", "outcome", "PASSED"),
    ("case", "file", MISSING), ("case", "file", 1), ("case", "file", "functional/test_other.py"),
    ("case", "file", "boundary/test_usage.py"),
    ("case", "kind", MISSING), ("case", "kind", 1), ("case", "kind", "unit"), ("case", "kind", "boundary"),
    ("case", "step", MISSING), ("case", "step", 1), ("case", "step", "change-1"), ("case", "step", "change-9"),
    ("case", "weakness", MISSING), ("case", "weakness", 1), ("case", "weakness", "path-traversal"),
    ("security case", "weakness", None), ("security case", "weakness", "made-up"), ("security case", "weakness", ""),
    ("case", "message", MISSING), ("case", "message", 1), ("case", "message", None),
    ("reward", None, []), ("reward", "reward", MISSING), ("reward", "reward", "1"), ("reward", "reward", True),
    ("reward", "reward", float("nan")), ("reward", "reward", float("inf")), ("reward", "reward", 0.5),
    ("reward", "unjudged", MISSING), ("reward", "unjudged", "0"), ("reward", "bonus", 1.0),
]


def not_of_it(reward: dict, details: dict, where: str, part, value) -> tuple:
    """The judge's result with one part made not of it."""
    import copy
    reward, details = copy.deepcopy(reward), copy.deepcopy(details)
    if where == "left out":                     # the cases of one file the step runs are not there
        details["tests"] = [c for c in details["tests"] if c["kind"] != part]
        return reward, details
    if part is None:
        return (value, details) if where == "reward" else (reward, value)
    target = {"reward": reward, "details": details, "case": details["tests"][0],
              "security case": next((c for c in details["tests"] if c["kind"] == "security"), {})}[where]
    if value == MISSING:
        target.pop(part, None)
    else:
        target[part] = value
    return reward, details


def vetted_with(tmp_path, monkeypatch, fake_docker, task, path: str, reward, details) -> list[dict]:
    """The tool's results when every judging of the task leaves these files: in the image, where the
    container leaves them, or without separation, where the judge on this machine leaves them."""
    import dodambench.vet as vetting
    if path == "image":
        (fake_docker / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (fake_docker / "details.json").write_text(json.dumps(details), encoding="utf-8")
        monkeypatch.setenv("FAKE_EXIT", "0")
        return vetting.vet_in_image(task, "python", tmp_path / "out", "dodam-bench/python:dev")

    def judge(tests_dir, app_dir, step, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (out_dir / "details.json").write_text(json.dumps(details), encoding="utf-8")
        return {"reward": reward, "report": details}
    monkeypatch.setattr(vetting, "judge", judge)
    monkeypatch.setattr(vetting, "_missing_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(vetting, "scaffold_cache", lambda *args, **kwargs: None)
    return vetting.vet(task, "python", tmp_path / "out", unseparated=True)


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_the_judges_own_result_gives_the_secure_solution_its_judgement(tmp_path, monkeypatch, fake_docker,
                                                                         cli_task_dir, path):
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    secure = [r for r in results if r["step"] == "change-1" and r["solution"] == "secure"]
    assert secure and secure[0]["ok"] and secure[0]["reward"] == reward, results


@pytest.mark.parametrize("path", ["image", "unseparated"])
@pytest.mark.parametrize("where, part, value", NOT_OF_IT,
                         ids=[f"{w}-{p}-{'missing' if v == MISSING else repr(v)}" for w, p, v in NOT_OF_IT])
def test_a_result_with_a_part_not_of_it_is_a_fault_and_never_a_success(tmp_path, monkeypatch, fake_docker,
                                                                        cli_task_dir, path, where, part, value):
    """The tool turns what a judging left into a record in one place; a result that cannot be turned
    into it is a fault of the judge, for every solution, and never a success."""
    task = load_task(cli_task_dir)
    reward, details = not_of_it(*judges_result(task, "change-1"), where, part, value)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    assert results and all(r["ok"] is False and r.get("judge_fault") is True for r in results), results


def test_every_part_the_judging_code_reads_is_tried():
    """The record holds exactly the parts the judging code reads, and each is tried above."""
    from dataclasses import fields
    import dodambench.vet as vetting
    tried = {(w, p) for w, p, _ in NOT_OF_IT}
    assert {f.name for f in fields(vetting.Judged)} == {"step", "build_ok", "left_behind", "cases", "reward"}
    assert {("details", p) for p in ("step", "build_ok", "left_behind", "tests")} <= tried
    assert {("case", f.name) for f in fields(vetting.Case)} <= tried
    assert set(vetting.REWARD_KEYS) == set(judges_result(load_task(SAMPLE), "build")[0])


def test_the_judging_code_takes_nothing_but_the_record():
    import dodambench.vet as vetting
    with pytest.raises(TypeError):
        vetting.judgement_problems("secure", None, {"build_ok": True, "step": "build", "tests": []})


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_the_judging_code_is_handed_the_record_on_every_path(tmp_path, monkeypatch, fake_docker, cli_task_dir, path):
    import dodambench.vet as vetting
    handed, real = [], vetting.judgement_problems

    def spy(expect, weakness, judged):
        handed.append(judged)
        return real(expect, weakness, judged)
    monkeypatch.setattr(vetting, "judgement_problems", spy)
    task = load_task(cli_task_dir)
    vetted_with(tmp_path, monkeypatch, fake_docker, task, path, *judges_result(task, "change-1"))
    assert handed and all(type(j) is vetting.Judged for j in handed), handed
