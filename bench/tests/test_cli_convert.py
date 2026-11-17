"""A command-line task as a Harbor task: what the agent is told, what only the verifier gets."""
import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from conftest import CANARY
from dodambench.convert import to_harbor, verifier_env
from dodambench.languages import LANGUAGES
from dodambench.task import load_task

BENCH = Path(__file__).resolve().parents[1]
SAMPLE = BENCH / "tasks" / "cli-csv-json"
WEB = BENCH / "tasks" / "web-signup"


def tree(root: Path, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).as_posix() not in skip}


def test_the_agent_is_told_how_the_program_is_run_and_limited(cli_task_dir, tmp_path):
    out = to_harbor(load_task(cli_task_dir), "go", tmp_path / "out")
    text = (out / "steps/build/instruction.md").read_text(encoding="utf-8")
    assert "Build a tool" in text and LANGUAGES["go"].name in text
    assert "`go build -o app .`" in text and "`/app/app`" in text
    assert "working folder" in text and "without network" in text
    for limit in ("2 seconds", "256 MB of memory", "64 KB", "8 processes (threads count", "8 MB of files"):
        assert limit in text, limit
    for web_only in ("PORT", "DB_PATH", "APP_SECRET", "Framework"):
        assert web_only not in text, web_only
    assert CANARY not in text
    python = (to_harbor(load_task(cli_task_dir), "python", tmp_path / "out") / "instruction.md").read_text()
    assert "`python3 /app/main.py`" in python and "FastAPI" not in python


def test_hidden_tests_and_the_case_runner_go_to_the_verifier_only(cli_task_dir, tmp_path):
    out = to_harbor(load_task(cli_task_dir), "python", tmp_path / "out")
    assert (out / "tests/functional/test_upper.py").exists()
    assert (out / "tests/lib/dodam_cli.py").exists() and (out / "tests/lib/dodam_judge.py").exists()
    t = tomllib.loads((out / "task.toml").read_text(encoding="utf-8"))
    assert t["verifier"]["environment_mode"] == "separate"
    assert t["verifier"]["network_mode"] == "no-network"
    assert t["artifacts"] == ["/app"]
    # what the agent gets: the instructions. They carry no test, no expected answer and no prepared solution.
    for step in ("build", "change-1"):
        seen = (out / "steps" / step / "instruction.md").read_text(encoding="utf-8")
        for hidden in (CANARY, "test_upper", "planted.txt", "copies-anywhere", "def test_"):
            assert hidden not in seen, hidden
    # the environment folder goes into both containers: it may hold the caps and nothing else
    assert [p.name for p in (out / "environment").iterdir()] in ([], ["docker-compose.yaml"]) if (out / "environment").exists() else True
    for path in (out / "environment").glob("*"):
        for hidden in (CANARY, "test_upper", "planted.txt", "copies-anywhere", "def test_"):
            assert hidden not in path.read_text(encoding="utf-8"), hidden


def test_the_verifier_learns_the_command_and_the_limits_of_a_case(cli_task_dir):
    env = verifier_env(load_task(cli_task_dir), "python")
    assert env.get("INTERFACE") == "cli" and env.get("RUN_CMD") == "python3 {app}/main.py"
    assert env["BUILD_CMD"] == "" and env["STEPS"] == "build,change-1"
    assert [env.get(key) for key in ("CASE_TIMEOUT_SEC", "CASE_MEMORY_MB", "CASE_OUTPUT_KB", "CASE_PROCESSES",
                                     "CASE_DISK_MB")] == ["2", "256", "64", "8", "8"]
    assert "PORT" not in env and "START_CMD" not in env
    go = verifier_env(load_task(cli_task_dir), "go")
    assert go.get("RUN_CMD") == "{app}/app" and go["BUILD_CMD"] == "go build -o app ."


def test_every_language_has_a_way_to_run_a_command_line_program():
    for key, lang in LANGUAGES.items():
        assert "{app}/" in getattr(lang, "run_cmd", ""), key


def test_one_set_of_tests_judges_every_language(tmp_path):
    """The verifier's tests folder is the same for every language but for the file that names the commands."""
    task = load_task(SAMPLE)
    skip = ("lang.env",)
    first = tree(to_harbor(task, "python", tmp_path / "out") / "tests", skip)
    assert any(name.startswith("security/") for name in first)
    for lang in task.languages[1:]:
        assert tree(to_harbor(task, lang, tmp_path / "out") / "tests", skip) == first, lang
    # and no test names a language or one of its tools
    words = ("python", "typescript", "java", "csharp", "golang", "rust", "cpp", "node", "dotnet", "cargo", "maven")
    for name, data in first.items():
        if name.startswith(("functional/", "boundary/", "security/")):
            text = data.decode("utf-8").lower()
            assert not [w for w in words if w in text], name


# The Harbor task made from the web task, for each language, at the commit before command-line tasks:
# every file but the judge itself, which is shared. Command-line support changes none of them.
WEB_BEFORE = {
    "python": "6140e044a7b53b688f401f72ff744f69a53d4144195616511e14bf57b394715b",
    "typescript": "36706c8df9930307239e8eefa8e4e83cf7c3f523961f49ec765fc83e3458d8c2",
    "java": "65c4dd690bda93b77fbdf8deec8760219702681ee82dc7fa5e2fc7c951dc84c4",
    "csharp": "26d30b029fc7202209f5b3ade5a280495dd81c08b83715b46f19dfbda8d9e458",
    "go": "4650543c2aacc9cb0081d32ec39faf9ea82e9f09bb8c84afb2b96bc80671128d",
    "rust": "e0199e9988297d49818977cb791cc1fe8aabfd7b3737eb04a867c36771a08657",
    "cpp": "ee8a900dfe1b943b41a57c3a809ea7d79f17488774ed6a05f688ce2c89a436ed",
}


def test_nothing_changes_in_what_the_web_task_becomes(tmp_path):
    task = load_task(WEB)
    for lang, before in WEB_BEFORE.items():
        digest = hashlib.sha256()
        for name, data in tree(to_harbor(task, lang, tmp_path / "out"), ("tests/lib/dodam_judge.py",)).items():
            digest.update(name.encode() + b"\0" + data + b"\0")
        assert digest.hexdigest() == before, lang


def test_a_web_task_converts_as_before_and_is_vetted_in_a_capped_container():
    import dodambench.vet as vetting
    room_of = getattr(vetting, "container_room", None)
    assert room_of is not None, "vetting has no way to say what caps a container gets"
    task = load_task(WEB)
    room = room_of(task, 2048)
    # the task asks for more memory than this machine has: the cap is lowered so that it holds, and said
    assert room["memory_mb"] == 1536 and room["adjusted"]["asked_mb"] == 4096 and room["adjusted"]["message"]
    assert room_of(task, 65536)["memory_mb"] == 4096 and "adjusted" not in room_of(task, 65536)
    assert room["processes"] >= 256 and "cases" not in room
    cmd = vetting.docker_create_cmd("name", "dodam-bench/go:dev", "build", room)
    assert cmd[:8] == ["docker", "create", "--name", "name", "--network", "none", "--user", "0"]
    assert cmd[cmd.index("--memory") + 1] == "1536m" and cmd[cmd.index("--memory-swap") + 1] == "1536m"
    assert cmd[cmd.index("--pids-limit") + 1] == str(room["processes"]) and "--tmpfs" not in cmd
    env = verifier_env(task, "go")
    assert set(env) == {"STEPS", "BUILD_CMD", "START_CMD", "PORT", "APP_DB", "APP_SECRET_NEEDED", "DEPS_DIR", "DEPS_LINKS"}
    made = to_harbor(task, "go", Path(__import__("tempfile").mkdtemp()))
    assert not (made / "tests/lib/dodam_cli.py").exists() and not (made / "environment").exists()


def test_a_command_line_task_carries_its_caps_onto_the_path_the_measurement_uses(cli_task_dir, tmp_path):
    """Harbor's task settings can cap memory and processors. The cap on processes and the capped folder
    of the cases travel in a file Harbor merges into the container it makes."""
    import dodambench.vet as vetting
    task = load_task(cli_task_dir)
    made = to_harbor(task, "python", tmp_path / "out")
    compose = made / "environment/docker-compose.yaml"
    assert compose.is_file(), "nothing tells Harbor to cap the processes or to give the cases their folder"
    text = compose.read_text()
    assert "pids_limit:" in text and "/dodam-cases:" in text and "size=8m" in text and "nr_inodes=20000" in text
    assert CANARY not in text
    room_of = getattr(vetting, "container_room", None)
    assert room_of is not None
    room = room_of(task, 65536)
    cmd = vetting.docker_create_cmd("name", "dodam-bench/python:dev", "build", room)
    assert cmd[cmd.index("--tmpfs") + 1] in text, "vetting and the measurement must give the cases the same folder"
    assert f"pids_limit: {room['processes']}" in text
    # a machine that cannot give the cases their room is refused, with a diagnostic that is JSON first
    with pytest.raises(vetting.VetError) as refused:
        room_of(task, 400)
    assert json.loads(str(refused.value))["key"] == "limits.memory_mb"
