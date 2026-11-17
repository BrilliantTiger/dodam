"""The format rules of a command-line task, and the checker's JSON-first diagnostics."""
import json
import subprocess
import sys
from pathlib import Path

from conftest import CANARY, edit, write
from dodambench.task import load_task, validate

BENCH = Path(__file__).resolve().parents[1]
SAMPLE = BENCH / "tasks" / "cli-csv-json"


def errors_of(root: Path) -> list[str]:
    return validate(load_task(root))


def check(root: Path, *flags: str) -> subprocess.CompletedProcess:
    """Run the format checker as a command, the way a person or a script runs it."""
    return subprocess.run([sys.executable, "-m", "dodambench.task", str(root), *flags],
                          capture_output=True, text=True, cwd=BENCH)


def report_of(done: subprocess.CompletedProcess) -> dict:
    assert done.stdout.lstrip().startswith("{"), f"the checker printed no JSON: {done.stdout!r} {done.stderr[-300:]!r}"
    return json.loads(done.stdout)


def test_a_valid_command_line_task_has_no_errors(cli_task_dir):
    assert errors_of(cli_task_dir) == []


def test_the_sample_command_line_task_is_accepted():
    assert errors_of(SAMPLE) == []
    task = load_task(SAMPLE)
    assert task.meta["interface"] == "cli"
    assert [s.name for s in task.steps] == ["build", "change-1"]
    assert {t.kind for t in task.tests} == {"functional", "boundary", "security"}


def test_a_command_line_task_needs_the_limits_of_a_case(cli_task_dir):
    toml = cli_task_dir / "task.toml"
    text = toml.read_text()
    start, end = text.index("[cli]"), text.index("[limits]")
    toml.write_text(text[:start] + text[end:])
    assert any("[cli]" in e for e in errors_of(cli_task_dir))


def test_every_limit_of_a_case_must_be_a_positive_whole_number(cli_task_dir):
    for key in ("case_timeout_sec", "case_memory_mb", "case_output_kb", "case_processes", "case_disk_mb"):
        for bad in ("0", "-3", '"ten"', "1.5", "true"):
            root = cli_task_dir.parent / f"{key}-{abs(hash(bad))}"
            if not root.exists():
                import shutil
                shutil.copytree(cli_task_dir, root)
            toml = root / "task.toml"
            lines = [f"{key} = {bad}" if line.startswith(key + " =") else line for line in toml.read_text().splitlines()]
            toml.write_text("\n".join(lines) + "\n")
            assert any(key in e for e in errors_of(root)), (key, bad)


def test_a_missing_limit_and_an_unknown_one_are_reported(cli_task_dir):
    edit(cli_task_dir / "task.toml", "case_processes = 8\n", "case_threads = 8\n")
    errors = errors_of(cli_task_dir)
    assert any("case_processes" in e for e in errors) and any("case_threads" in e for e in errors)


def test_a_case_may_not_take_all_the_memory_the_judge_lives_in(cli_task_dir):
    edit(cli_task_dir / "task.toml", "case_memory_mb = 256", "case_memory_mb = 2048")
    assert any("case_memory_mb" in e and "memory_mb" in e for e in errors_of(cli_task_dir))


def test_a_command_line_task_takes_no_server_settings(cli_task_dir):
    edit(cli_task_dir / "task.toml", "[limits]", "[app]\nport = 8080\n\n[limits]")
    assert any("[app]" in e for e in errors_of(cli_task_dir))


def test_a_web_task_takes_no_case_limits(task_dir):
    edit(task_dir / "task.toml", "[limits]", "[cli]\ncase_timeout_sec = 5\n\n[limits]")
    assert any("[cli]" in e for e in errors_of(task_dir))


def test_the_judge_must_have_time_for_a_program_that_hangs_in_every_case(cli_task_dir):
    """Every test may use its whole time limit, so the verifier's time must cover the build and all of them."""
    edit(cli_task_dir / "task.toml", "verifier_timeout_sec = 1200", "verifier_timeout_sec = 900")
    assert any("verifier_timeout_sec" in e and "hangs" in e for e in errors_of(cli_task_dir))


def test_a_hidden_test_may_not_start_the_program_itself(cli_task_dir):
    for word in ("import subprocess", "os.system('x')", "os.execv('x', [])", "os.fork()", "os.popen('x')"):
        path = cli_task_dir / "tests/functional/test_upper.py"
        original = path.read_text()
        path.write_text(original + f"\n{word}\n")
        assert any("test_upper.py" in e and "dodam_cli" in e for e in errors_of(cli_task_dir)), word
        path.write_text(original)
    assert errors_of(cli_task_dir) == []


def test_a_hidden_test_may_not_ask_which_language_it_judges(cli_task_dir):
    path = cli_task_dir / "tests/functional/test_upper.py"
    original = path.read_text()
    for word in ("open('/tests/lang.env')", "os.environ['DODAM_CLI_CONFIG']"):
        path.write_text(original + f"\n{word}\n")
        assert any("test_upper.py" in e and "language" in e for e in errors_of(cli_task_dir)), word


def test_a_hidden_test_must_use_the_case_runner_and_may_not_multiply_itself(cli_task_dir):
    write(cli_task_dir, "tests/boundary/test_usage.py", f"# {CANARY}\ndef test_nothing():\n    assert True\n")
    assert any("test_usage.py" in e and "dodam_cli" in e for e in errors_of(cli_task_dir))
    path = cli_task_dir / "tests/functional/test_upper.py"
    path.write_text(path.read_text() + "\nimport pytest\npytest.mark.parametrize\n")
    assert any("test_upper.py" in e and "parametrize" in e for e in errors_of(cli_task_dir))


def test_a_security_test_of_overwriting_is_a_known_weakness(cli_task_dir):
    manifest = cli_task_dir / "tests/manifest.toml"
    manifest.write_text(manifest.read_text().replace('weakness = "path-traversal"', 'weakness = "file-overwrite"'))
    assert not any("weakness" in e and "manifest" in e for e in errors_of(cli_task_dir))


def test_the_checker_prints_json_and_accepts_the_sample():
    done = check(SAMPLE)
    report = report_of(done)
    assert done.returncode == 0, done.stdout
    assert report == {"task": "cli-csv-json", "ok": True, "problems": []}


def test_the_checker_refuses_a_malformed_command_line_task_with_json_first(cli_task_dir):
    edit(cli_task_dir / "task.toml", "case_timeout_sec = 2", "case_timeout_sec = 0")
    edit(cli_task_dir / "task.toml", "case_disk_mb = 8\n", "")
    done = check(cli_task_dir)
    assert done.returncode == 1
    report = report_of(done)
    assert report["task"] == "cli-demo" and report["ok"] is False
    by_key = {p.get("key"): p for p in report["problems"]}
    assert by_key["cli.case_timeout_sec"]["file"] == "task.toml"
    assert by_key["cli.case_timeout_sec"]["severity"] == "error"
    assert "positive whole number" in by_key["cli.case_timeout_sec"]["message"]
    assert "cli.case_disk_mb" in by_key
    # the lines a person reads are made from the same data
    text = check(cli_task_dir, "--text")
    assert text.returncode == 1
    for problem in report["problems"]:
        assert problem["message"] in text.stdout
    assert not text.stdout.lstrip().startswith("{")


def test_the_checker_reports_a_task_it_cannot_read_as_json(tmp_path):
    write(tmp_path, "broken/task.toml", "schema = = 1\n")
    done = check(tmp_path / "broken")
    assert done.returncode == 2
    report = report_of(done)
    assert report["ok"] is False and report["problems"][0]["file"] == "task.toml"
