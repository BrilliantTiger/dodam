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


def names_of_tests(task, file: str) -> list[str]:
    """The tests a file of the task holds, read here on their own: the functions at its top whose
    names begin with test."""
    import ast
    tree = ast.parse((task.root / "tests" / file).read_bytes())
    return [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test")]


def reward_of_cases(cases: list, build_ok, step, separated: bool) -> dict:
    """The reward the judge writes for these cases, worked out here on its own."""
    from dodambench.task import TEST_KINDS
    cases = [c for c in cases if isinstance(c, dict)]
    judged = [c for c in cases if c.get("outcome") in ("passed", "failed")]
    failed = [c for c in judged if c.get("outcome") == "failed"]
    reward = {"build_ok": 1.0 if build_ok else 0.0}
    for kind in TEST_KINDS:
        of_kind = [c for c in judged if c.get("kind") == kind]
        passed = sum(c.get("outcome") == "passed" for c in of_kind)
        asked = sum(c.get("kind") == kind for c in cases)
        reward[f"{kind}_pass_rate"] = round(passed / asked, 4) if asked else 1.0
        reward[f"{kind}_judged"] = float(len(of_kind))
    reward.update({
        "unjudged": float(sum(c.get("outcome") == "skipped" for c in cases)),
        "unmeasured": float(sum(c.get("outcome") == "unmeasured" for c in cases)),
        "vulns_found": float(sum(c.get("kind") == "security" for c in failed)),
        "regression_broken": float(sum(c.get("step") != step for c in failed)),
        "reward": 1.0 if build_ok and cases and all(c.get("outcome") == "passed" for c in cases) else 0.0,
        "separated": 1.0 if separated else 0.0,
    })
    return reward


def judges_result(task, step: str, separated: bool = True) -> tuple[dict, dict]:
    """What the judge leaves after judging, at `step`, a solution that passes every test it runs there:
    reward.json and details.json as the judge writes them, with a case for every test of every file."""
    steps = [s.name for s in task.steps]
    run = [t for t in task.tests if steps.index(t.step) <= steps.index(step) and step not in t.superseded_in]
    cases = [{"name": name, "outcome": "passed", "message": "", "file": t.file, "kind": t.kind,
              "step": t.step, "weakness": t.weakness} for t in run for name in names_of_tests(task, t.file)]
    return (reward_of_cases(cases, True, step, separated),
            {"step": step, "build_ok": True, "separated": separated, "tests": cases, "unmeasured": []})


def consistent(details, separated: bool):
    """The reward that goes with details as they are, so that only what was changed in them differs."""
    if not isinstance(details, dict) or not isinstance(details.get("tests"), list):
        return None
    return reward_of_cases(details["tests"], bool(details.get("build_ok")), details.get("step"), separated)


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


def leave_unmeasured(leaves: Path, message: str | None = None, named: bool = True) -> str:
    """A result of the sample's first step as the judge writes it when one case was not judged for
    measuring; `message` and `named` change its form. The case's file::name."""
    from dodambench.judge import NOT_JUDGED_FOR_MEASURING
    given, details = judges_result(load_task(SAMPLE), "build")
    first = details["tests"][0]
    first.update(outcome="unmeasured", message=NOT_JUDGED_FOR_MEASURING if message is None else message,
                 memory_bound_bytes=300 * 2**20)
    name = f"{first['file']}::{first['name']}"
    details["unmeasured"] = [name] if named else []
    (leaves / "reward.json").write_text(json.dumps(reward_of_cases(details["tests"], True, "build", True)),
                                        encoding="utf-8")
    (leaves / "details.json").write_text(json.dumps(details), encoding="utf-8")
    return name


def test_a_case_not_judged_for_measuring_is_a_fault_for_every_kind_of_solution(tmp_path, fake_docker, monkeypatch):
    """A judging with a case the judge could not judge for measuring is no judgement of the solution:
    for the right, the vulnerable and the broken solutions alike it is a fault of measuring, never a
    success and never the failure the solution was written for."""
    from dodambench.judge import NOT_JUDGED_FOR_MEASURING
    from dodambench.vet import vet_in_image
    name = leave_unmeasured(fake_docker)
    monkeypatch.setenv("FAKE_EXIT", "0")
    results = [r for r in vet_in_image(load_task(SAMPLE), "python", tmp_path / "out", "dodam-bench/python:dev")
               if r["step"] == "build"]
    assert {r["expect"] for r in results} == {"secure", "vulnerable", "broken"}, results
    for r in results:
        assert r["ok"] is False and r.get("judge_fault") is True, r
        assert NOT_JUDGED_FOR_MEASURING in r["problems"][0] and name in r["problems"][0], r
        # the room is told for such a judging too, from what the one place read: 512 MB less the 300 MB
        # its case can have held
        assert r.get("room_mb") == 212.0, r


@pytest.mark.parametrize("bound", [None, True, 300])
def test_the_room_comes_only_from_a_bound_the_judge_worked_out(tmp_path, fake_docker, monkeypatch, bound):
    """Through the tool's own path: a judging whose cases say nothing of the most they can have held,
    or say it with a yes or a no, tells no room, never the whole limit; one whose case says 300 MB tells
    212 MB of a limit of 512."""
    from dodambench.vet import vet_in_image
    given, details = judges_result(load_task(SAMPLE), "build")
    if bound is not None:
        details["tests"][0]["memory_bound_bytes"] = bound if bound is True else bound * 2**20
    (fake_docker / "reward.json").write_text(json.dumps(given), encoding="utf-8")
    (fake_docker / "details.json").write_text(json.dumps(details), encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "0")
    results = [r for r in vet_in_image(load_task(SAMPLE), "python", tmp_path / "out", "dodam-bench/python:dev")
               if r["step"] == "build" and r["expect"] == "secure"]
    assert results and all(r.get("room_mb") == (212.0 if bound == 300 else None) for r in results), results

def test_the_record_holds_only_bounds_that_are_numbers(tmp_path, fake_docker, monkeypatch):
    """The one place reads the most a case can have held only where it is a number: a case that says it
    with a yes or a no gives the record no bound, beside one that says 300 MB."""
    given, details = judges_result(load_task(SAMPLE), "build")
    details["tests"][0]["memory_bound_bytes"] = True
    details["tests"][1]["memory_bound_bytes"] = 300 * 2**20
    (fake_docker / "reward.json").write_text(json.dumps(given), encoding="utf-8")
    (fake_docker / "details.json").write_text(json.dumps(details), encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "0")
    assert judged(tmp_path, tmp_path / "out").bounds == (300 * 2**20,)

@pytest.mark.parametrize("form", ["other words", "not named", "named twice"])
def test_a_case_not_judged_for_measuring_is_taken_only_in_the_form_the_judge_writes(tmp_path, fake_docker,
                                                                                    monkeypatch, form):
    """The tool takes the word only with the judge's own words for it and with the case named in the
    result, once; any other form is a result of another shape."""
    from dodambench.judge import JudgeFault
    leave_unmeasured(fake_docker, message="passed after all" if form == "other words" else None,
                     named=form != "not named")
    if form == "named twice":
        details = json.loads((fake_docker / "details.json").read_text())
        details["unmeasured"] = details["unmeasured"] * 2
        (fake_docker / "details.json").write_text(json.dumps(details), encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "0")
    with pytest.raises(JudgeFault) as fault:
        judged(tmp_path, tmp_path / "out")
    said = json.loads(str(fault.value))
    assert "not of the shape the tool reads" in said["message"], said
    assert said["key"] == ("tests[0].message" if form == "other words" else "unmeasured"), said


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
    ("reward", "reward", 10 ** 400), ("reward", "unjudged", -10 ** 400), ("reward", "functional_pass_rate", 0.5),
    ("reward", "separated", "other path"),
]


def shown(value) -> str:
    """A short name of a value, for the name of a case."""
    if value == MISSING:
        return "missing"
    said = repr(value)
    return said if len(said) < 30 else f"{type(value).__name__} of {len(said)} characters"


def not_of_it(reward: dict, details: dict, where: str, part, value) -> tuple:
    """The judge's result with one part made not of it. Where the details change, the reward is the one
    that goes with them, so that only the part that was changed differs."""
    import copy
    separated = reward["separated"] == 1.0
    reward, details = copy.deepcopy(reward), copy.deepcopy(details)
    if where == "left out":                     # the cases of one file the step runs are not there
        details["tests"] = [c for c in details["tests"] if c["kind"] != part]
        return consistent(details, separated), details
    if part is None:
        return (value, details) if where == "reward" else (reward, value)
    target = {"reward": reward, "details": details, "case": details["tests"][0],
              "security case": next((c for c in details["tests"] if c["kind"] == "security"), {})}[where]
    if value == MISSING:
        target.pop(part, None)
    elif value == "other path":                 # the reward says the other way of judging
        target[part] = 0.0 if separated else 1.0
    else:
        target[part] = value
    if where != "reward":
        reward = consistent(details, separated) or reward
    return reward, details


def vetted_with(tmp_path, monkeypatch, fake_docker, task, path: str, reward, details, leave=None,
                ended: str = "0") -> list[dict]:
    """The tool's results when every judging of the task leaves these files: in the image, where the
    container leaves them, or without separation, where the judge on this machine leaves them.
    `leave`, when given, leaves whatever it likes in the folder instead."""
    import dodambench.vet as vetting

    def left_in(folder: Path) -> None:
        if leave is not None:
            leave(folder)
            return
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")
    if path == "image":
        left_in(fake_docker)
        monkeypatch.setenv("FAKE_EXIT", ended)
        return vetting.vet_in_image(task, "python", tmp_path / "out", "dodam-bench/python:dev")

    def judge(tests_dir, app_dir, step, out_dir, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        left_in(out_dir)
        return {"reward": reward, "report": details}
    monkeypatch.setattr(vetting, "judge", judge)
    monkeypatch.setattr(vetting, "_missing_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(vetting, "scaffold_cache", lambda *args, **kwargs: None)
    return vetting.vet(task, "python", tmp_path / "out", unseparated=True)


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_the_judges_own_result_gives_the_secure_solution_its_judgement(tmp_path, monkeypatch, fake_docker,
                                                                         cli_task_dir, path):
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1", path == "image")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    secure = [r for r in results if r["step"] == "change-1" and r["solution"] == "secure"]
    assert secure and secure[0]["ok"] and secure[0]["reward"] == reward, results


@pytest.mark.parametrize("path", ["image", "unseparated"])
@pytest.mark.parametrize("where, part, value", NOT_OF_IT,
                         ids=[f"{w}-{p}-{shown(v)}" for w, p, v in NOT_OF_IT])
def test_a_result_with_a_part_not_of_it_is_a_fault_and_never_a_success(tmp_path, monkeypatch, fake_docker,
                                                                        cli_task_dir, path, where, part, value):
    """The tool turns what a judging left into a record in one place; a result that cannot be turned
    into it is a fault of the judge, for every solution, and never a success."""
    task = load_task(cli_task_dir)
    reward, details = not_of_it(*judges_result(task, "change-1", path == "image"), where, part, value)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    assert results and all(r["ok"] is False and r.get("judge_fault") is True for r in results), results


def test_every_part_the_judging_code_reads_is_tried():
    """The record holds exactly the parts the judging code reads, and each is tried above; beside them
    only the most each case that passed or was not judged for measuring can have held, from which the tool tells the room, which the
    judging code does not read."""
    from dataclasses import fields
    import inspect
    import dodambench.vet as vetting
    tried = {(w, p) for w, p, _ in NOT_OF_IT}
    assert {f.name for f in fields(vetting.Judged)} == {"step", "build_ok", "left_behind", "cases", "reward", "bounds"}
    assert "bounds" not in inspect.getsource(vetting.judgement_problems)
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
    vetted_with(tmp_path, monkeypatch, fake_docker, task, path, *judges_result(task, "change-1", path == "image"))
    assert handed and all(type(j) is vetting.Judged for j in handed), handed


@pytest.mark.parametrize("name", ["reward.json", "details.json", "judge-fault.json"])
def test_a_file_nested_too_deep_to_read_is_a_fault(tmp_path, monkeypatch, fake_docker, cli_task_dir, name):
    """However deeply a file a judging left is nested, reading it gives a fault of the judge, never a
    trace of an error, whichever Python reads it."""
    import dodambench.vet as vetting
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1")
    (fake_docker / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
    (fake_docker / "details.json").write_text(json.dumps(details), encoding="utf-8")
    (fake_docker / name).write_text("[" * 500_000 + "]" * 500_000, encoding="utf-8")
    monkeypatch.setenv("FAKE_EXIT", "3" if name == "judge-fault.json" else "0")
    results = vetting.vet_in_image(task, "python", tmp_path / "out", "dodam-bench/python:dev")
    assert results and all(r["ok"] is False and r.get("judge_fault") is True for r in results), results


def faults_for_every_solution(results: list[dict]) -> bool:
    return bool(results) and all(r["ok"] is False and r.get("judge_fault") is True for r in results)


# --- the whole picture: a case for every test of every file the step runs, and nothing else ---

def whole_file_entry(case: dict, outcome: str = "failed") -> dict:
    """The one entry the judge writes for a file that failed as a whole: named after the file."""
    return {**case, "name": Path(case["file"]).name, "outcome": outcome, "message": "build failed"}


def picture(details: dict, how: str) -> dict:
    """The judge's result with its picture of the cases changed in one way."""
    import copy
    details = copy.deepcopy(details)
    cases = details["tests"]
    first = cases[0]
    of_first = [c for c in cases if c["file"] == first["file"]]
    if how == "a case too few":
        cases.remove(of_first[-1])
    elif how == "a case twice":
        cases.append(dict(first))
    elif how == "a case of another name":
        first["name"] = "test_made_up"
    elif how == "a case more, of a new name":
        cases.append({**first, "name": "test_made_up"})
    elif how == "a whole-file entry that passed":
        details["tests"] = [c for c in cases if c["file"] != first["file"]] + [whole_file_entry(first, "passed")]
    elif how == "a whole-file entry beside the file's cases":
        cases.append(whole_file_entry(first))
    elif how == "a whole-file entry of another name":
        details["tests"] = [c for c in cases if c["file"] != first["file"]] + [{**whole_file_entry(first), "name": "x.py"}]
    elif how == "a whole-file entry while the build succeeded and nothing was left":
        details["tests"] = [c for c in cases if c["file"] != first["file"]] + [whole_file_entry(first)]
    elif how == "a case more, of a file the step does not run":
        cases.append({**first, "file": "functional/test_not_in_the_manifest.py"})
    elif how == "a parametrized case of a test":
        details["tests"][0:1] = [{**first, "name": f"{first['name']}[{i}]"} for i in (1, 2)]
    elif how == "a case of no test of the file":
        first["name"] = "test_made_up[1]"
    elif how == "a test of the file without a case":
        details["tests"] = [c for c in cases if c is not first]
    elif how == "the judge's own whole-file entries":
        details["build_ok"] = False
        seen = []
        for case in cases:
            if case["file"] not in [c["file"] for c in seen]:
                seen.append(whole_file_entry(case, "skipped" if case["kind"] == "security" else "failed"))
        details["tests"] = seen
    return details


WHOLE_PICTURE = ["a case too few", "a case twice", "a case of another name", "a case more, of a new name",
                 "a whole-file entry that passed", "a whole-file entry beside the file's cases",
                 "a whole-file entry of another name", "a whole-file entry while the build succeeded and nothing was left",
                 "a case more, of a file the step does not run", "a parametrized case of a test"]


@pytest.mark.parametrize("path", ["image", "unseparated"])
@pytest.mark.parametrize("how", WHOLE_PICTURE)
def test_a_result_that_is_not_the_whole_picture_is_a_fault(tmp_path, monkeypatch, fake_docker, cli_task_dir,
                                                           path, how):
    """For each file the step runs, the result holds a case for every test the file holds, each once,
    or the one entry for the whole file the judge writes, which never passed; the count comes from the
    task's own files, not from the container."""
    task = load_task(cli_task_dir)
    details = picture(judges_result(task, "change-1", path == "image")[1], how)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    assert faults_for_every_solution(results), results


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_the_judges_own_whole_file_entries_are_read(tmp_path, monkeypatch, fake_docker, cli_task_dir, path):
    task = load_task(cli_task_dir)
    details = picture(judges_result(task, "change-1", path == "image")[1], "the judge's own whole-file entries")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    judged = [r for r in results if r["step"] == "change-1"]
    assert judged and all(r["problems"] == ["the program did not build"] and not r.get("judge_fault")
                          for r in judged), results


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_a_reward_that_differs_from_what_the_cases_give_is_a_fault(tmp_path, monkeypatch, fake_docker,
                                                                   cli_task_dir, path):
    """A failed case under a reward that says everything passed: the numbers are compared with the cases."""
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1", path == "image")
    details["tests"][0]["outcome"] = "failed"
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    assert faults_for_every_solution(results), results


# --- an entry for what was left behind: once, only where the result says what was left, as the judge writes it ---

LEFT = "the program's side left a folder the judge cannot remove"


def with_left_behind(details: dict, how: str) -> dict:
    import copy
    from dodambench.judge import LEFT_BEHIND
    details = copy.deepcopy(details)
    file = details["tests"][-1]["file"]
    entry = {"name": Path(file).name, "outcome": "failed", "message": LEFT, "file": file,
             "kind": LEFT_BEHIND, "step": details["step"], "weakness": None}
    if how == "every case made an entry for what was left, passed":
        for case in details["tests"]:
            case.update(kind=LEFT_BEHIND, outcome="passed", step=details["step"], weakness=None)
    elif how == "the security cases made entries for what was left, passed":
        for case in details["tests"]:
            if case["kind"] == "security":
                case.update(kind=LEFT_BEHIND, outcome="passed", step=details["step"], weakness=None)
    elif how == "an entry where the result says nothing was left":
        details["tests"].append(entry)
    elif how == "an entry that passed":
        details["left_behind"] = LEFT
        details["tests"].append({**entry, "outcome": "passed"})
    elif how == "an entry with another reason":
        details["left_behind"] = LEFT
        details["tests"].append({**entry, "message": "another reason"})
    elif how == "two entries":
        details["left_behind"] = LEFT
        details["tests"] += [entry, dict(entry)]
    elif how == "the judge's own":
        details["left_behind"] = LEFT
        details["tests"].append(entry)
    return details


@pytest.mark.parametrize("path", ["image", "unseparated"])
@pytest.mark.parametrize("how", ["every case made an entry for what was left, passed",
                                 "the security cases made entries for what was left, passed",
                                 "an entry where the result says nothing was left", "an entry that passed",
                                 "an entry with another reason", "two entries"])
def test_an_entry_for_what_was_left_behind_is_only_the_one_the_judge_writes(tmp_path, monkeypatch, fake_docker,
                                                                           cli_task_dir, path, how):
    task = load_task(cli_task_dir)
    details = with_left_behind(judges_result(task, "change-1", path == "image")[1], how)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    assert faults_for_every_solution(results), results


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_the_judges_own_entry_for_what_was_left_behind_gives_its_reason(tmp_path, monkeypatch, fake_docker,
                                                                        cli_task_dir, path):
    task = load_task(cli_task_dir)
    details = with_left_behind(judges_result(task, "change-1", path == "image")[1], "the judge's own")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    judged = [r for r in results if r["step"] == "change-1"]
    assert judged and all(not r["ok"] and not r.get("judge_fault") and LEFT in r["problems"][0] for r in judged), results


# --- the one place is total: a record or a fault, never a trace, never what was there before ---

BEFORE = "words of a result from before this run"


def total_reader_inputs():
    """What a judging may leave, of every kind: (name, paths it is tried on, how it is left)."""
    def links(folder, reward, details, elsewhere):
        for name in ("reward.json", "details.json"):
            (folder / name).symlink_to(elsewhere / name)

    def hard_links(folder, reward, details, elsewhere):
        for name in ("reward.json", "details.json"):
            os.link(elsewhere / name, folder / name)

    def dangling(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").symlink_to(elsewhere / "no-such-file.json")

    def a_folder(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").mkdir()

    def a_pipe(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        os.mkfifo(folder / "details.json")

    def unreadable(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")
        (folder / "details.json").chmod(0)

    def too_large(folder, reward, details, elsewhere):
        details = {**details, "tests": [dict(c) for c in details["tests"]]}
        details["tests"][0]["message"] = "x" * (2 * 2 ** 20)
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")

    def not_utf8(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        text = json.dumps(details).replace('"message": ""', '"message": "MARK"', 1).encode("utf-8")
        (folder / "details.json").write_bytes(text.replace(b"MARK", b"\xff\xfe"))

    def random_bytes(folder, reward, details, elsewhere):
        (folder / "reward.json").write_bytes(bytes(range(256)) * 16)
        (folder / "details.json").write_bytes(bytes(reversed(range(256))) * 16)

    def empty(folder, reward, details, elsewhere):
        (folder / "reward.json").write_bytes(b"")
        (folder / "details.json").write_bytes(b"")

    def deep(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").write_text("[" * 500_000 + "]" * 500_000, encoding="utf-8")

    def huge_number(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward).replace("1.0", "9" * 4000, 1), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")

    def endless_number(folder, reward, details, elsewhere):
        (folder / "reward.json").write_text(json.dumps(reward).replace("1.0", "1e400", 1), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")

    def half_a_pair(folder, reward, details, elsewhere):
        details = {**details, "tests": [dict(c) for c in details["tests"]]}
        details["tests"][0]["message"] = "\ud800"
        (folder / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")

    def fault_through_a_link(folder, reward, details, elsewhere):
        (folder / "judge-fault.json").symlink_to(elsewhere / "fault.json")

    def fault_with_half_a_pair(folder, reward, details, elsewhere):
        (folder / "judge-fault.json").write_text(json.dumps({"judge_fault": "\ud800"}), encoding="utf-8")

    both, here = ("image", "unseparated"), ("unseparated",)
    return [("links to the files of another judging", both, links),
            ("hard links to the files of another judging", here, hard_links),
            ("a link that leads nowhere", both, dangling), ("a folder where a file goes", both, a_folder),
            ("a pipe where a file goes", here, a_pipe), ("a file that cannot be read", here, unreadable),
            ("a file above the bound", both, too_large), ("bytes that are no UTF-8", both, not_utf8),
            ("bytes of every value", both, random_bytes), ("empty files", both, empty),
            ("nesting half a million deep", both, deep), ("a number of 4,000 digits", both, huge_number),
            ("a number too large for a float", both, endless_number),
            ("half of a pair of surrogates", both, half_a_pair),
            ("an account of a fault through a link", both, fault_through_a_link),
            ("an account of a fault with half of a pair of surrogates", both, fault_with_half_a_pair)]


TOTAL = [(name, path, leave) for name, paths, leave in total_reader_inputs() for path in paths]


@pytest.mark.parametrize("name, path, leave", TOTAL, ids=[f"{name}-{path}" for name, path, _ in TOTAL])
def test_the_one_place_gives_a_record_or_a_fault_for_anything_a_judging_leaves(tmp_path, monkeypatch, fake_docker,
                                                                              cli_task_dir, name, path, leave):
    """Whatever kind of file, link, permission, size, depth, number or bytes a judging leaves, the tool
    gives a fault in its own form for every solution: never a trace, never a result from before this
    run or from outside its place, and its own results stay text that can be printed."""
    if name == "a file that cannot be read" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads every file")
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1", path == "image")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
    marked = {**details, "tests": [{**c, "message": BEFORE} for c in details["tests"]]}
    (elsewhere / "details.json").write_text(json.dumps(marked), encoding="utf-8")
    (elsewhere / "fault.json").write_text(json.dumps({"judge_fault": BEFORE}), encoding="utf-8")
    ended = "3" if "account of a fault" in name else "0"
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details,
                          lambda folder: leave(folder, reward, details, elsewhere), ended)
    assert faults_for_every_solution(results), results
    printed = json.dumps(results, ensure_ascii=False)
    printed.encode("utf-8")
    assert BEFORE not in printed, results


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_an_error_inside_the_one_place_is_a_fault(tmp_path, monkeypatch, fake_docker, cli_task_dir, path):
    """Any error inside the one place is a fault in the tool's form, never a trace."""
    import dodambench.vet as vetting
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1", path == "image")

    def breaks(*args, **kwargs):
        raise KeyError("an error nobody thought of")
    monkeypatch.setattr(vetting, "record_of", breaks)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details)
    assert faults_for_every_solution(results), results


@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_a_result_from_before_this_run_is_never_read(tmp_path, monkeypatch, fake_docker, cli_task_dir, path):
    """The place is made empty for this run: a judging that leaves nothing finds no earlier result."""
    task = load_task(cli_task_dir)
    reward, details = judges_result(task, "change-1", path == "image")
    for step in task.steps:
        for variant in task.solutions_for("python", step.name):
            place = tmp_path / "out" / step.name / variant.label
            place.mkdir(parents=True, exist_ok=True)
            (place / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
            (place / "details.json").write_text(json.dumps(details), encoding="utf-8")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, reward, details, lambda folder: None)
    assert faults_for_every_solution(results), results


# --- a served task: a test may be parametrized, so its cases are the test's name with what it was given ---

SERVED = Path(__file__).resolve().parents[1] / "tasks" / "web-signup"   # read only; whatever is made goes to tmp_path

@pytest.mark.parametrize("path", ["image", "unseparated"])
def test_a_served_tasks_cases_may_be_the_parametrized_cases_of_its_tests(tmp_path, monkeypatch, fake_docker, path):
    task = load_task(SERVED)
    details = picture(judges_result(task, "change-1", path == "image")[1], "a parametrized case of a test")
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    secure = [r for r in results if r["step"] == "change-1" and r["solution"] == "secure"]
    assert secure and secure[0]["ok"], results


@pytest.mark.parametrize("path", ["image", "unseparated"])
@pytest.mark.parametrize("how", ["a case of no test of the file", "a test of the file without a case", "a case twice",
                                 "a whole-file entry that passed", "a whole-file entry beside the file's cases",
                                 "a case more, of a file the step does not run"])
def test_a_served_tasks_result_that_is_not_the_whole_picture_is_a_fault(tmp_path, monkeypatch, fake_docker,
                                                                        path, how):
    task = load_task(SERVED)
    details = picture(judges_result(task, "change-1", path == "image")[1], how)
    results = vetted_with(tmp_path, monkeypatch, fake_docker, task, path, consistent(details, path == "image"), details)
    assert faults_for_every_solution(results), results


# The room the tool tells for a judging, through its own path: how the judging went, and how one case of it
# came out and what it said of the most it can have held; another case passed saying 100 MB, and the rest
# say nothing of it.
ROOM_BOUNDS = {"none": None, "a number": 300 * 2**20, "nought": 0, "a yes or a no": True}
ROOM_MESSAGES = {"passed": "", "failed": "the test's own failure", "skipped": "the feature it attacks does not work"}


def room_rows() -> list[tuple]:
    """(how the judging went, the reason of its unmeasured cases, one case's outcome, its bound)."""
    from dodambench.judge import UNMEASURED_REASONS
    from dodambench.vet import COULD_NOT_JUDGE
    rows = [("could not run: " + kind.__name__, None, None, None) for kind in COULD_NOT_JUDGE]
    rows += [(ended, None, None, None) for ended in ("ended without a result", "the judge wrote a fault", "a result of another shape",
                                                      "the program's side left something")]
    for outcome in ("passed", "failed", "not run"):
        rows += [("ran", None, outcome, bound) for bound in ROOM_BOUNDS]
    for reason in UNMEASURED_REASONS:
        for outcome in ("passed", "failed", "unmeasured", "not run"):
            rows += [("has an unmeasured case", reason, outcome, bound) for bound in ROOM_BOUNDS]
    return rows


def test_the_room_a_judging_leaves_is_told_from_the_bounds_the_judge_worked_out(tmp_path, fake_docker, monkeypatch):
    """Every row, through the tool's own path: a judging that could not run, for every kind of error the
    tool takes as that; one that ended without a result, in which the judge wrote a fault of its own, or
    whose result is of another shape; one that ran, and one in which the program's side left something
    the judge could not take back; and one that has a case not judged for measuring, for
    every reason the judge gives that. One case of it passed, failed, was not judged for measuring or was
    not run, saying the most it can have held with nothing, a number, nought, or a yes or a no, beside a
    case that passed saying 100 MB. The room is the limit of 512 MB less the largest bound that is a
    number among the cases that passed or were not judged for measuring; where the judging gave no result
    and no such bound, no room is told, never the whole limit."""
    from dodambench import vet as tool
    from dodambench.judge import UNMEASURED_REASONS
    rows = room_rows()
    assert len(rows) == 7 + 12 + 16 * len(UNMEASURED_REASONS), len(rows)
    wrong = []
    for n, (judging, reason, outcome, bound) in enumerate(rows):
        leaves = tmp_path / f"leaves-{n}"
        leaves.mkdir()
        with monkeypatch.context() as m:
            m.setenv("FAKE_LEAVES", str(leaves))
            m.setenv("FAKE_EXIT", "0")
            expected = None
            if judging.startswith("could not run"):
                kind = next(k for k in tool.COULD_NOT_JUDGE if judging.endswith(k.__name__))

                def cannot(*args, kind=kind, **kwargs):
                    raise kind("the container could not be made")
                m.setattr(tool, "judge_in_image", cannot)
            elif judging == "ended without a result":
                m.setenv("FAKE_EXIT", "137")
            elif judging == "the judge wrote a fault":
                (leaves / "judge-fault.json").write_text(json.dumps({"judge_fault": "the judge failed"}), encoding="utf-8")
                m.setenv("FAKE_EXIT", "3")
            else:
                given, details = judges_result(load_task(SAMPLE), "build")
                if judging == "a result of another shape":
                    for case in details["tests"]:
                        case["memory_bound_bytes"] = 300 * 2**20
                    given.pop("reward")
                elif judging == "the program's side left something":
                    details = with_left_behind(details, "the judge's own")
                    details["tests"][2]["memory_bound_bytes"] = 100 * 2**20
                    given, expected = consistent(details, True), 412.0
                else:
                    word = "skipped" if outcome == "not run" else outcome
                    case = details["tests"][0]
                    case.update(outcome=word, message=reason if word == "unmeasured" else ROOM_MESSAGES[word])
                    if ROOM_BOUNDS[bound] is not None:
                        case["memory_bound_bytes"] = ROOM_BOUNDS[bound]
                    if judging == "has an unmeasured case":
                        details["tests"][1].update(outcome="unmeasured", message=reason)
                    details["tests"][2]["memory_bound_bytes"] = 100 * 2**20
                    details["unmeasured"] = [f"{c['file']}::{c['name']}" for c in details["tests"] if c["outcome"] == "unmeasured"]
                    given = reward_of_cases(details["tests"], True, "build", True)
                    worked_out = (word in ("passed", "unmeasured") and isinstance(ROOM_BOUNDS[bound], int)
                                  and not isinstance(ROOM_BOUNDS[bound], bool))
                    expected = round(512 - max([100 * 2**20] + ([ROOM_BOUNDS[bound]] if worked_out else [])) / 2**20, 1)
                (leaves / "reward.json").write_text(json.dumps(given), encoding="utf-8")
                (leaves / "details.json").write_text(json.dumps(details), encoding="utf-8")
            results = [r for r in tool.vet_in_image(load_task(SAMPLE), "python", tmp_path / str(n), "dodam-bench/python:dev")
                       if r["step"] == "build"]
        if not results or any(r.get("room_mb") != expected for r in results):
            wrong.append(((judging, reason, outcome, bound), [r.get("room_mb") for r in results], expected))
    assert not wrong, wrong


def test_the_table_of_the_room_has_every_way_a_judging_ends_and_every_reason():
    """The table has a row for every kind of error the tool takes as a judging it could not run, and for
    every reason the judge gives a case not judged for measuring: one the code gains and the table lacks
    fails this test."""
    from dodambench.judge import UNMEASURED_REASONS
    from dodambench.vet import COULD_NOT_JUDGE
    rows = room_rows()
    assert {j for j, *_ in rows if j.startswith("could not run")} == {"could not run: " + k.__name__ for k in COULD_NOT_JUDGE}
    assert {r for _, r, *_ in rows if r} == set(UNMEASURED_REASONS)


def test_the_tool_prints_for_each_language_the_smallest_room_its_prepared_solutions_left(tmp_path, monkeypatch, capsys):
    """Whoever writes a task sees, for each language, the smallest room between the memory bound and the
    limit that a prepared solution's judging left. It refuses nothing for it."""
    import dodambench.vet as tool
    rooms = {"python": [496.8, 500.1], "go": [None, 401.6]}
    monkeypatch.setattr(tool, "vet", lambda task, lang, out, **kwargs: [
        {"step": "build", "solution": f"s{n}", "ok": True, "problems": [], "seconds": 1.0, "room_mb": room}
        for n, room in enumerate(rooms[lang])])
    code = tool.main([str(SAMPLE), "--lang", "python",
                      "--lang", "go", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "room  python      the smallest room between the memory bound and the limit: 496.8 MB" in out, out
    assert "room  go          the smallest room between the memory bound and the limit: 401.6 MB" in out, out
