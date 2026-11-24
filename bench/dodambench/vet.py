"""Vet a source task: judge every prepared solution and check it gets the judgement it was written for.

The secure solution must pass every test. A vulnerable variant must pass every functional and boundary
test and fail the security tests of its own weakness, and only those. A broken variant must fail a
functional or boundary test. Tests that cannot tell these apart would let weak programs through, so a
task that fails vetting is not ready. The judge is the one the verifier runs. By default the language
tools come from this machine, with this Python environment's own tools first on PATH; with an image,
each solution is judged inside the language image exactly as the verifier does it: no network, the
judge as root with its own Python, the program built and run as the plain user agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import stat
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from .convert import to_harbor, verifier_env
from .judge import (LEFT_BEHIND, OUTCOMES, UNMEASURED, UNMEASURED_REASONS, JudgeFault, judge, names_of_tests,
                    reward_of)
from .languages import LANGUAGES, Language
from .task import (PROCESS_CAP, Task, TaskError, TestFile, cases_mount, container_memory_mb, load_task,
                   materialize, task_problems)

FEATURE_KINDS = ("functional", "boundary")
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
BENCH = Path(__file__).resolve().parents[1]
SCAFFOLDS = BENCH / "images"
SCAFFOLD_CACHE = BENCH / ".cache" / "scaffolds"
READY = ".ready"


class VetError(Exception):
    """The task cannot be vetted. It holds every reason as a problem: its file, its key and its message.
    As text it is the first of them as JSON."""

    def __init__(self, problems: list[dict] | dict | str):
        if isinstance(problems, str):
            problems = json.loads(problems) if problems.startswith("{") else {"file": "task", "key": "",
                                                                                "message": problems}
        self.problems = [{"severity": "error", **p} for p in ([problems] if isinstance(problems, dict) else problems)]
        super().__init__(json.dumps(self.problems[0]))


# --- what a judging left, as the one record the judging code reads ---

REWARD_KEYS = tuple(reward_of([], True, "", True))  # the parts of the judge's reward
# The most a file a judging left may hold. The largest of 70 results of earlier judgings held 17,130
# bytes; a case's message is at most 300 characters and the build's log 2,000.
READ_LIMIT = 2 ** 20
FAULT_FILE, REWARD_FILE, DETAILS_FILE = "judge-fault.json", "reward.json", "details.json"


@dataclass(frozen=True)
class Case:
    """One case of a judging, as the judging code reads it."""
    name: str
    outcome: str              # one of OUTCOMES
    file: str                 # a file of tests the judge runs at the step judged
    kind: str                 # that file's kind, or the kind of what the program's side left behind
    step: str                 # that file's step, or the step judged for what was left behind
    weakness: str | None      # that file's weakness
    message: str


@dataclass(frozen=True)
class Judged:
    """What one judging left, as the record the judging code reads. It is made in one place,
    record_of, and holds exactly the parts the judging code reads."""
    step: str                 # the step judged
    build_ok: bool
    left_behind: str          # what the program's side left that the judge could not take back; empty when nothing
    cases: tuple[Case, ...]
    reward: dict              # the judge's reward, the one the cases give
    bounds: tuple[int, ...] = ()  # the most each case that passed or was not judged for measuring can have held,
    #                               where the judge worked it out


def _not_of_the_shape(file: str, key: str, what: str) -> JudgeFault:
    return JudgeFault(json.dumps({"severity": "error", "file": file, "key": key,
                                  "message": f"the judging's result is not of the shape the tool reads: {what}"}))


def _whole_picture(cases: list[dict], test: TestFile, names: list[str], build_ok: bool, left: str, cli: bool) -> bool:
    """Whether a file's cases are the whole picture the judge gives of it: a case for every test the
    file holds, each once (on a served task, a test's parametrized cases), or the one entry the judge
    writes for the whole file, which never passed. On a command-line task the judge writes that entry
    only where the build failed or the program's side left something, as a case that was not run."""
    whole = PurePosixPath(test.file).name
    if len(cases) == 1 and cases[0]["name"] == whole:
        if cli:
            return (not build_ok or bool(left)) and cases[0]["outcome"] == ("skipped" if test.kind == "security"
                                                                            else "failed")
        return cases[0]["outcome"] != "passed"
    given = [case["name"] for case in cases]
    if len(set(given)) != len(given):
        return False
    if cli:
        return sorted(given) == sorted(names)
    of_test = {name: [g for g in given if g == name or (g.startswith(f"{name}[") and g.endswith("]"))]
               for name in names}
    return all(of_test.values()) and sum(len(found) for found in of_test.values()) == len(given)


def record_of(reward: object, details: object, tests_dir: Path, run: Sequence[TestFile], step: str,
              separated: bool, cli: bool) -> Judged:
    """The record of a judging at `step`, from the judge's reward and details as JSON. `run` is what the
    judge runs at that step and `tests_dir` the task's own tests, from which the tests of each file
    are counted. Whatever does not fit is a fault of the judge: a part that is missing, of another kind,
    outside its set of values or naming another step; a case too few or too many; a reward other than
    the one the cases give."""
    if not isinstance(details, dict):
        raise _not_of_the_shape(DETAILS_FILE, "", "an object")
    if details.get("step") != step:
        raise _not_of_the_shape(DETAILS_FILE, "step", f"the step judged, {step}")
    if not isinstance(details.get("build_ok"), bool):
        raise _not_of_the_shape(DETAILS_FILE, "build_ok", "true or false")
    build_ok = details["build_ok"]
    left = details.get("left_behind", "")
    if "left_behind" in details and not (isinstance(left, str) and left):
        raise _not_of_the_shape(DETAILS_FILE, "left_behind", "a text that is not empty, where there is one")
    given = details.get("tests")
    if not isinstance(given, list):
        raise _not_of_the_shape(DETAILS_FILE, "tests", "a list")
    files = {t.file: t for t in run}
    cases: list[Case] = []
    of_file: dict[str, list[dict]] = {t.file: [] for t in run}
    left_entries = 0
    for i, case in enumerate(given):
        key = f"tests[{i}]"
        if not isinstance(case, dict):
            raise _not_of_the_shape(DETAILS_FILE, key, "an object")
        for name in ("name", "outcome", "file", "kind", "step", "message"):
            if not isinstance(case.get(name), str):
                raise _not_of_the_shape(DETAILS_FILE, f"{key}.{name}", "a text")
        if "weakness" not in case or not (case["weakness"] is None or isinstance(case["weakness"], str)):
            raise _not_of_the_shape(DETAILS_FILE, f"{key}.weakness", "a text or null")
        if not case["name"]:
            raise _not_of_the_shape(DETAILS_FILE, f"{key}.name", "a text that is not empty")
        if case["outcome"] not in OUTCOMES:
            raise _not_of_the_shape(DETAILS_FILE, f"{key}.outcome", f"one of {', '.join(OUTCOMES)}")
        if case["outcome"] == UNMEASURED and case["message"] not in UNMEASURED_REASONS:
            raise _not_of_the_shape(DETAILS_FILE, f"{key}.message", "the judge's own words for a case not judged "
                                                                    "for measuring")
        own = files.get(case["file"])
        if own is None:
            raise _not_of_the_shape(DETAILS_FILE, f"{key}.file", f"a file of tests the judge runs at step {step}")
        if case["kind"] == LEFT_BEHIND:
            # the judge writes this entry once, only where the result says what was left: failed with
            # that reason, named after its file, of the step judged and without a weakness
            if (not left or left_entries or case["outcome"] != "failed" or case["message"] != left
                    or case["name"] != PurePosixPath(case["file"]).name or case["step"] != step
                    or case["weakness"] is not None):
                raise _not_of_the_shape(DETAILS_FILE, key, "an entry for what was left behind only as the judge "
                                                           "writes it, once, where the result says what was left")
            left_entries += 1
        else:
            for name, value in (("kind", own.kind), ("step", own.step), ("weakness", own.weakness)):
                if case[name] != value:
                    raise _not_of_the_shape(DETAILS_FILE, f"{key}.{name}", f"{value} for {case['file']}")
            of_file[case["file"]].append(case)
        cases.append(Case(**{name: case[name] for name in ("name", "outcome", "file", "kind", "step", "weakness",
                                                           "message")}))
    for test in run:
        # the count comes from the task's own file of tests, not from what the judging left
        names = names_of_tests((tests_dir / test.file).read_bytes())
        if not _whole_picture(of_file[test.file], test, names, build_ok, left, cli):
            raise _not_of_the_shape(DETAILS_FILE, "tests", f"a case for every test of {test.file}, each once, or "
                                                           f"the one entry the judge writes for the whole file")
    unmeasured = [f"{c.file}::{c.name}" for c in cases if c.outcome == UNMEASURED]
    if details.get(UNMEASURED) != unmeasured:
        raise _not_of_the_shape(DETAILS_FILE, UNMEASURED, "the cases not judged for measuring, as file::name in the "
                                                          "order of the cases, and an empty list where there is none")
    expected = reward_of([{"outcome": c.outcome, "kind": c.kind, "step": c.step} for c in cases], build_ok, step,
                         separated)
    if not isinstance(reward, dict) or set(reward) != set(expected):
        raise _not_of_the_shape(REWARD_FILE, "", f"an object of exactly {', '.join(expected)}")
    for key, value in expected.items():
        if isinstance(reward[key], bool) or not isinstance(reward[key], (int, float)):
            raise _not_of_the_shape(REWARD_FILE, key, "a number")
        if reward[key] != value:
            raise _not_of_the_shape(REWARD_FILE, key, f"{value}, what the cases give")
    # the most each case that passed, or was not judged for measuring, can have held, where the judge
    # worked it out: read here, so that nothing else reads the result again
    bounds = tuple(case["memory_bound_bytes"] for case in given
                   if isinstance(case, dict) and case.get("outcome") in ("passed", UNMEASURED)
                   and isinstance(case.get("memory_bound_bytes"), int) and not isinstance(case["memory_bound_bytes"], bool))
    if unmeasured:
        # the judging is no judgement of the solution, whatever kind it was written as: a fault of measuring;
        # the room it left is told all the same, from the bounds read here
        fault = JudgeFault(json.dumps({"severity": "error", "file": DETAILS_FILE, "key": UNMEASURED,
                                       "message": "cases not judged for measuring: " + "; ".join(
                                           f"{c.file}::{c.name}, {c.message}" for c in cases if c.outcome == UNMEASURED)}))
        fault.bounds = bounds
        raise fault
    return Judged(step=step, build_ok=build_ok, left_behind=left, cases=tuple(cases), reward=dict(reward), bounds=bounds)


def _present(folder: int, name: str) -> bool:
    """Whether anything at all, a link as well, stands at `name` in the folder."""
    try:
        os.lstat(name, dir_fd=folder)
    except FileNotFoundError:
        return False
    return True


def _read_json(folder: int, name: str):
    """A file a judging left directly in the folder, as JSON; None when it is no plain file of one name
    of at most READ_LIMIT bytes, not UTF-8, not JSON, nested too deep for this Python to read, or
    holds a text that is no Unicode. A link is never followed and a pipe never waited on."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        return None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
            return None
        if found.st_size > READ_LIMIT:
            return None
        raw = b""
        while len(raw) <= found.st_size:
            more = os.read(fd, found.st_size + 1 - len(raw))
            if not more:
                break
            raw += more
        loaded = json.loads(raw.decode("utf-8"))
        # half of a pair of surrogates is no text: what holds one is no JSON the tool takes
        json.dumps(loaded, ensure_ascii=False).encode("utf-8")
        return loaded
    except (ValueError, RecursionError):
        return None
    finally:
        os.close(fd)


def read_judging(out_dir: Path, task: Task, step: str, ended: int | None = 0, timeout: float | None = None,
                 separated: bool = True) -> Judged:
    """The one place the tool reads what a judging left in out_dir, the folder it made empty for this
    judging: the judge's account of a fault, or its result turned into the record the judging code
    reads. It is total: for anything it finds it gives the record or a fault of the judge, never a
    result otherwise, and any error inside it is such a fault."""
    try:
        folder = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _present(folder, FAULT_FILE):
                said = _read_json(folder, FAULT_FILE)
                if isinstance(said, dict) and isinstance(said.get("judge_fault"), str):
                    raise JudgeFault(said["judge_fault"])
                raise JudgeFault(json.dumps({"severity": "error", "file": FAULT_FILE, "key": "",
                                             "message": "the judge's account of a fault has another shape"}))
            missing = not (_present(folder, REWARD_FILE) and _present(folder, DETAILS_FILE))
            if ended != 0 or missing:
                how = (f"the container did not end within {timeout:g} seconds" if ended is None
                       else f"the container ended with exit code {ended}" if ended != 0 else "the judging ended")
                said = how + (" and left no result" if missing else ", so what it left is no result")
                raise JudgeFault(json.dumps({"severity": "error", "file": "container", "key": "ended",
                                             "message": said}))
            return record_of(_read_json(folder, REWARD_FILE), _read_json(folder, DETAILS_FILE), task.root / "tests",
                             task.tests_for(step), step, separated, task.meta.get("interface") == "cli")
        finally:
            os.close(folder)
    except JudgeFault:
        raise
    except Exception as exc:
        raise JudgeFault(json.dumps({"severity": "error", "file": "result", "key": "",
                                     "message": f"the judging's result could not be read: {type(exc).__name__}"}))


def _files(cases: Sequence[Case]) -> str:
    return ", ".join(sorted({c.file for c in cases}))


def judgement_problems(expect: str, weakness: str | None, judged: Judged) -> list[str]:
    """Why a judged solution did not get the judgement it was written for; empty when it did. It reads
    the record of the judging and nothing else."""
    if not isinstance(judged, Judged):
        raise TypeError("the judging code reads only the record of a judging, which record_of makes")
    if not judged.build_ok:
        return ["the program did not build"]
    if judged.left_behind:
        # the step failed for what the program's side left, not for what the solution was written to show
        return [f"the program's side left what the judge cannot take back: {judged.left_behind}"]
    tests = judged.cases
    step = judged.step
    if not tests:
        # a step that judged no test shows nothing a solution was written for
        return [f"no test was judged at step {step}"]
    if any(t.message == "program did not start" for t in tests):
        return ["the program did not start"]
    failed = [t for t in tests if t.outcome == "failed"]
    features = [t for t in failed if t.kind in FEATURE_KINDS]
    unjudged = [t for t in tests if t.outcome == "skipped"]
    problems = []
    if unjudged and expect in ("secure", "vulnerable"):
        problems.append(f"tests left unjudged: {_files(unjudged)}")
    if expect == "secure":
        if failed:
            problems.append(f"the secure solution failed {_files(failed)}")
    elif expect == "vulnerable":
        security = [t for t in failed if t.kind == "security"]
        own = [t for t in security if t.weakness == weakness]
        others = [t for t in security if t.weakness != weakness]
        # the solution is written for its step: a test of its weakness at that step has to fail
        if not [t for t in own if t.step == step]:
            problems.append(f"no security test of {weakness} at step {step} failed")
        if others:
            kinds = ", ".join(sorted({str(t.weakness) for t in others}))
            problems.append(f"security tests of other weaknesses failed too: {kinds} in {_files(others)}")
        if features:
            problems.append(f"functional or boundary tests failed: {_files(features)}")
    elif expect == "broken":
        if not features:
            problems.append("no functional or boundary test failed")
    else:
        problems.append(f"unknown expectation {expect!r}")
    return problems


def scaffold_cache(lang: Language, cache_root: Path, scaffolds_root: Path = SCAFFOLDS) -> Path | None:
    """The language's starting /app with its dependencies installed, prepared once per content of the files."""
    src = scaffolds_root / lang.key / "app"
    if not src.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(src)).encode() + b"\0" + path.read_bytes() + b"\0")
    dest = cache_root / f"{lang.key}-{digest.hexdigest()[:12]}"
    if not (dest / READY).exists():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        if lang.deps_cmd:
            res = subprocess.run(lang.deps_cmd, shell=True, cwd=dest, capture_output=True, text=True)
            if res.returncode != 0:
                raise VetError(f"installing {lang.key} dependencies failed: {(res.stdout + res.stderr)[-500:]}")
        (dest / READY).write_text("", encoding="utf-8")
    return dest


def assemble_app(scaffold: Path | None, links: tuple[str, ...], solution: Path, dest: Path) -> Path:
    """Build an app folder as /app looks after the solution script: scaffold, linked dependencies, solution on top."""
    dest.mkdir(parents=True)
    if scaffold is not None:
        for item in scaffold.iterdir():
            if item.name == READY:
                continue
            if item.name in links:
                (dest / item.name).symlink_to(item, target_is_directory=item.is_dir())
            elif item.is_dir():
                shutil.copytree(item, dest / item.name)
            else:
                shutil.copy2(item, dest / item.name)
    shutil.copytree(solution, dest, dirs_exist_ok=True)
    return dest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _missing_tools(lang_env: dict[str, str], path: str, deps_cmd: str = "") -> list[str]:
    missing = []
    for cmd in (lang_env.get("BUILD_CMD", ""), lang_env.get("START_CMD", ""), lang_env.get("RUN_CMD", ""), deps_cmd):
        cmd = cmd.strip()
        tool = shlex.split(cmd)[0] if cmd else ""
        if tool and not tool.startswith(("./", "/", "{app}")) and not shutil.which(tool, path=path) and tool not in missing:
            missing.append(tool)
    return missing


MACHINE_SHARE = 0.75  # how much of the Docker machine's memory one container may be capped at


def machine_memory_mb() -> int | None:
    """The memory of the machine Docker runs containers on; None when Docker does not say."""
    try:
        res = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True,
                             timeout=60)
        return int(res.stdout.strip()) // 2**20 or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def container_room(task: Task, machine_mb: int | None) -> dict:
    """The caps of the container a solution is judged in: memory, processors, processes and, for a
    command-line task, the capped folder of the cases.

    A container is capped at no more than three quarters of the Docker machine's memory, so that the
    machine keeps room for itself and for a second container beside it: a task that asks for more is
    given three quarters, and the result says so. A command-line task whose cases do not fit under
    that is refused; it is vetted on a Docker machine with at least four thirds of what it needs."""
    asked = int(task.limits["memory_mb"])
    share = None if machine_mb is None else int(machine_mb * MACHINE_SHARE)
    memory = asked if share is None else min(asked, share)
    room = {"memory_mb": memory, "cpus": task.limits["cpus"], "processes": PROCESS_CAP}
    if memory != asked:
        room["adjusted"] = {"key": "limits.memory_mb", "asked_mb": asked, "given_mb": memory,
                            "machine_mb": machine_mb,
                            "message": f"the task asks for {asked} MB; this Docker machine has {machine_mb} MB, and "
                                       f"a container here is capped at no more than three quarters of it, {memory} MB"}
    if task.meta.get("interface") == "cli":
        needed = container_memory_mb(task.cli)
        if memory < needed:
            raise VetError({"file": "task.toml", "key": "limits.memory_mb",
                            "message": f"the cases need {needed} MB of container memory, and a container on this "
                                       f"Docker machine gets at most {memory} MB, three quarters of its {machine_mb} MB: "
                                       f"vet this task on a Docker machine with at least {math.ceil(needed / MACHINE_SHARE)} MB"})
        room["cases"] = cases_mount(task)
    return room


def docker_create_cmd(name: str, image: str, step: str, room: dict) -> list[str]:
    """A stopped container that, once /tests and /solution are copied in, judges the solution like the verifier.

    Every such container is capped in memory (with no swap beyond it), processors and processes, so
    that neither a build nor a program can take the Docker machine down."""
    # the script the verifier runs, so that a judge that is ended leaves the same account here as there
    script = ("mkdir -p /logs/verifier && cp -R /solution/. /app/ && "
              f"printf 'DODAM_STEP=%s\\n' {shlex.quote(step)} > /tests/step.env && exec sh /tests/test.sh")
    caps = ["--memory", f"{room['memory_mb']}m", "--memory-swap", f"{room['memory_mb']}m",
            "--cpus", str(room["cpus"]), "--pids-limit", str(room["processes"])]
    if room.get("cases"):
        caps += ["--tmpfs", room["cases"]]
    return ["docker", "create", "--name", name, "--network", "none", "--user", "0", *caps,
            "-e", "DODAM_APP_USER=agent", image, "sh", "-c", script]


def empty_place(folder: Path) -> None:
    """Make the place for one judging's results empty, so that nothing an earlier judging left there
    is read as this one's."""
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    elif folder.is_dir():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)


def judge_in_image(image: str, tests_dir: Path, solution_dir: Path, out_dir: Path, step: str,
                   room: dict, timeout: float, task: Task) -> Judged:
    """Judge one solution in a fresh container of the image; the judge's files land in out_dir, which
    is emptied first, and are read as the record of the judging. A container that did not end well,
    or ended without a result, is a fault of the judging and not a result: so is one that runs past
    `timeout`, the task's own judging time."""
    name = f"dodam-vet-{uuid.uuid4().hex[:12]}"
    empty_place(out_dir)
    try:
        subprocess.run(docker_create_cmd(name, image, step, room), check=True, capture_output=True, text=True)
        subprocess.run(["docker", "cp", f"{tests_dir}/.", f"{name}:/tests"], check=True, capture_output=True)
        subprocess.run(["docker", "cp", f"{solution_dir}/.", f"{name}:/solution"], check=True, capture_output=True)
        try:
            ended = subprocess.run(["docker", "start", "--attach", name], capture_output=True,
                                   timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            ended = None
        # what the container left, if anything: a container that left nothing is read below
        subprocess.run(["docker", "cp", f"{name}:/logs/verifier/.", str(out_dir)], capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    return read_judging(out_dir, task, step, ended, timeout, separated=True)


def vet_in_image(task: Task, lang: str, out_dir: Path, image: str) -> list[dict]:
    results = []
    room = container_room(task, machine_memory_mb())
    with tempfile.TemporaryDirectory(prefix="dodam-vet-") as work:
        harbor = to_harbor(task, lang, Path(work), image)
        for step in task.steps:
            for variant in task.solutions_for(lang, step.name):
                result = {"lang": lang, "step": step.name, "solution": variant.label, "expect": variant.expect,
                          "weakness": variant.weakness, "image": image, "separated": True,
                          "caps": {k: v for k, v in room.items() if k != "adjusted"}}
                if "adjusted" in room:
                    result["adjusted"] = room["adjusted"]
                started = time.monotonic()
                bounds: tuple[int, ...] = ()
                try:
                    judged = judge_in_image(image, harbor / "tests",
                                            harbor / "steps" / step.name / "solution" / "variants" / variant.label,
                                            out_dir / step.name / variant.label, step.name, room,
                                            task.limits["verifier_timeout_sec"], task)
                except JudgeFault as exc:
                    result.update(ok=False, judge_fault=True, problems=[f"the judge itself failed: {exc}"])
                    # a judging with a case not judged for measuring tells its room as well
                    bounds = getattr(exc, "bounds", ())
                except (subprocess.SubprocessError, OSError, ValueError) as exc:
                    result.update(ok=False, problems=[f"could not judge in {image}: {exc}"])
                else:
                    found = judgement_problems(variant.expect, variant.weakness, judged)
                    result.update(ok=not found, problems=found, reward=dict(judged.reward))
                    bounds = judged.bounds
                if task.cli.get("case_memory_mb"):
                    # from what the one place read, never from a bound the judge did not work out
                    result["room_mb"] = room_of(bounds, task.cli["case_memory_mb"])
                result["seconds"] = round(time.monotonic() - started, 1)
                results.append(result)
    return results


def room_of(bounds: tuple[int, ...], limit_mb: int) -> float | None:
    """The room a judging left: the case's memory limit less the largest of `bounds`, in MB; None where
    there is none. The bounds are those the one place read: of the cases that passed or were not judged
    for measuring, where the judge worked them out. A case that failed is left out there: it may have
    been ended above its limit by design. The bound counts every record of the kernel's that stands, so
    a language whose build leaves more of them leaves less room."""
    return round(limit_mb - max(bounds) / 2**20, 1) if bounds else None


def vet(task: Task, lang: str, out_dir: Path | str, cache_root: Path = SCAFFOLD_CACHE,
        image: str | None = None, unseparated: bool = False) -> list[dict]:
    """Judge each solution of one language at its step; write the judge's files and vet.json to out_dir.

    With an image, judge inside that language image, where the program is separated from the judge.
    Without one the solutions run with this machine's tools as the user who runs this: nothing
    separates them from the judge, so it must be asked for with unseparated=True, every result says
    "separated": false, and it is only for the project's own prepared solutions."""
    problems = task_problems(task)
    if problems:
        raise VetError(problems)
    if lang not in LANGUAGES or lang not in task.languages:
        raise VetError({"file": "vet", "key": "lang",
                        "message": f"{lang} is not a language of the task {task.id}: "
                                   f"its languages are {', '.join(task.languages)}"})
    out_dir = Path(out_dir)
    if image is None and not unseparated:
        raise VetError({
            "severity": "error", "file": "vet", "key": "image",
            "message": "judging without an image runs the solutions as the user who runs this, with nothing "
                       "between them and the judge; name an image, or ask for it with --unseparated when "
                       "every solution is the project's own"})
    if image is not None:
        results = vet_in_image(task, lang, out_dir, image or LANGUAGES[lang].image)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "vet.json").write_text(json.dumps({"task": task.id, "lang": lang, "results": results}, indent=1),
                                          encoding="utf-8")
        return results
    language = LANGUAGES[lang]
    extra_env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    lang_env = verifier_env(task, lang)
    if "PORT" in lang_env:
        lang_env["PORT"] = str(_free_port())
    missing = _missing_tools(lang_env, extra_env["PATH"], language.deps_cmd)
    scaffold = None if missing else scaffold_cache(language, cache_root)
    # on this machine the pinned dependency folders are the prepared scaffold's, not the image's
    lang_env["DEPS_DIR"] = str(scaffold) if scaffold and language.deps_links else ""
    results = []
    with tempfile.TemporaryDirectory(prefix="dodam-vet-") as work:
        tests_dir = Path(work) / "tests"
        shutil.copytree(task.root / "tests", tests_dir, ignore=COPY_IGNORE)
        for step in task.steps:
            for variant in task.solutions_for(lang, step.name):
                result = {"lang": lang, "step": step.name, "solution": variant.label,
                          "expect": variant.expect, "weakness": variant.weakness, "separated": False}
                if missing:
                    result.update(ok=False, skipped=True, problems=[f"not on this machine: {', '.join(missing)}"])
                    results.append(result)
                    continue
                solution = materialize(task, variant, Path(work) / "solutions" / step.name / variant.label)
                app_dir = assemble_app(scaffold, language.deps_links, solution,
                                       Path(work) / "apps" / step.name / variant.label)
                started = time.monotonic()
                place = out_dir / step.name / variant.label
                try:
                    empty_place(place)
                    judge(tests_dir, app_dir, step.name, place, lang_env=lang_env, extra_env=extra_env,
                          unseparated=True)
                    # what the judge left is read through the one place, as in the image
                    judged = read_judging(place, task, step.name, separated=False)
                except JudgeFault as exc:
                    result.update(ok=False, judge_fault=True, problems=[f"the judge itself failed: {exc}"])
                else:
                    found = judgement_problems(variant.expect, variant.weakness, judged)
                    result.update(ok=not found, problems=found, reward=dict(judged.reward))
                result["seconds"] = round(time.monotonic() - started, 1)
                results.append(result)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vet.json").write_text(json.dumps({"task": task.id, "lang": lang, "results": results}, indent=1),
                                      encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("task", type=Path)
    p.add_argument("--lang", action="append", help="language to vet; repeat for several (default: every language of the task)")
    p.add_argument("--out", type=Path, default=Path("vet-out"))
    p.add_argument("--image", nargs="?", const="", default=None,
                   help="judge inside the language image (default tag dodam-bench/<language>:dev) instead of on this machine")
    p.add_argument("--unseparated", action="store_true",
                   help="judge with this machine's tools as the user who runs this, with nothing between the "
                        "solutions and the judge; only for the project's own prepared solutions")
    a = p.parse_args(argv)

    def refused(problems: list[dict], what: str) -> int:
        for found in problems:
            print(json.dumps(found))  # JSON first; the line a person reads is made from it
            print(f"{what} cannot be vetted: {found['file']}: {found['message']}", file=sys.stderr)
        return 2

    try:
        task = load_task(a.task)
    except TaskError as exc:
        return refused([{"severity": "error", "file": "task.toml", "key": "", "message": str(exc)}], str(a.task))
    langs = a.lang or task.languages
    every = []
    for lang in langs:
        try:
            results = vet(task, lang, a.out / task.id / lang, image=a.image, unseparated=a.unseparated)
        except VetError as exc:
            return refused(exc.problems, f"task {task.id}")
        for r in results:
            mark = "skip" if r.get("skipped") else ("ok" if r["ok"] else "FAIL")
            print(f"{mark:4}  {lang:10}  {r['step']:10}  {r['solution']:34}  {r.get('seconds', 0):6.1f}s  "
                  + "; ".join(r["problems"]))
        rooms = [r["room_mb"] for r in results if r.get("room_mb") is not None]
        if rooms:
            # for whoever writes the task: how close the prepared solutions of this language came; it refuses nothing
            print(f"room  {lang:10}  the smallest room between the memory bound and the limit: {min(rooms):.1f} MB")
        every += results
    unchecked = [r for r in every if r.get("skipped")]
    if unchecked:
        # a solution that was not judged is no success: the run says how many and why
        reasons = sorted({p for r in unchecked for p in r["problems"]})
        said = {"severity": "error", "file": "vet", "key": "checked",
                "message": (f"nothing was checked: " if len(unchecked) == len(every) else "")
                + f"{len(unchecked)} of {len(every)} solutions were not checked: {'; '.join(reasons)}"}
        print(json.dumps(said))
        print(f"task {task.id}: {said['message']}", file=sys.stderr)
        return 1
    if not every:
        print(json.dumps({"severity": "error", "file": "vet", "key": "checked", "message": "nothing was checked"}))
        return 1
    return 0 if all(r["ok"] for r in every) else 1


if __name__ == "__main__":
    raise SystemExit(main())
