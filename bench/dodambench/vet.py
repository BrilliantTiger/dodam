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
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .convert import to_harbor, verifier_env
from .judge import JudgeFault, judge
from .languages import LANGUAGES, Language
from .task import (PROCESS_CAP, Task, TaskError, cases_mount, container_memory_mb, load_task, materialize,
                   task_problems)

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


def _files(cases: list[dict]) -> str:
    return ", ".join(sorted({c["file"] for c in cases}))


def judgement_problems(expect: str, weakness: str | None, report: dict) -> list[str]:
    """Why a judged solution did not get the judgement it was written for; empty when it did."""
    if not report.get("build_ok"):
        return ["the program did not build"]
    if report.get("left_behind"):
        # the step failed for what the program's side left, not for what the solution was written to show
        return [f"the program's side left what the judge cannot take back: {report['left_behind']}"]
    tests = report.get("tests", [])
    step = report.get("step")
    if any(t.get("message") == "program did not start" for t in tests):
        return ["the program did not start"]
    failed = [t for t in tests if t["outcome"] == "failed"]
    features = [t for t in failed if t["kind"] in FEATURE_KINDS]
    unjudged = [t for t in tests if t["outcome"] == "skipped"]
    problems = []
    if unjudged and expect in ("secure", "vulnerable"):
        problems.append(f"tests left unjudged: {_files(unjudged)}")
    if expect == "secure":
        if failed:
            problems.append(f"the secure solution failed {_files(failed)}")
    elif expect == "vulnerable":
        security = [t for t in failed if t["kind"] == "security"]
        own = [t for t in security if t["weakness"] == weakness]
        others = [t for t in security if t["weakness"] != weakness]
        # the solution is written for its step: a test of its weakness at that step has to fail
        if not [t for t in own if t.get("step") == step]:
            problems.append(f"no security test of {weakness}" + (f" at step {step}" if step else "") + " failed")
        if others:
            kinds = ", ".join(sorted({str(t["weakness"]) for t in others}))
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
                   room: dict, timeout: float) -> dict:
    """Judge one solution in a fresh container of the image; the judge's files land in out_dir, which
    is emptied first. A container that did not end well, or ended without a result, is a fault of
    the judging and not a result: so is one that runs past `timeout`, the task's own judging time."""
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
    fault = out_dir / "judge-fault.json"
    if fault.is_file():
        raise JudgeFault(json.loads(fault.read_text(encoding="utf-8")).get("judge_fault", "unknown"))
    results = (out_dir / "reward.json", out_dir / "details.json")
    missing = not all(path.is_file() for path in results)
    if ended != 0 or missing:
        how = f"did not end within {timeout:g} seconds" if ended is None else f"ended with exit code {ended}"
        said = f"the container {how}" + (" and left no result" if missing else ", so what it left is no result")
        raise JudgeFault(json.dumps({"severity": "error", "file": "container", "key": "ended", "message": said}))
    return {"reward": json.loads(results[0].read_text(encoding="utf-8")),
            "report": json.loads(results[1].read_text(encoding="utf-8"))}


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
                try:
                    judged = judge_in_image(image, harbor / "tests",
                                            harbor / "steps" / step.name / "solution" / "variants" / variant.label,
                                            out_dir / step.name / variant.label, step.name, room,
                                            task.limits["verifier_timeout_sec"])
                except JudgeFault as exc:
                    result.update(ok=False, judge_fault=True, problems=[f"the judge itself failed: {exc}"])
                except (subprocess.SubprocessError, OSError, ValueError) as exc:
                    result.update(ok=False, problems=[f"could not judge in {image}: {exc}"])
                else:
                    found = judgement_problems(variant.expect, variant.weakness, judged["report"])
                    result.update(ok=not found, problems=found, reward=judged["reward"])
                result["seconds"] = round(time.monotonic() - started, 1)
                results.append(result)
    return results


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
                try:
                    judged = judge(tests_dir, app_dir, step.name, out_dir / step.name / variant.label,
                                   lang_env=lang_env, extra_env=extra_env, unseparated=True)
                except JudgeFault as exc:
                    result.update(ok=False, judge_fault=True, problems=[f"the judge itself failed: {exc}"])
                else:
                    found = judgement_problems(variant.expect, variant.weakness, judged["report"])
                    result.update(ok=not found, problems=found, reward=judged["reward"])
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
