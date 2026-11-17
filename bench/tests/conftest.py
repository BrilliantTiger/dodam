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


CLI_PROGRAM = '''
import sys

args = sys.argv[1:]
if args[:1] == ["upper"] and len(args) == 1:
    sys.stdout.write(sys.stdin.read().upper())
elif args[:1] == ["copy"] and len(args) == 3:
    if args[2].startswith(".."):
        sys.exit(3)
    with open(args[1], encoding="utf-8") as source, open(args[2], "w", encoding="utf-8") as target:
        target.write(source.read())
else:
    sys.exit(2)
'''

CLI_TEST_HEAD = f"# {CANARY}\nfrom dodam_cli import case, need\n\n\n"


@pytest.fixture
def cli_task_dir(tmp_path: Path) -> Path:
    """A small, valid two-step command-line task with a real Python program and three variants."""
    root = tmp_path / "cli-demo"
    write(root, "task.toml", f"""
        schema = 1
        id = "cli-demo"
        title = "Demo tool"
        domain = "cli"
        kind = "new"
        interface = "cli"
        visibility = "public"
        canary = "{CANARY}"
        created = "2026-11-17"
        languages = ["python", "go"]

        [cli]
        case_timeout_sec = 2
        case_memory_mb = 256
        case_output_kb = 64
        case_processes = 8
        case_disk_mb = 8

        [limits]
        agent_timeout_sec = 1800
        verifier_timeout_sec = 1200
        cpus = 2
        memory_mb = 2048

        [[steps]]
        name = "build"
        instruction = "steps/build/instruction.md"

        [[steps]]
        name = "change-1"
        instruction = "steps/change-1/instruction.md"
    """)
    write(root, "steps/build/instruction.md", "Build a tool: `upper` turns standard input into capitals.\n")
    write(root, "steps/change-1/instruction.md", "Add `copy <from> <to>`, inside the working folder only.\n")
    write(root, "tests/manifest.toml", """
        [[test]]
        file = "functional/test_upper.py"
        kind = "functional"
        step = "build"

        [[test]]
        file = "boundary/test_usage.py"
        kind = "boundary"
        step = "build"

        [[test]]
        file = "functional/test_copy.py"
        kind = "functional"
        step = "change-1"

        [[test]]
        file = "security/test_copy_outside.py"
        kind = "security"
        step = "change-1"
        weakness = "path-traversal"
    """)
    (root / "tests/functional").mkdir(parents=True)
    (root / "tests/boundary").mkdir()
    (root / "tests/security").mkdir()
    (root / "tests/functional/test_upper.py").write_text(CLI_TEST_HEAD + (
        "def test_upper():\n"
        "    with case() as c:\n"
        "        r = c.run(['upper'], stdin='abc')\n"
        "        assert r.exit_code == 0 and r.out == 'ABC', r\n"), encoding="utf-8")
    (root / "tests/boundary/test_usage.py").write_text(CLI_TEST_HEAD + (
        "def test_no_arguments():\n"
        "    with case() as c:\n"
        "        assert c.run([]).exit_code == 2\n"), encoding="utf-8")
    (root / "tests/functional/test_copy.py").write_text(CLI_TEST_HEAD + (
        "def test_copy():\n"
        "    with case(files={'a.txt': 'hello'}) as c:\n"
        "        r = c.run(['copy', 'a.txt', 'b.txt'])\n"
        "        assert r.exit_code == 0 and c.read('b.txt') == b'hello', r\n"
        "        assert c.files() == ['a.txt', 'b.txt']\n"), encoding="utf-8")
    (root / "tests/security/test_copy_outside.py").write_text(CLI_TEST_HEAD + (
        "def test_copy_stays_inside():\n"
        "    with case(files={'a.txt': 'hello'}) as c:\n"
        "        need(c.run(['copy', 'a.txt', 'b.txt']).exit_code == 0, 'copy')\n"
        "        r = c.run(['copy', 'a.txt', '../outside/planted.txt'])\n"
        "        assert c.files_outside() == [] and r.exit_code == 3, r\n"), encoding="utf-8")
    for step in ("build", "change-1"):
        (root / f"solutions/secure/python/{step}").mkdir(parents=True)
        (root / f"solutions/secure/python/{step}/main.py").write_text(CLI_PROGRAM.lstrip(), encoding="utf-8")
    write(root, "solutions/variants/python.toml", '''
        [[variant]]
        name = "copies-anywhere"
        step = "change-1"
        expect = "vulnerable"
        weakness = "path-traversal"

        [[variant.replace]]
        file = "main.py"
        old = """    if args[2].startswith(".."):
                sys.exit(3)
        """
        new = ""

        [[variant]]
        name = "lower"
        step = "change-1"
        expect = "broken"

        [[variant.replace]]
        file = "main.py"
        old = ".upper()"
        new = ".lower()"

        [[variant]]
        name = "harmless"
        step = "change-1"
        expect = "vulnerable"
        weakness = "path-traversal"

        [[variant.replace]]
        file = "main.py"
        old = "sys.exit(2)"
        new = "sys.exit(2)  # nothing changes"
    ''')
    return root


def unseparated(function) -> dict:
    """How a test asks for judging without separation, which only the project's own programs may get.
    A judge that does not know the request yet is called without it."""
    import inspect
    return {"unseparated": True} if "unseparated" in inspect.signature(function).parameters else {}
