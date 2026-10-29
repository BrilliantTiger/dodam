"""Vet a source task: judge every prepared solution and check it gets the judgement it was written for.

The secure solution must pass every test. A vulnerable variant must pass every functional and boundary
test and fail the security tests of its own weakness, and only those. A broken variant must fail a
functional or boundary test. Tests that cannot tell these apart would let weak programs through, so a
task that fails vetting is not ready. The judge is the one the verifier runs; the language tools come
from this machine, with this Python environment's own tools first on PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .convert import verifier_env
from .judge import judge
from .languages import LANGUAGES, Language
from .task import Task, load_task, materialize, validate

FEATURE_KINDS = ("functional", "boundary")
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
BENCH = Path(__file__).resolve().parents[1]
SCAFFOLDS = BENCH / "images"
SCAFFOLD_CACHE = BENCH / ".cache" / "scaffolds"
READY = ".ready"


class VetError(Exception):
    pass


def _files(cases: list[dict]) -> str:
    return ", ".join(sorted({c["file"] for c in cases}))


def judgement_problems(expect: str, weakness: str | None, report: dict) -> list[str]:
    """Why a judged solution did not get the judgement it was written for; empty when it did."""
    if not report.get("build_ok"):
        return ["the program did not build"]
    tests = report.get("tests", [])
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
        if not own:
            problems.append(f"no security test of {weakness} failed")
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
    for cmd in (lang_env.get("BUILD_CMD", ""), lang_env.get("START_CMD", ""), deps_cmd):
        cmd = cmd.strip()
        tool = shlex.split(cmd)[0] if cmd else ""
        if tool and not tool.startswith(("./", "/")) and not shutil.which(tool, path=path) and tool not in missing:
            missing.append(tool)
    return missing


def vet(task: Task, lang: str, out_dir: Path | str, cache_root: Path = SCAFFOLD_CACHE) -> list[dict]:
    """Judge each solution of one language at its step; write the judge's files and vet.json to out_dir."""
    problems = validate(task)
    if problems:
        raise VetError("; ".join(problems))
    out_dir = Path(out_dir)
    language = LANGUAGES[lang]
    extra_env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    lang_env = {**verifier_env(task, lang), "PORT": str(_free_port())}
    missing = _missing_tools(lang_env, extra_env["PATH"], language.deps_cmd)
    scaffold = None if missing else scaffold_cache(language, cache_root)
    results = []
    with tempfile.TemporaryDirectory(prefix="dodam-vet-") as work:
        tests_dir = Path(work) / "tests"
        shutil.copytree(task.root / "tests", tests_dir, ignore=COPY_IGNORE)
        for step in task.steps:
            for variant in task.solutions_for(lang, step.name):
                result = {"lang": lang, "step": step.name, "solution": variant.label,
                          "expect": variant.expect, "weakness": variant.weakness}
                if missing:
                    result.update(ok=False, skipped=True, problems=[f"not on this machine: {', '.join(missing)}"])
                    results.append(result)
                    continue
                solution = materialize(task, variant, Path(work) / "solutions" / step.name / variant.label)
                app_dir = assemble_app(scaffold, language.deps_links, solution,
                                       Path(work) / "apps" / step.name / variant.label)
                started = time.monotonic()
                judged = judge(tests_dir, app_dir, step.name, out_dir / step.name / variant.label,
                               lang_env=lang_env, extra_env=extra_env)
                found = judgement_problems(variant.expect, variant.weakness, judged["report"])
                result.update(ok=not found, problems=found, reward=judged["reward"],
                              seconds=round(time.monotonic() - started, 1))
                results.append(result)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vet.json").write_text(json.dumps({"task": task.id, "lang": lang, "results": results}, indent=1),
                                      encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("task", type=Path)
    p.add_argument("--lang", action="append", help="language to vet; repeat for several (default: every one with a secure solution)")
    p.add_argument("--out", type=Path, default=Path("vet-out"))
    a = p.parse_args(argv)
    task = load_task(a.task)
    langs = a.lang or [lang for lang in task.languages if (task.root / "solutions" / "secure" / lang).is_dir()]
    good = True
    for lang in langs:
        try:
            results = vet(task, lang, a.out / task.id / lang)
        except VetError as exc:
            print(f"task {task.id} is not valid: {exc}", file=sys.stderr)
            return 2
        for r in results:
            mark = "skip" if r.get("skipped") else ("ok" if r["ok"] else "FAIL")
            print(f"{mark:4}  {lang:10}  {r['step']:10}  {r['solution']:34}  {r.get('seconds', 0):6.1f}s  "
                  + "; ".join(r["problems"]))
            good = good and (r["ok"] or bool(r.get("skipped")))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
