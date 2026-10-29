import os
import tomllib
from pathlib import Path

import pytest

from conftest import CANARY
from dodambench.convert import ConversionError, to_harbor, verifier_env
from dodambench.languages import LANGUAGES
from dodambench.task import load_task


def harbor_toml(out: Path) -> dict:
    return tomllib.loads((out / "task.toml").read_text(encoding="utf-8"))


def test_core_languages_are_the_seven_existing_ones():
    assert list(LANGUAGES) == ["python", "typescript", "java", "csharp", "go", "rust", "cpp"]


def test_converts_to_a_separate_verifier_multi_step_task(task_dir, tmp_path):
    out = to_harbor(load_task(task_dir), "python", tmp_path / "out")
    assert out.name == "web-demo__python"
    t = harbor_toml(out)
    assert t["schema_version"] == "1.3"
    assert t["task"]["name"] == "dodam/web-demo-python"
    assert t["verifier"]["environment_mode"] == "separate"
    assert t["verifier"]["network_mode"] == "no-network"
    assert t["agent"]["timeout_sec"] == 1800
    assert t["verifier"]["timeout_sec"] == 600
    assert t["environment"]["cpus"] == 2 and t["environment"]["memory_mb"] == 2048
    assert [s["name"] for s in t["steps"]] == ["build", "change-1"]
    assert all("min_reward" not in s for s in t["steps"])
    assert t["metadata"]["language"] == "python"


def test_agent_instructions_name_the_language_and_drop_nothing(task_dir, tmp_path):
    out = to_harbor(load_task(task_dir), "go", tmp_path / "out")
    text = (out / "steps/build/instruction.md").read_text(encoding="utf-8")
    assert "Build a demo API with GET /health." in text
    assert LANGUAGES["go"].name in text
    assert "8080" in text and "DB_PATH" in text and "APP_SECRET" in text
    assert CANARY not in text


def test_hidden_tests_go_to_the_verifier_only(task_dir, tmp_path):
    out = to_harbor(load_task(task_dir), "python", tmp_path / "out")
    assert (out / "tests/functional/test_health.py").exists()
    assert (out / "tests/manifest.toml").exists()
    test_sh = out / "tests/test.sh"
    assert test_sh.exists() and os.access(test_sh, os.X_OK)
    assert (out / "steps/build/tests/step.env").read_text().strip() == "DODAM_STEP=build"
    assert not (out / "environment").exists() or not any((out / "environment").rglob("test_*.py"))
    lang_env = (out / "tests/lang.env").read_text()
    assert "BUILD_CMD=" in lang_env and "START_CMD=" in lang_env


def test_solution_script_picks_a_variant(task_dir, tmp_path):
    out = to_harbor(load_task(task_dir), "python", tmp_path / "out")
    solve = (out / "steps/build/solution/solve.sh").read_text()
    assert "SOLUTION" in solve and "secure" in solve
    assert (out / "steps/build/solution/variants/secure/main.py").exists()


def test_all_languages_get_their_own_task(task_dir, tmp_path):
    task = load_task(task_dir)
    outs = {to_harbor(task, lang, tmp_path / "out").name for lang in task.languages}
    assert outs == {"web-demo__python", "web-demo__go"}


def test_language_outside_the_task_is_refused(task_dir, tmp_path):
    with pytest.raises(ConversionError):
        to_harbor(load_task(task_dir), "rust", tmp_path / "out")


def test_invalid_task_is_refused(task_dir, tmp_path):
    (task_dir / "steps/change-1/instruction.md").unlink()
    with pytest.raises(ConversionError):
        to_harbor(load_task(task_dir), "python", tmp_path / "out")


def test_variants_are_written_with_their_replacements(task_dir, tmp_path):
    out = to_harbor(load_task(task_dir), "python", tmp_path / "out")
    variants = out / "steps/build/solution/variants"
    assert (variants / "secure/main.py").read_text() == "app = None\n"
    assert (variants / "vulnerable-no-check/main.py").read_text() == "app = 'open'\n"
    assert not (out / "steps/change-1/solution/variants").exists()


def test_verifier_env_lists_steps_and_commands(task_dir):
    env = verifier_env(load_task(task_dir), "go")
    assert env["STEPS"] == "build,change-1"
    assert env["BUILD_CMD"] == LANGUAGES["go"].build_cmd and env["START_CMD"] == LANGUAGES["go"].start_cmd
    assert env["PORT"] == "8080"
