from pathlib import Path

from conftest import CANARY, write
from dodambench.task import load_task, validate


def errors_of(root: Path) -> list[str]:
    return validate(load_task(root))


def test_valid_task_has_no_errors(task_dir):
    assert errors_of(task_dir) == []


def test_loads_steps_in_order(task_dir):
    task = load_task(task_dir)
    assert [s.name for s in task.steps] == ["build", "change-1"]
    assert task.languages == ["python", "go"]


def test_missing_instruction_file_is_reported(task_dir):
    (task_dir / "steps/change-1/instruction.md").unlink()
    assert any("steps/change-1/instruction.md" in e for e in errors_of(task_dir))


def test_unknown_domain_is_reported(task_dir):
    toml = task_dir / "task.toml"
    toml.write_text(toml.read_text().replace('domain = "web"', 'domain = "games"'))
    assert any("domain" in e for e in errors_of(task_dir))


def test_unknown_language_is_reported(task_dir):
    toml = task_dir / "task.toml"
    toml.write_text(toml.read_text().replace('["python", "go"]', '["python", "cobol"]'))
    assert any("cobol" in e for e in errors_of(task_dir))


def test_duplicate_step_names_are_reported(task_dir):
    toml = task_dir / "task.toml"
    toml.write_text(toml.read_text().replace('name = "change-1"', 'name = "build"'))
    assert any("duplicate step" in e for e in errors_of(task_dir))


def test_canary_must_not_reach_agent_instructions(task_dir):
    write(task_dir, "steps/build/instruction.md", f"Build it. {CANARY}\n")
    assert any("canary" in e and "instruction" in e for e in errors_of(task_dir))


def test_canary_must_not_reach_spec_shown_to_agents(task_dir):
    write(task_dir, "spec/contract.openapi.yaml", f"# {CANARY}\nopenapi: 3.0.3\n")
    assert any("canary" in e and "spec" in e for e in errors_of(task_dir))


def test_every_hidden_test_carries_the_canary(task_dir):
    write(task_dir, "tests/functional/test_health.py", "def test_x():\n    assert True\n")
    assert any("canary" in e and "test_health.py" in e for e in errors_of(task_dir))


def test_unlisted_test_file_is_reported(task_dir):
    write(task_dir, "tests/boundary/test_empty.py", f"# {CANARY}\n")
    assert any("not listed" in e and "test_empty.py" in e for e in errors_of(task_dir))


def test_listed_test_file_must_exist(task_dir):
    (task_dir / "tests/functional/test_version.py").unlink()
    assert any("test_version.py" in e for e in errors_of(task_dir))


def test_security_test_needs_known_weakness(task_dir):
    manifest = task_dir / "tests/manifest.toml"
    manifest.write_text(manifest.read_text().replace('weakness = "sql-injection"', 'weakness = "magic"'))
    assert any("weakness" in e for e in errors_of(task_dir))


def test_test_kind_must_match_folder(task_dir):
    manifest = task_dir / "tests/manifest.toml"
    text = manifest.read_text().replace('file = "functional/test_health.py"\nkind = "functional"',
                                        'file = "functional/test_health.py"\nkind = "boundary"')
    manifest.write_text(text)
    assert any("folder" in e for e in errors_of(task_dir))


def test_test_step_must_exist(task_dir):
    manifest = task_dir / "tests/manifest.toml"
    manifest.write_text(manifest.read_text().replace('step = "change-1"', 'step = "change-9"'))
    assert any("change-9" in e for e in errors_of(task_dir))


def test_http_task_needs_app_section(task_dir):
    toml = task_dir / "task.toml"
    text = toml.read_text()
    start = text.index("[app]")
    end = text.index("[limits]")
    toml.write_text(text[:start] + text[end:])
    assert any("[app]" in e for e in errors_of(task_dir))


def test_select_tests_for_a_step_includes_earlier_steps(task_dir):
    task = load_task(task_dir)
    files = [t.file for t in task.tests_for("change-1")]
    assert files == ["functional/test_health.py", "security/test_injection.py", "functional/test_version.py"]
    assert [t.file for t in task.tests_for("build")] == ["functional/test_health.py", "security/test_injection.py"]


def test_superseded_tests_drop_out_of_later_steps(task_dir):
    manifest = task_dir / "tests/manifest.toml"
    text = manifest.read_text().replace(
        'file = "functional/test_health.py"\nkind = "functional"\nstep = "build"',
        'file = "functional/test_health.py"\nkind = "functional"\nstep = "build"\nsuperseded_in = ["change-1"]')
    manifest.write_text(text)
    task = load_task(task_dir)
    assert errors_of(task_dir) == []
    assert "functional/test_health.py" not in [t.file for t in task.tests_for("change-1")]
