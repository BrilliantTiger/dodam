"""The format checker reads the Python of a command-line task's tests as Python reads it.

A hidden test of a command-line task runs the program only through `case` of `dodam_cli`, learns
nothing about the language or the judge's settings, and is one case: one test function that pytest
collects once. Every Python file in the task's tests folder, helpers as well, is held to the same
rules. A word in a comment or a string is no code and is not refused; the same word as code is.
"""
import math
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CANARY, write
from dodambench.judge import step_terms
from dodambench.task import cli_problems, judging_seconds, load_task

BENCH = Path(__file__).resolve().parents[1]
SAMPLE = BENCH / "tasks" / "cli-csv-json"
TEST = "tests/functional/test_upper.py"


def summed(tests_per_file: list[int], case_seconds: int) -> int:
    return math.ceil(sum(step_terms(tests_per_file, case_seconds).values()))


def problems(root: Path) -> list[dict]:
    return cli_problems(load_task(root))


def refused_in(root: Path, rel: str) -> list[dict]:
    return [p for p in problems(root) if p["file"] == rel]


def with_added(root: Path, rel: str, code: str) -> list[dict]:
    """The problems of the task once `code` is added at the end of one of its files."""
    path = root / rel
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n\n" + code, encoding="utf-8")
    try:
        return refused_in(root, rel)
    finally:
        path.write_text(original, encoding="utf-8")


# Ways a test could start a process itself or reach the machine around it. None of these runs the
# program through the case runner.
STARTS_SOMETHING = {
    "the operating system module": "import os\n",
    "a name of the operating system module": "from os import path\n",
    "the platform module": "import platform\n",
    "loading modules by name": "import importlib\n",
    "calling foreign code": "import ctypes\n",
    "processes of Python": "import multiprocessing\n",
    "asynchronous work": "import asyncio\n",
    "pools of workers": "from concurrent import futures\n",
    "running a module": "import runpy\n",
    "signals": "import signal\n",
    "evaluating text": "X = eval('1')\n",
    "executing text": "exec('X = 1')\n",
    "compiling text": "X = compile('1', 'x', 'eval')\n",
    "importing by a call": "X = __import__('json')\n",
    "an attribute by name": "X = getattr(case, 'run')\n",
    "setting an attribute by name": "setattr(case, 'x', 1)\n",
    "the module's names": "X = globals()\n",
    "the names of a scope": "X = vars()\n",
    "the debugger": "breakpoint()\n",
    "opening a file": "X = open('/tests/manifest.toml').read()\n",
    "a name the file does not define": "X = os.getpid()\n",
    "a name the file does not define, alone": "X = somewhere\n",
    "a relative import": "from . import helper\n",
    "a module reached through an allowed module": "import json\n\nX = json.codecs\n",
    "a built-in name bound by a parameter": "def f(open=None):\n    return open\n",
    "a built-in name bound in a class": "class K:\n    getattr = None\n",
    "a built-in name bound by a loop": "for eval in ():\n    pass\n",
    "a built-in name bound where it never runs": "if False:\n    exec = None\n",
    "storing an attribute": "case.x = 1\n",
    "a module imported from an allowed module": "from json import codecs\n",
    "an allowed built-in name bound by a parameter": "def f(str=None):\n    return str\n",
    "a built-in name bound by a def": "def str(value):\n    return value\n",
    "a built-in name bound by a class": "class open:\n    pass\n",
    "a built-in name bound by an exception caught": "try:\n    pass\nexcept ValueError as open:\n    pass\n",
    "a built-in name bound by a pattern": "match 1:\n    case open:\n        pass\n",
    "a built-in name made global": "def f():\n    global open\n",
    "a built-in name bound by importing a module": "import json as open\n",
    "a built-in name bound by importing a name": "from json import loads as open\n",
    "storing an attribute on the list": "import json\n\njson.dumps = None\n",
}

# Ways a test could learn which language it judges, or anything else of the judge's settings.
LEARNS_SOMETHING = {
    "the case runner's settings": "from dodam_cli import config\n",
    "the case runner as a module": "import dodam_cli\n",
    "a name of the case runner under another name": "from dodam_cli import case as start\n",
    "a hidden attribute": "X = case._config\n",
    "a function's globals": "X = need.__globals__\n",
    "the wrapped function": "X = case.__wrapped__\n",
    "the built-in names": "X = __builtins__\n",
    "the module's loader": "X = __loader__\n",
    "reading a file through a path the case gives": (
        "def look():\n    with case() as c:\n        return c.write('a', 'b').parent.read_text()\n"),
    "listing a folder through a path the case gives": (
        "def look():\n    with case() as c:\n        return list(c.mkdir('a').iterdir())\n"),
    "asking whether a file is there through a path": (
        "def look():\n    with case() as c:\n        return c.write('a', 'b').exists()\n"),
    "a frame of a running generator": "def gen():\n    yield\n\n\nX = gen().gi_frame\n",
    "the frame of a traceback": "def look(error):\n    return error.tb_frame\n",
    "attributes through a format string": "X = '{0.__class__}'.format(case)\n",
    "the system module beyond the search path": "import sys\n\nX = sys.modules\n",
    "the system module beside the line for the helper folder": (
        "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"
        "X = str(sys)\n"),
    "a name with two underscores bound": "__all__ = []\n",
    "a path the case gives, kept in a name": (
        "def look():\n    with case() as c:\n        p = c.outside / 'x'\n        return str(p)\n"),
    "a path a method of the case returns, used": (
        "def look():\n    with case() as c:\n        return c.write('a', 'b').name\n"),
    "a method of the case that gives a path, kept in a name": (
        "def look():\n    with case() as c:\n        w = c.write\n        return w\n"),
    "a method of the case that gives a path, in a list": (
        "def look():\n    with case() as c:\n        [c.mkdir][0]('d')\n"),
    "a method of the case that gives a path, passed to a call": (
        "def look():\n    with case() as c:\n        box = []\n        box.append(c.write)\n"),
    "a method of the case that gives a path, from a function": (
        "def look():\n    with case() as c:\n        (lambda: c.sparse)()('y', 1)\n"),
    "a way of a path that copies a file": "def look(q):\n    return q.copy\n",
    "a way of a path that asks about a file": "def look(q):\n    return q.group\n",
    "a path a method of the case returns, made a string": (
        "def look():\n    with case() as c:\n        return str(c.write('a', 'b'))\n"),
    "a path the case gives, asked about": (
        "def look():\n    with case() as c:\n        return sorted(c.work.parts)\n"),
    "a path beside the line for the helper folder": (
        "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"
        "X = str(Path('x'))\n"),
    "a path module beyond the search path": "from pathlib import Path\n\nX = Path('/tests').iterdir()\n",
}

# Ways one test function could become several cases, or cases could be collected that the checker
# does not count.
MULTIPLIES = {
    "the test framework itself": "import pytest\n",
    "a class of tests": ("class TestMore:\n    def test_a(self):\n        with case() as c:\n"
                         "            c.run([])\n"),
    "an asynchronous test": "async def test_later():\n    with case() as c:\n        c.run([])\n",
    "a test that asks for a fixture": "def test_with(request):\n    with case() as c:\n        c.run([])\n",
    "a decorated test": ("def wrap(f):\n    return f\n\n\n@wrap\ndef test_wrapped():\n    with case() as c:\n"
                         "        c.run([])\n"),
    "a hook that makes tests": "def pytest_generate_tests(metafunc):\n    metafunc.parametrize('x', [1, 2])\n",
    "plug-ins named by the file": "pytest_plugins = ['x']\n",
    "a hook of pytest by its name": "def pytest_collection_modifyitems(items):\n    items.reverse()\n",
    "a class pytest would collect, even an empty one": "class TestNothing:\n    pass\n",
    "a test name bound by a pattern": "match test_upper:\n    case test_alias:\n        pass\n",
    "a class named like a test": "class test_more:\n    pass\n",
    "a class named like a test inside a function": "def make():\n    class TestInner:\n        pass\n    return TestInner\n",
    "a test defined inside a function": "def outer():\n    def test_inner():\n        pass\n    return test_inner\n",
    "a test that yields": "def test_yields():\n    with case() as c:\n        c.run([])\n    yield\n",
    "a mark for the whole file": "pytestmark = 1\n",
    "a test stored on a class": "class Holder:\n    pass\n\n\nHolder.test_m = test_upper\n",
    "a class bound to a name pytest collects": "class Holder:\n    pass\n\n\nTestHolder = Holder\n",
    "a test bound twice": "test_again = test_upper\n",
    "a test name bound by a loop": "for test_each in range(2):\n    pass\n",
    "a test name bound by an import": "from json import loads as test_load\n",
    "every name of a module": "from json import *\n",
    "a test defined twice": "def test_upper():\n    with case() as c:\n        c.run([])\n",
    "a test defined only sometimes": ("if True:\n    def test_maybe():\n        with case() as c:\n"
                                      "            c.run([])\n"),
    "a test taken away": "del test_upper\n",
    "a test name made global": "def bind():\n    global test_late\n    test_late = bind\n",
}


@pytest.mark.parametrize("way", list(STARTS_SOMETHING))
def test_a_test_that_could_start_something_itself_is_refused(cli_task_dir, way):
    assert problems(cli_task_dir) == []
    found = with_added(cli_task_dir, TEST, STARTS_SOMETHING[way])
    assert found, f"{way} was accepted in a test of a command-line task"
    assert all(p["key"] and p["message"] for p in found), found


@pytest.mark.parametrize("way", list(LEARNS_SOMETHING))
def test_a_test_that_could_learn_the_language_or_the_judges_settings_is_refused(cli_task_dir, way):
    found = with_added(cli_task_dir, TEST, LEARNS_SOMETHING[way])
    assert found, f"{way} was accepted in a test of a command-line task"


@pytest.mark.parametrize("way", list(MULTIPLIES))
def test_a_test_that_could_become_several_cases_is_refused(cli_task_dir, way):
    found = with_added(cli_task_dir, TEST, MULTIPLIES[way])
    assert found, f"{way} was accepted in a test of a command-line task"


def test_a_word_in_a_comment_or_a_string_is_no_code(cli_task_dir):
    """Pinned both ways: the words of the refused ways in a comment and in a string are accepted, and
    the same words as code are refused (the tests above)."""
    words = "subprocess os.system os.fork pty lang.env DODAM_CLI_CONFIG parametrize conftest importlib ctypes"
    code = (f"# {words}\n"
            f"NOTE = '{words}'\n"
            "TEXT = '''import subprocess\nimport pytest\n@pytest.mark.parametrize('x', [1])\n'''\n"
            "def test_words_are_input():\n"
            "    with case(files={'note.txt': NOTE}) as c:\n"
            "        r = c.run(['upper'], stdin=TEXT)\n"
            "        assert r.exit_code == 0, r\n")
    assert with_added(cli_task_dir, TEST, code) == []


def test_what_a_test_may_use_is_accepted(cli_task_dir):
    """The modules of the standard library on the checker's list, a helper of the task's own, local
    names that begin with test, and the one line that puts the task's helper folder on the search
    path. A class with attributes of its own is not on the lists: its attributes are stored and read
    by names the lists do not hold."""
    write(cli_task_dir, "tests/lib/expected.py", "import json\n\n\ndef upper(text):\n    return text.upper()\n")
    code = ("import sys\nfrom pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"
            "import collections, json, math, re  # noqa: E401\n"
            "from expected import upper  # noqa: E402\n\n\n"
            "def test_expected_text():\n"
            "    test_input = 'abc'\n"
            "    with case() as c:\n"
            "        r = c.run(['upper'], stdin=test_input)\n"
            "        assert r.out == upper(test_input) == 'ABC', r\n"
            "        assert json.loads('[1]') == [1] and math.floor(1.5) == 1 and re.match('A', r.out)\n"
            "        assert collections.Counter(r.out)['A'] == 1\n")
    assert with_added(cli_task_dir, TEST, code) == []
    assert problems(SAMPLE) == []


def test_every_python_file_of_the_tests_is_held_to_the_same_rules(cli_task_dir):
    """A helper is held to the rules of a test, and so is a file Python runs on its own when a
    test is collected."""
    write(cli_task_dir, "tests/lib/helper.py", "import os\n\n\ndef where():\n    return os.getcwd()\n")
    assert refused_in(cli_task_dir, "tests/lib/helper.py")
    (cli_task_dir / "tests/lib/helper.py").write_text("def where():\n    return 'here'\n", encoding="utf-8")
    assert problems(cli_task_dir) == []
    write(cli_task_dir, "tests/security/__init__.py", "import subprocess\n")
    assert refused_in(cli_task_dir, "tests/security/__init__.py")


@pytest.mark.parametrize("rel", ["tests/conftest.py", "tests/functional/conftest.py", "tests/pytest.ini",
                                 "tests/functional/pytest.ini", "tests/pyproject.toml", "tests/tox.ini",
                                 "tests/setup.cfg", "tests/lib/fast.so", "tests/functional/__pycache__/x.pyc",
                                 "tests/lib/sitecustomize.py", "tests/lib/json.py", "tests/functional/dodam_cli.py",
                                 "tests/lib/anyio.py", "tests/lib/httpx.py", "tests/lib/pluggy.py",
                                 "tests/lib/annotationlib.py", "tests/lib/compression.py", "tests/lib/telnetlib.py"])
def test_a_file_pytest_or_python_would_run_or_obey_on_its_own_is_refused(cli_task_dir, rel):
    """pytest runs a conftest.py and obeys its settings files without a test asking for them, and
    Python loads compiled files and files named like its own modules in their place."""
    write(cli_task_dir, rel, "X = 1\n")
    assert [p for p in problems(cli_task_dir) if p["file"] == rel], f"{rel} was accepted"


def test_each_file_of_tests_calls_the_case_helper_itself(cli_task_dir):
    """Each file the manifest lists imports `case` from dodam_cli and calls it. A comment naming
    the case runner is no use of it, and neither is a helper that uses it."""
    head = f"# {CANARY}\n"
    write(cli_task_dir, "tests/boundary/test_usage.py", head + "# dodam_cli\n\n\ndef test_nothing():\n    assert True\n")
    assert refused_in(cli_task_dir, "tests/boundary/test_usage.py")
    write(cli_task_dir, "tests/boundary/test_usage.py",
          head + "from dodam_cli import case\n\n\ndef test_nothing():\n    assert case\n")
    assert refused_in(cli_task_dir, "tests/boundary/test_usage.py")
    write(cli_task_dir, "tests/lib/runs.py",
          "from dodam_cli import case\n\n\ndef exit_code(args):\n    with case() as c:\n        return c.run(args).exit_code\n")
    write(cli_task_dir, "tests/boundary/test_usage.py",
          head + "import sys\nfrom pathlib import Path\n\n"
          "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"
          "from runs import exit_code  # noqa: E402\n\n\ndef test_no_arguments():\n    assert exit_code([]) == 2\n")
    assert refused_in(cli_task_dir, "tests/boundary/test_usage.py")
    write(cli_task_dir, "tests/boundary/test_usage.py",
          head + "from dodam_cli import case\n\n\ndef test_no_arguments():\n    with case() as c:\n"
          "        assert c.run([]).exit_code == 2\n")
    assert problems(cli_task_dir) == []


def collected(test_file: Path) -> int:
    """How many cases pytest collects from a file of tests, as the case runner would."""
    here = test_file.parent
    lib = test_file.parents[1] / "lib"
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           str(test_file)], capture_output=True, text=True, cwd=here,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{BENCH / 'dodambench'}:{lib}",
                               "PYTHONDONTWRITEBYTECODE": "1"})
    return sum(1 for line in done.stdout.splitlines() if "::" in line)


def test_the_checker_counts_what_pytest_collects(cli_task_dir):
    """The sum of time counts the tests of a file as pytest collects them: a test whose name begins
    with `test` without an underscore is a test too."""
    path = cli_task_dir / TEST
    path.write_text(path.read_text(encoding="utf-8") + (
        "\n\ndef testlower():\n    with case() as c:\n        assert c.run(['upper'], stdin='a').out == 'A'\n"),
        encoding="utf-8")
    assert collected(path) == 2
    task = load_task(cli_task_dir)
    counts = {"build": [collected(cli_task_dir / "tests" / t.file) for t in task.tests_for("build")],
              "change-1": [collected(cli_task_dir / "tests" / t.file) for t in task.tests_for("change-1")]}
    whole = max(summed(counts[step], 2) for step in counts)
    assert judging_seconds(task) == whole


def test_the_sample_counts_what_pytest_collects():
    task = load_task(SAMPLE)
    for t in task.tests:
        path = SAMPLE / "tests" / t.file
        assert collected(path) >= 1, t.file
    counts = [collected(SAMPLE / "tests" / t.file) for t in task.tests_for("change-1")]
    assert sum(counts) == 50
    assert judging_seconds(task) == summed(counts, task.cli["case_timeout_sec"])


PATH_LINE = "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"


def test_a_helper_is_a_file_of_the_helper_folder_only(cli_task_dir):
    """A test imports a helper from the task's helper folder and nowhere else: not a Python file in
    another folder of the tests, not a file of tests, and the line for the search path names the
    helper folder only."""
    write(cli_task_dir, "tests/data/extra.py", "def value():\n    return 1\n")
    head = "import sys\nfrom pathlib import Path\n\n" + PATH_LINE
    assert with_added(cli_task_dir, TEST, head + "import extra  # noqa: E402\n")
    assert with_added(cli_task_dir, TEST, head + "import test_usage as other  # noqa: E402\n")
    for folder in ("'data'", "'/opt'", "'lib.zip'"):
        line = PATH_LINE.replace("'lib'", folder)
        assert with_added(cli_task_dir, TEST, "import sys\nfrom pathlib import Path\n\n" + line), folder
    for deep in ("parents[0]", "parents[2]"):
        line = PATH_LINE.replace("parents[1]", deep)
        assert with_added(cli_task_dir, TEST, "import sys\nfrom pathlib import Path\n\n" + line), deep
    write(cli_task_dir, "tests/lib/expected.py", "def value():\n    return 1\n\n\n_hidden = 1\n")
    assert with_added(cli_task_dir, TEST, head + "from expected import value  # noqa: E402\n") == []
    assert with_added(cli_task_dir, TEST, head + "from expected import _hidden  # noqa: E402\n")


def test_a_test_imports_from_a_helper_only_what_the_helper_defines(cli_task_dir):
    """A helper's own line for the search path imports sys and Path; a test does not get them, nor
    anything else the helper imported, through the helper."""
    write(cli_task_dir, "tests/lib/paths.py", "import json\nimport sys\nfrom pathlib import Path\n\n"
          + PATH_LINE + "VALUE = json.dumps(1)\n")
    assert refused_in(cli_task_dir, "tests/lib/paths.py") == []
    head = "import sys\nfrom pathlib import Path\n\n" + PATH_LINE
    for name in ("Path", "sys", "json"):
        assert with_added(cli_task_dir, TEST, head + f"from paths import {name} as other  # noqa: E402\n"), name
    assert with_added(cli_task_dir, TEST, head + "from paths import VALUE  # noqa: E402\n") == []
    # an annotation without a value binds nothing, and a name the helper imported stays the imported one
    for line in ("Path: int\n", "sys: int\n", "Path: int = 1\n", "json = 1\n"):
        name = line.split(":")[0].split("=")[0].strip()
        write(cli_task_dir, "tests/lib/paths.py", "import json\nimport sys\nfrom pathlib import Path\n\n"
              + PATH_LINE + "VALUE = json.dumps(1)\n" + line)
        assert with_added(cli_task_dir, TEST, head + f"from paths import {name} as other  # noqa: E402\n"), line
    write(cli_task_dir, "tests/lib/paths.py", "import json\nimport sys\nfrom pathlib import Path\n\n"
          + PATH_LINE + "VALUE = json.dumps(1)\nLATER: int\n")
    assert with_added(cli_task_dir, TEST, head + "from paths import LATER  # noqa: E402\n")


def test_a_link_in_the_tests_folder_is_refused(cli_task_dir, tmp_path):
    """The checker reads what the converter copies: a link in the tests folder would bring in files the
    checker never read."""
    import os
    outside = tmp_path / "extra"
    write(outside, "loose.py", "X = 1\n")
    os.symlink(outside, cli_task_dir / "tests/more")
    assert [p for p in problems(cli_task_dir) if p["file"].startswith("tests/more")]
    (cli_task_dir / "tests/more").unlink()
    os.symlink(outside / "loose.py", cli_task_dir / "tests/lib-loose.py")
    assert [p for p in problems(cli_task_dir) if p["file"].startswith("tests/lib-loose.py")]


DANGEROUS = {eval, exec, compile, open, getattr, setattr, delattr, globals, locals, vars, __import__, breakpoint, input}


def accepted_lines(root: Path, head: str, lines: list[str]) -> list[bool]:
    """Which of these lines the checker accepts, each added after `head` at the end of a test file."""
    path = root / TEST
    original = path.read_text(encoding="utf-8")
    before = original + "\n\n" + head
    path.write_text(before + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    try:
        said = {p["key"] for p in cli_problems(load_task(root)) if p["file"] == TEST}
    finally:
        path.write_text(original, encoding="utf-8")
    first = before.count("\n") + 1
    return [f"line {first + i}" not in said for i in range(len(lines))]


def test_from_what_a_test_may_import_and_reach_no_module_or_dangerous_function_is_reached(cli_task_dir):
    """The modules a test may import and the attributes it may reach are lists. Asked of every module
    of the standard library, and of every attribute of what is reached to a depth of three, the
    checker admits no chain that leads to a module or to a built-in function a test may not use."""
    import importlib
    import sys as system
    names = sorted(n for n in system.stdlib_module_names if not n.startswith("_") and n.isidentifier())
    allowed = [n for n, ok in zip(names, accepted_lines(cli_task_dir, "", [f"import {n}" for n in names])) if ok]
    assert allowed, "no module of the standard library may be imported at all"
    head = "".join(f"import {n}\n" for n in allowed)
    level = [(n, importlib.import_module(n)) for n in allowed]
    reached = []
    for _ in range(3):
        candidates = [(f"{expr}.{a}", getattr(obj, a)) for expr, obj in level for a in dir(obj)
                      if a.isidentifier() and hasattr(obj, a)]
        if not candidates:
            break
        flags = accepted_lines(cli_task_dir, head, [f"X = {expr}" for expr, _ in candidates])
        level = [pair for pair, ok in zip(candidates, flags) if ok]
        reached += level
    bad = [expr for expr, obj in reached if type(obj).__name__ == "module" or any(obj is d for d in DANGEROUS)]
    assert not bad, bad[:20]
