import textwrap
from pathlib import Path

import pytest

CANARY = "dodam-bench-canary-00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def pilot_guard_folder(tmp_path: Path, monkeypatch) -> Path:
    """The pilot's five-hour guard lives in one folder per user; in tests, in each test's own folder."""
    monkeypatch.setenv("DODAM_PILOT_GUARD_DIR", str(tmp_path))
    return tmp_path


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    """A small, valid two-step HTTP task."""
    root = tmp_path / "web-demo"
    write(root, "task.toml", f"""
        schema = 1
        id = "web-demo"
        title = "Demo API"
        domain = "web"
        kind = "new"
        interface = "http"
        visibility = "public"
        canary = "{CANARY}"
        created = "2026-10-29"
        languages = ["python", "go"]

        [app]
        port = 8080
        db = true
        secret = true

        [limits]
        agent_timeout_sec = 1800
        verifier_timeout_sec = 600
        cpus = 2
        memory_mb = 2048

        [[steps]]
        name = "build"
        instruction = "steps/build/instruction.md"

        [[steps]]
        name = "change-1"
        instruction = "steps/change-1/instruction.md"
    """)
    write(root, "steps/build/instruction.md", "Build a demo API with GET /health.\n")
    write(root, "steps/change-1/instruction.md", "Add GET /version.\n")
    write(root, "spec/contract.openapi.yaml", "openapi: 3.0.3\n")
    write(root, "tests/manifest.toml", """
        [[test]]
        file = "functional/test_health.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "security/test_injection.py"
        kind = "security"
        step = "build"
        weakness = "sql-injection"

        [[test]]
        file = "functional/test_version.py"
        kind = "functional"
        step = "change-1"
    """)
    for rel in ("functional/test_health.py", "security/test_injection.py", "functional/test_version.py"):
        write(root, f"tests/{rel}", f"# {CANARY}\ndef test_placeholder():\n    assert True\n")
    write(root, "solutions/secure/python/build/main.py", "app = None\n")
    write(root, "solutions/variants/python.toml", """
        [[variant]]
        name = "no-check"
        lang = "python"
        step = "build"
        expect = "vulnerable"
        weakness = "sql-injection"

        [[variant.replace]]
        file = "main.py"
        old = "app = None"
        new = "app = 'open'"
    """)
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")
