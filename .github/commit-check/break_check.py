"""Breaks the check by machine and runs its tests against every break.

The rule, fixed before any run: in check.py and fork_paths.py
- every statement that refuses or reports is taken out (a raise, a return of a refusal or of the exit codes 1 and 2,
  an addition to a list of failures), and so is every call made as a statement, whatever it runs;
- every condition that decides is turned round (the test of an if, an elif, a while, a conditional expression,
  an assert and a comprehension's condition, and the value of a return that is a comparison or a boolean expression);
- every operand of an "or" is made False and every operand of an "and" is made True, one at a time;
- every element of a list, a set or a tuple at the module's level, and of a literal that a loop or a comprehension runs
  over (the waits between the tries among them), is taken out, one at a time.
A break is caught only when a test fails on an assertion. A break ends in one of three classes: caught; not settled
(the tests met their time limit twice, or ended in an error with no test failing on an assertion, or did not load);
not caught (every test passed). A break that does not settle is listed; one that is not caught fails the run.

It runs in the automatic check on a side branch, never on the project's computer. It first runs the tests once with
nothing broken and stops when they do not all pass or when no test runs.

    python3 -I .github/commit-check/break_check.py --shard 1/4 --workers 4 --report breaks.json
"""

import argparse
import ast
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGETS = ("check.py", "fork_paths.py")
TIME_LIMIT = 240
WORKFLOWS = HERE.parent / "workflows"


def describe(node):
    return f"line {node.lineno}"


class Breaks(ast.NodeVisitor):
    """Collects every place to break, as (kind, a path of indexes to the node, a description)."""

    def __init__(self):
        self.found = []
        self.path = []

    def generic_visit(self, node):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        self.path.append((field, index))
                        self.visit(item)
                        self.path.pop()
            elif isinstance(value, ast.AST):
                self.path.append((field, None))
                self.visit(value)
                self.path.pop()

    def add(self, kind, node, extra=None):
        self.found.append({"kind": kind, "where": list(self.path), "line": node.lineno, "column": node.col_offset,
                           "extra": extra})

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call):
            self.add("take out", node)  # every call made for what it does: refusing, reporting, or running a part
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if isinstance(node.op, ast.Add):
            self.add("take out", node)
        self.generic_visit(node)

    def visit_Raise(self, node):
        self.add("take out", node)
        self.generic_visit(node)

    def visit_Return(self, node):
        value = node.value
        if isinstance(value, ast.Constant) and value.value in (1, 2, False):
            self.add("take out", node)
        elif isinstance(value, (ast.List, ast.Dict)) and (getattr(value, "elts", None) or getattr(value, "keys", None)):
            self.add("take out", node)
        elif isinstance(value, ast.Call) and getattr(value.func, "id", None) == "report":
            self.add("take out", node)
        elif isinstance(value, (ast.Compare, ast.BoolOp, ast.UnaryOp)) or (
                isinstance(value, ast.Call) and getattr(value.func, "id", None) in ("bool", "any", "all")):
            self.add("turn round", node)
        self.generic_visit(node)

    def visit_If(self, node):
        self.add("turn round", node)
        self.generic_visit(node)

    visit_While = visit_If
    visit_IfExp = visit_If
    visit_Assert = visit_If

    def visit_BoolOp(self, node):
        for index in range(len(node.values)):
            self.add("operand", node, index)
        self.generic_visit(node)

    def collection(self, node, attribute):
        value = getattr(node, attribute)
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)) and value.elts:
            for index in range(len(value.elts)):
                self.found.append({"kind": "element out", "where": list(self.path), "line": value.elts[index].lineno,
                                   "column": value.elts[index].col_offset, "extra": [attribute, index]})

    def visit_Assign(self, node):
        if len(self.path) == 1:  # a list, a set or a tuple at the module's level, which a rule or git reads
            self.collection(node, "value")
        self.generic_visit(node)

    def visit_For(self, node):
        self.collection(node, "iter")  # a literal a loop runs over, such as the waits between tries
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.collection(node, "iter")
        for index in range(len(node.ifs)):
            self.found.append({"kind": "turn round comprehension", "where": list(self.path), "line": node.ifs[index].lineno,
                               "column": node.ifs[index].col_offset, "extra": index})
        self.generic_visit(node)


def locate(tree, where):
    node = tree
    for field, index in where:
        value = getattr(node, field)
        node = value[index] if index is not None else value
    return node


def parent_slot(tree, where):
    node = tree
    for field, index in where[:-1]:
        value = getattr(node, field)
        node = value[index] if index is not None else value
    return node, where[-1]


def span(source, node):
    """The byte offsets of a node in the source (the syntax tree counts columns in bytes of UTF-8)."""
    data = source.encode("utf-8")
    starts = [0]
    for line in data.split(b"\n")[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset


def apply(source, tree, brk):
    """The source with one break, written into its text at the place of the node, so that the rest of the file,
    the parts its rules are written in among them, stays as it is."""
    node = locate(tree, brk["where"])
    kind = brk["kind"]
    data = source.encode("utf-8")

    def segment(part):
        start, end = span(source, part)
        return start, end, data[start:end].decode("utf-8")

    if kind == "take out":
        start, end, _ = segment(node)
        text = "pass"
        if isinstance(node, ast.Return):
            value = node.value
            if isinstance(value, ast.Constant):
                text = "return 0" if value.value in (1, 2) else "return True"
            elif isinstance(value, ast.List):
                text = "return []"
            elif isinstance(value, ast.Dict):
                text = "return {}"
            else:
                text = "return 0"
    elif kind == "turn round":
        part = node.value if isinstance(node, ast.Return) else node.test
        start, end, old = segment(part)
        text = f"not ({old})"
    elif kind == "turn round comprehension":
        start, end, old = segment(node.ifs[brk["extra"]])
        text = f"not ({old})"
    elif kind == "operand":
        start, end, _ = segment(node.values[brk["extra"]])
        text = "True" if isinstance(node.op, ast.And) else "False"
    elif kind == "element out":
        attribute, index = brk["extra"]
        collection = getattr(node, attribute)
        elements = collection.elts
        if len(elements) == 1:
            start, end, _ = segment(collection)
            text = {"Tuple": "()", "List": "[]", "Set": "set()"}[type(collection).__name__]
        elif index < len(elements) - 1:
            start, _ = span(source, elements[index])
            end, _ = span(source, elements[index + 1])
            text = ""
        else:
            _, start = span(source, elements[index - 1])
            _, end = span(source, elements[index])
            text = ""
    else:
        raise ValueError(kind)
    return (data[:start] + text.encode("utf-8") + data[end:]).decode("utf-8")


def run_tests(folder, limit, failfast=True):
    """Runs the check's tests in a copy; returns (class, number of tests run, names that failed)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "COMMIT_CHECK_", "FORK_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        done = subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", *(["-f"] if failfast else []),
                               "-s", str(folder), "-p", "test_check.py"], capture_output=True, text=True, timeout=limit, env=env,
                              cwd=folder)
    except subprocess.TimeoutExpired:
        return "not settled", 0, []
    out = done.stdout + done.stderr
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    count = int(ran.group(1)) if ran else 0
    failed = re.findall(r"^FAIL: (\S+)", out, re.M)
    errors = re.findall(r"^ERROR: (\S+)", out, re.M)
    if done.returncode == 0 and count > 0:
        return "not caught", count, []
    if failed:
        return "caught", count, failed  # a test failed on an assertion
    if errors and failfast:
        return run_tests(folder, limit, failfast=False)  # an error stopped the run first: look for an assertion
    return "not settled", count, errors  # an error, or nothing ran: no test failed on an assertion


def prepare(base, sources, target, broken, workflows):
    """A copy of the check's folder with one file broken, next to a copy of the desk's tool it imports."""
    folder = Path(tempfile.mkdtemp(dir=base))
    check = folder / ".github" / "commit-check"
    check.mkdir(parents=True)
    for name, text in sources.items():
        (check / name).write_text(text)
    desk = folder / "tools" / "desk"
    desk.mkdir(parents=True)
    shutil.copy(HERE.parents[1] / "tools" / "desk" / "pr_files.py", desk / "pr_files.py")
    for name in ("bench/tests", "bench/tasks"):
        source = HERE.parents[1] / name
        (folder / name).mkdir(parents=True)
        for entry in source.iterdir():
            (folder / name / entry.name).mkdir() if entry.is_dir() else (folder / name / entry.name).touch()
    workflow_folder = workflows
    workflows = folder / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for name in ("pull-requests.yml", "commits.yml"):
        shutil.copy(Path(workflow_folder) / name, workflows / name)
    if broken is not None:
        (check / target).write_text(broken)
    return check


def one(job):
    base, sources, target, brk, tree, workflow_folder = job
    broken = apply(sources[target], tree, brk)
    try:
        compile(broken, target, "exec")
    except SyntaxError:
        return {"file": target, "line": brk["line"], "column": brk["column"], "kind": brk["kind"], "part": brk["extra"],
                "result": "caught at loading", "tests": 0, "first failed": [], "seconds": 0}
    folder = prepare(base, sources, target, broken, workflow_folder)
    started = time.monotonic()
    result, count, failed = run_tests(folder, TIME_LIMIT)
    if result == "not settled":
        result, count, failed = run_tests(folder, TIME_LIMIT * 2)
    shutil.rmtree(folder.parents[1], ignore_errors=True)
    return {"file": target, "line": brk["line"], "column": brk["column"], "kind": brk["kind"], "part": brk["extra"],
            "result": result,
            "tests": count, "first failed": failed[:1], "seconds": round(time.monotonic() - started, 1)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", default="1/1")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--report", required=True)
    parser.add_argument("--workflows", help="the folder that holds the two workflows of the check, where it is not "
                                             ".github/workflows (on the side branch, which runs no other workflow)")
    args = parser.parse_args(argv)
    workflow_folder = str(Path(args.workflows) if args.workflows else WORKFLOWS)
    part, parts = (int(x) for x in args.shard.split("/"))
    sources = {path.name: path.read_text() for path in HERE.iterdir() if path.suffix in (".py", ".json", ".md")}
    jobs = []
    base = Path(tempfile.mkdtemp(prefix="breaks-"))
    for target in TARGETS:
        tree = ast.parse(sources[target])
        collector = Breaks()
        collector.visit(tree)
        jobs += [(base, sources, target, brk, tree, workflow_folder) for brk in collector.found]
    total = len(jobs)
    mine = jobs[part - 1::parts]
    # Nothing broken first: every test passes, and there are tests.
    result, count, failed = run_tests(prepare(base, sources, TARGETS[0], None, workflow_folder), TIME_LIMIT)
    if result != "not caught" or count == 0:
        print(f"::error::with nothing broken the tests did not all pass ({result}, {count} tests)")
        return 1
    print(f"{count} tests pass with nothing broken; {total} breaks in all, {len(mine)} in this share")
    started = time.monotonic()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, mine))
    shutil.rmtree(base, ignore_errors=True)
    Path(args.report).write_text(json.dumps({"share": args.shard, "breaks in all": total, "tests": count,
                                             "seconds": round(time.monotonic() - started), "results": results},
                                            indent=1))
    counts = {}
    for entry in results:
        counts[entry["result"]] = counts.get(entry["result"], 0) + 1
    print(json.dumps(counts))
    for entry in results:
        if entry["result"] in ("not caught", "not settled"):
            print(f"{entry['result']}: {entry['file']} line {entry['line']} column {entry['column']}, {entry['kind']}"
                  + (f", part {entry['part']}" if entry["part"] is not None else ""))
    # A break that no test catches fails the run; one that does not settle (an error, a time limit) is listed.
    return 1 if any(entry["result"] == "not caught" for entry in results) else 0


if __name__ == "__main__":
    sys.exit(main())
