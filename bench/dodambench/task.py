"""Source task format: load a language-neutral task folder and check it against the format rules."""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .judge import CASE_ENTRIES, CASES_DIR, JUDGE_ROOM_MB, step_terms
from .languages import LANGUAGES

DOMAINS = {"web", "cli", "data", "net", "sys"}
KINDS = {"new", "add-feature", "schema-change", "concurrency", "package-lure"}
INTERFACES = {"http", "cli", "file"}
VISIBILITY = {"public", "private"}
TEST_KINDS = ("functional", "boundary", "security")
EXPECTATIONS = ("secure", "vulnerable", "broken")
WEAKNESSES = {
    "sql-injection", "xss", "command-injection", "code-injection", "path-traversal", "ssrf",
    "broken-access-control", "broken-authentication", "token-forgery", "plaintext-credentials",
    "secret-exposure", "log-injection", "resource-exhaustion", "unsafe-upload", "weak-crypto",
    "missing-rate-limit", "input-validation", "error-handling", "race-condition", "file-exposure",
    "file-overwrite",
}
# The limits of one case of a command-line task, with the largest value each may take.
CLI_LIMIT_KEYS = {"case_timeout_sec": 600, "case_memory_mb": 65536, "case_output_kb": 1048576,
                  "case_processes": 1024, "case_disk_mb": 65536}
PROCESS_CAP = 1024  # processes and threads in one container: the judge, the build and the program together
# A hidden test of a command-line task reaches the program only through the judge's case runner, and
# learns nothing about the language: these would start the program another way or read the settings.
CLI_TEST_FORBIDDEN = {
    "subprocess": r"\bsubprocess\b", "os.system": r"\bos\.system\b", "os.popen": r"\bos\.popen\b",
    "os.exec": r"\bos\.exec", "os.spawn": r"\bos\.(posix_)?spawn", "os.fork": r"\bos\.fork", "pty": r"\bpty\b",
    "DODAM_CLI_CONFIG": r"DODAM_CLI_CONFIG", "lang.env": r"lang\.env", "parametrize": r"\bparametrize\b",
}
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CANARY_PREFIX = "dodam-bench-canary-"


class TaskError(Exception):
    """The task folder cannot be read at all."""


@dataclass(frozen=True)
class Step:
    name: str
    instruction: str


@dataclass(frozen=True)
class TestFile:
    file: str
    kind: str
    step: str
    weakness: str | None
    fresh_app: bool
    superseded_in: tuple[str, ...]


@dataclass(frozen=True)
class Replacement:
    file: str
    old: str
    new: str


@dataclass(frozen=True)
class Variant:
    """A prepared solution of one language and step: the secure one, or exact replacements on it."""
    name: str
    lang: str
    step: str
    expect: str
    weakness: str | None = None
    replacements: tuple[Replacement, ...] = ()
    source: str = ""

    @property
    def label(self) -> str:
        return self.expect if not self.name else f"{self.expect}-{self.name}"


@dataclass
class Task:
    root: Path
    meta: dict
    steps: list[Step]
    tests: list[TestFile]
    manifest_problem: str | None
    variants: list[Variant]
    variants_problem: str | None

    @property
    def id(self) -> str:
        return self.meta.get("id", "")

    @property
    def canary(self) -> str:
        return self.meta.get("canary", "")

    @property
    def languages(self) -> list[str]:
        return list(self.meta.get("languages", list(LANGUAGES)))

    @property
    def app(self) -> dict:
        return self.meta.get("app", {})

    @property
    def limits(self) -> dict:
        return self.meta.get("limits", {})

    @property
    def cli(self) -> dict:
        return self.meta.get("cli", {})

    def step_index(self, name: str) -> int:
        return [s.name for s in self.steps].index(name)

    def tests_for(self, step: str) -> list[TestFile]:
        """Tests judged at a step: those of this and earlier steps, minus the ones this step supersedes."""
        upto = self.step_index(step)
        names = [s.name for s in self.steps]
        return [t for t in self.tests
                if t.step in names and names.index(t.step) <= upto and step not in t.superseded_in]

    def secure_dir(self, lang: str, step: str) -> Path:
        return self.root / "solutions" / "secure" / lang / step

    def solutions_for(self, lang: str, step: str) -> list[Variant]:
        """The secure solution of a language and step, then its variants; empty when there is no secure one."""
        if not self.secure_dir(lang, step).is_dir():
            return []
        return [Variant("", lang, step, "secure")] + [v for v in self.variants if v.lang == lang and v.step == step]


def _load_variants(folder: Path) -> tuple[list[Variant], str | None]:
    """Variants live in solutions/variants/<language>.toml; a variant's language defaults to its file's."""
    problems: list[str] = []
    if (folder.parent / "variants.toml").exists():
        problems.append("solutions/variants.toml is no longer read; "
                        "put each language's variants in solutions/variants/<language>.toml")
    variants = []
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        where = f"solutions/variants/{path.name}"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{where}: {exc}")
            continue
        for v in data.get("variant", []):
            lang = str(v.get("lang", path.stem))
            if lang != path.stem:
                problems.append(f"{where}: variant {v.get('name', '')} says language {lang}, "
                                f"but this file holds {path.stem} variants")
                continue
            reps = tuple(Replacement(r.get("file", ""), r.get("old", ""), r.get("new", ""))
                         for r in v.get("replace", []))
            variants.append(Variant(str(v.get("name", "")), lang, str(v.get("step", "")),
                                    str(v.get("expect", "")), v.get("weakness"), reps, where))
    return variants, "; ".join(problems) or None


def load_task(root: Path | str) -> Task:
    root = Path(root)
    try:
        meta = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"{root}/task.toml: {exc}") from exc
    steps = [Step(s.get("name", ""), s.get("instruction", "")) for s in meta.get("steps", [])]
    tests, problem = [], None
    try:
        manifest = tomllib.loads((root / "tests/manifest.toml").read_text(encoding="utf-8"))
        for t in manifest.get("test", []):
            kind = t.get("kind", "")
            tests.append(TestFile(
                file=t.get("file", ""), kind=kind, step=t.get("step", ""), weakness=t.get("weakness"),
                fresh_app=bool(t.get("fresh_app", kind == "security")),
                superseded_in=tuple(t.get("superseded_in", ()))))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problem = f"tests/manifest.toml: {exc}"
    variants, variants_problem = _load_variants(root / "solutions" / "variants")
    return Task(root, meta, steps, tests, problem, variants, variants_problem)


def materialize(task: Task, variant: Variant, dest: Path | str) -> Path:
    """Write a prepared solution to dest: a copy of the secure solution with the variant's replacements applied."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(task.secure_dir(variant.lang, variant.step), dest)
    for r in variant.replacements:
        path = dest / r.file
        text = path.read_text(encoding="utf-8")
        if text.count(r.old) != 1:
            raise TaskError(f"variant {variant.label}: the text to replace in {r.file} must occur exactly once")
        path.write_text(text.replace(r.old, r.new), encoding="utf-8")
    return dest


def _agent_visible_files(task: Task) -> list[Path]:
    files = [task.root / s.instruction for s in task.steps if s.instruction]
    spec = task.root / "spec"
    if spec.is_dir():
        files += [p for p in spec.rglob("*") if p.is_file()]
    return files


def validate(task: Task) -> list[str]:
    errors: list[str] = []
    m = task.meta
    if m.get("schema") != 1:
        errors.append("task.toml: schema must be 1")
    if not SLUG.match(str(m.get("id", ""))):
        errors.append("task.toml: id must be lowercase words joined by hyphens")
    if not m.get("title"):
        errors.append("task.toml: title is missing")
    for key, allowed in (("domain", DOMAINS), ("kind", KINDS), ("interface", INTERFACES), ("visibility", VISIBILITY)):
        if m.get(key) not in allowed:
            errors.append(f"task.toml: {key} must be one of {sorted(allowed)}")
    if not str(m.get("canary", "")).startswith(CANARY_PREFIX) or len(m.get("canary", "")) < len(CANARY_PREFIX) + 16:
        errors.append(f"task.toml: canary must start with {CANARY_PREFIX} and carry a unique id")
    for lang in task.languages:
        if lang not in LANGUAGES:
            errors.append(f"task.toml: unknown language {lang}")
    if m.get("interface") == "http":
        app = task.app
        if not app:
            errors.append("task.toml: an http task needs an [app] section")
        elif not isinstance(app.get("port"), int):
            errors.append("task.toml: [app] port must be a number")
    errors += [f"{d['file']}: {d['message']}" for d in cli_problems(task)]
    limits = task.limits
    for key in ("agent_timeout_sec", "verifier_timeout_sec", "cpus", "memory_mb"):
        if not isinstance(limits.get(key), int) or limits.get(key) <= 0:
            errors.append(f"task.toml: [limits] {key} must be a positive number")

    names = [s.name for s in task.steps]
    if not names:
        errors.append("task.toml: at least one [[steps]] is needed")
    seen = set()
    for s in task.steps:
        if not SLUG.match(s.name):
            errors.append(f"task.toml: step name {s.name!r} must be lowercase words joined by hyphens")
        if s.name in seen:
            errors.append(f"task.toml: duplicate step {s.name}")
        seen.add(s.name)
        if not s.instruction or not (task.root / s.instruction).is_file():
            errors.append(f"task.toml: step {s.name} instruction {s.instruction} does not exist")

    canary = task.canary
    if canary:
        for path in _agent_visible_files(task):
            if path.is_file() and canary in path.read_text(encoding="utf-8", errors="replace"):
                rel = path.relative_to(task.root)
                where = "spec" if rel.parts[0] == "spec" else "instruction"
                errors.append(f"{rel}: the canary must not appear in an agent {where} file")

    errors += _variant_errors(task, names)
    if task.manifest_problem:
        errors.append(task.manifest_problem)
        return errors
    listed = set()
    for t in task.tests:
        listed.add(t.file)
        path = task.root / "tests" / t.file
        if t.kind not in TEST_KINDS:
            errors.append(f"tests/manifest.toml: {t.file} kind must be one of {list(TEST_KINDS)}")
        elif not t.file.startswith(t.kind + "/"):
            errors.append(f"tests/manifest.toml: {t.file} must sit in the {t.kind}/ folder")
        if t.step not in names:
            errors.append(f"tests/manifest.toml: {t.file} step {t.step} is not a step of this task")
        for s in t.superseded_in:
            if s not in names:
                errors.append(f"tests/manifest.toml: {t.file} superseded_in {s} is not a step of this task")
        if t.kind == "security" and t.weakness not in WEAKNESSES:
            errors.append(f"tests/manifest.toml: {t.file} needs a known weakness, got {t.weakness!r}")
        if not path.is_file():
            errors.append(f"tests/{t.file} is listed but does not exist")
        elif canary and canary not in path.read_text(encoding="utf-8", errors="replace"):
            errors.append(f"tests/{t.file} must carry the canary string")
    for kind in TEST_KINDS:
        folder = task.root / "tests" / kind
        if folder.is_dir():
            for path in sorted(folder.rglob("test_*.py")):
                rel = str(path.relative_to(task.root / "tests"))
                if rel not in listed:
                    errors.append(f"tests/{rel} is not listed in tests/manifest.toml")
    return errors


def _variant_errors(task: Task, step_names: list[str]) -> list[str]:
    if task.variants_problem:
        return [task.variants_problem]
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for v in task.variants:
        label, where = v.label, v.source or "solutions/variants"
        if not SLUG.match(v.name):
            errors.append(f"{where}: variant name {v.name!r} must be lowercase words joined by hyphens")
        if v.expect not in EXPECTATIONS:
            errors.append(f"{where}: variant {v.name} expect {v.expect!r} must be one of {list(EXPECTATIONS)}")
        if v.lang not in task.languages:
            errors.append(f"{where}: variant {v.name} language {v.lang} is not a language of this task")
        if v.step not in step_names:
            errors.append(f"{where}: variant {v.name} step {v.step} is not a step of this task")
            continue
        key = (v.lang, v.step, label)
        if key in seen:
            errors.append(f"{where}: duplicate variant {label} for {v.lang} at step {v.step}")
        seen.add(key)
        if v.expect == "vulnerable":
            if v.weakness not in WEAKNESSES:
                errors.append(f"{where}: vulnerable variant {v.name} needs a known weakness, got {v.weakness!r}")
            elif task.manifest_problem is None and not any(
                    t.kind == "security" and t.weakness == v.weakness for t in task.tests_for(v.step)):
                errors.append(f"{where}: variant {v.name} is vulnerable to {v.weakness}, "
                              f"but no security test checks {v.weakness} at step {v.step}")
        base = task.secure_dir(v.lang, v.step)
        if not base.is_dir():
            errors.append(f"{where}: variant {v.name} needs {base.relative_to(task.root)} to start from")
            continue
        if not v.replacements:
            errors.append(f"{where}: variant {v.name} has no replacements")
        for r in v.replacements:
            path = base / r.file
            if not r.file or not path.is_file():
                errors.append(f"{where}: variant {v.name} replaces text in {r.file!r}, which the secure solution lacks")
                continue
            count = path.read_text(encoding="utf-8").count(r.old) if r.old else 0
            if count == 0:
                errors.append(f"{where}: variant {v.name}: text to replace not found in {r.file}")
            elif count > 1:
                errors.append(f"{where}: variant {v.name}: text to replace occurs more than once in {r.file}")
    return errors


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def cases_mount(task: "Task") -> str:
    """The memory-backed folder the cases live in, as Docker is asked for it: capped at the files a
    case may write and at the number of entries it may hold."""
    return (f"{CASES_DIR}:rw,exec,nosuid,nodev,size={task.cli['case_disk_mb']}m,"
            f"nr_inodes={CASE_ENTRIES},mode=0711")


def judging_seconds(task: Task) -> int:
    """What the format checker adds up for the longest step of a command-line task: every wait and
    every time limit the judge has in a step, term by term as the judge's step_terms gives them, so
    that nothing the judge can spend in a step lies beyond the sum."""
    timeout = task.cli.get("case_timeout_sec")
    if not _whole(timeout) or task.manifest_problem:
        return 0
    longest = 0
    for step in task.steps:
        tests_per_file = []
        for t in task.tests_for(step.name):
            path = task.root / "tests" / t.file
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            tests_per_file.append(len(re.findall(r"^def test_", text, flags=re.M)))
        longest = max(longest, math.ceil(sum(step_terms(tests_per_file, timeout).values())))
    return longest


def cli_problems(task: Task) -> list[dict]:
    """What is wrong with the command-line part of a task, as data: the file, the key and the message."""
    def problem(file: str, key: str, message: str) -> dict:
        return {"file": file, "key": key, "message": message}

    found: list[dict] = []
    cli = task.meta.get("cli")
    if task.meta.get("interface") != "cli":
        if cli is not None:
            found.append(problem("task.toml", "cli", "a [cli] section belongs to a task whose interface is cli"))
        return found
    if "app" in task.meta:
        found.append(problem("task.toml", "app", "a cli task starts no server, so it takes no [app] section"))
    if not isinstance(cli, dict):
        found.append(problem("task.toml", "cli", "a cli task needs a [cli] section with the limits of one case"))
        return found
    for key, most in CLI_LIMIT_KEYS.items():
        value = cli.get(key)
        if not _whole(value) or value <= 0:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} must be a positive whole number"))
        elif value > most:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} must be at most {most}"))
    for key in cli:
        if key not in CLI_LIMIT_KEYS:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} is not a limit of a case"))
    memory, room = cli.get("case_memory_mb"), task.limits.get("memory_mb")
    disk = cli.get("case_disk_mb")
    if _whole(memory) and _whole(room) and _whole(disk) and 0 < room < memory + disk + JUDGE_ROOM_MB:
        found.append(problem("task.toml", "limits.memory_mb",
                             f"[limits] memory_mb must be at least {memory + disk + JUDGE_ROOM_MB}: the container's "
                             f"memory holds the case's memory ({memory}), its files ({disk}), which live in "
                             f"memory, and the judge ({JUDGE_ROOM_MB})"))
    needed, given = judging_seconds(task), task.limits.get("verifier_timeout_sec")
    if needed and _whole(given) and given < needed:
        found.append(problem("task.toml", "limits.verifier_timeout_sec",
                             f"[limits] verifier_timeout_sec must be at least {needed}: every wait and "
                             "time limit of a step, so a program that hangs in every case still gets a result"))
    tests = task.root / "tests"
    for path in sorted(tests.rglob("*.py")) if tests.is_dir() else []:
        rel = f"tests/{path.relative_to(tests).as_posix()}"
        text = path.read_text(encoding="utf-8", errors="replace")
        for word, pattern in CLI_TEST_FORBIDDEN.items():
            if re.search(pattern, text):
                found.append(problem(rel, word, f"a test of a cli task must not use {word}: it runs the program "
                                                "only through dodam_cli and knows nothing about the language"))
        if path.name.startswith("test_") and "dodam_cli" not in text:
            found.append(problem(rel, "dodam_cli", "a test of a cli task runs the program through dodam_cli"))
    return found


def diagnostics(task: Task) -> list[dict]:
    """Every problem of a task as data. The text a person reads is made from this."""
    structured = {f"{d['file']}: {d['message']}": d for d in cli_problems(task)}
    found = []
    for text in validate(task):
        if text in structured:
            found.append({"severity": "error", **structured[text]})
            continue
        head, sep, rest = text.partition(": ")
        if sep and " " not in head:
            found.append({"severity": "error", "file": head, "message": rest})
        else:
            first = text.split(" ", 1)[0]
            found.append({"severity": "error", "file": first if "/" in first or "." in first else "", "message": text})
    return found


def render(problems: list[dict]) -> str:
    """The lines a person reads, made from the diagnostics."""
    return "".join(f"{d['file']}: {d['message']}\n" if d.get("file") and not d["message"].startswith(d["file"])
                   else f"{d['message']}\n" for d in problems)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check a source task against the format rules. Prints JSON.")
    p.add_argument("task", type=Path)
    p.add_argument("--text", action="store_true", help="print the lines a person reads instead of JSON")
    a = p.parse_args(argv)
    try:
        task = load_task(a.task)
    except TaskError as exc:
        report, code = {"task": "", "ok": False,
                        "problems": [{"severity": "error", "file": "task.toml", "message": str(exc)}]}, 2
    else:
        problems = diagnostics(task)
        report, code = {"task": task.id, "ok": not problems, "problems": problems}, 1 if problems else 0
    if a.text:
        sys.stdout.write(render(report["problems"]) or "ok\n")
    else:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
