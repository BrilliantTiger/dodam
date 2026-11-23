"""Source task format: load a language-neutral task folder and check it against the format rules.

Every problem is data: the file it is about, the key in that file (a TOML key in TOML's own notation,
`line <n>` in a Python file, empty for a whole file) and the message. The lines a person reads are
made from that data.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib.machinery
import json
import math
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .judge import BUILD_SECONDS, CASE_ENTRIES, CASES_DIR, JUDGE_ROOM_MB, build_seconds, step_terms, tests_in
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
PROCESS_CAP = 1024  # processes and threads in one container: the judge, the build and the program together
# The judge's own processes while a case runs: the verifier's script, the judge and the case runner,
# three as measured, and one more that ends what a program left. The container keeps twice those four
# above a case's limit.
JUDGE_PROCESSES = 8
# The limits of one case of a command-line task, with the largest value each may take.
CLI_LIMIT_KEYS = {"case_timeout_sec": 600, "case_memory_mb": 65536, "case_output_kb": 1048576,
                  "case_processes": PROCESS_CAP - JUDGE_PROCESSES, "case_disk_mb": 65536}
# What the case runner keeps of a program's output: at most four times the limit on output and 1 MB,
# as a test of the case runner shows.
OUTPUT_KEPT_TIMES, OUTPUT_KEPT_MB = 4, 1
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CANARY_PREFIX = "dodam-bench-canary-"


class TaskError(Exception):
    """The task folder cannot be read at all."""


def problem(file: str, key: str, message: str) -> dict:
    return {"file": file, "key": key, "message": message}


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
    index: int = 0

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
    # what could not be read as the format says, as problems; and the keys they are about, so that
    # nothing is said twice about one value
    loading: list[dict] = field(default_factory=list)
    wrong: set[tuple[str, str]] = field(default_factory=set)

    @property
    def id(self) -> str:
        value = self.meta.get("id", "")
        return value if isinstance(value, str) else ""

    @property
    def canary(self) -> str:
        value = self.meta.get("canary", "")
        return value if isinstance(value, str) else ""

    @property
    def languages(self) -> list[str]:
        if ("task.toml", "languages") in self.wrong:
            return []
        value = self.meta.get("languages", list(LANGUAGES))
        return [lang for lang in value if isinstance(lang, str)] if isinstance(value, list) else []

    @property
    def app(self) -> dict:
        value = self.meta.get("app", {})
        return value if isinstance(value, dict) else {}

    @property
    def limits(self) -> dict:
        value = self.meta.get("limits", {})
        return value if isinstance(value, dict) else {}

    @property
    def cli(self) -> dict:
        value = self.meta.get("cli", {})
        return value if isinstance(value, dict) else {}

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
        """The secure solution of a language and step, then its variants; empty when there is none."""
        if not self.secure_dir(lang, step).is_dir():
            return []
        return [Variant("", lang, step, "secure")] + [v for v in self.variants if v.lang == lang and v.step == step]


# --- reading the files of a task, with every value of the wrong kind as a problem ---

KIND_NAMES = {str: "a string", int: "a whole number", bool: "true or false", list: "a list", dict: "a table"}
TASK_KINDS = {"schema": int, "id": str, "title": str, "domain": str, "kind": str, "interface": str,
              "visibility": str, "canary": str, "languages": list, "app": dict, "cli": dict, "limits": dict,
              "steps": list}


def _of_kind(value, kind) -> bool:
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


class _Reader:
    """Takes values out of a file's tables, and keeps a problem for every one of the wrong kind."""

    def __init__(self, file: str, found: list[dict], wrong: set[tuple[str, str]]):
        self.file, self.found, self.wrong = file, found, wrong

    def bad(self, key: str, kind, what: str = "") -> None:
        self.found.append(problem(self.file, key, f"{key} must be {what or KIND_NAMES[kind]}"))
        self.wrong.add((self.file, key))

    def value(self, table: dict, name: str, kind, key: str, default=None):
        if name not in table:
            return default
        if not _of_kind(table[name], kind):
            self.bad(key, kind)
            return default
        return table[name]

    def tables(self, value, key: str) -> list[tuple[int, dict]]:
        """The tables of a list of tables, each with its place; a problem for whatever is not one."""
        if value is None:
            return []
        if not isinstance(value, list):
            self.bad(key, list, "a list of tables")
            return []
        found = []
        for i, item in enumerate(value):
            if isinstance(item, dict):
                found.append((i, item))
            else:
                self.bad(f"{key}[{i}]", dict)
        return found

    def strings(self, value, key: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            self.bad(key, list, "a list of strings")
            return ()
        kept = []
        for i, item in enumerate(value):
            if isinstance(item, str):
                kept.append(item)
            else:
                self.bad(f"{key}[{i}]", str)
        return tuple(kept)


def _load_variants(folder: Path, found: list[dict], wrong: set[tuple[str, str]]) -> list[Variant]:
    """Variants live in solutions/variants/<language>.toml; a variant's language defaults to its file's."""
    if (folder.parent / "variants.toml").exists():
        found.append(problem("solutions/variants.toml", "",
                             "solutions/variants.toml is no longer read; "
                             "put each language's variants in solutions/variants/<language>.toml"))
    variants = []
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        where = f"solutions/variants/{path.name}"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            found.append(problem(where, "", f"cannot be read: {exc}"))
            continue
        read = _Reader(where, found, wrong)
        for i, v in read.tables(data.get("variant"), "variant"):
            name = read.value(v, "name", str, f"variant[{i}].name", "")
            lang = read.value(v, "lang", str, f"variant[{i}].lang", path.stem)
            if lang != path.stem:
                found.append(problem(where, f"variant[{i}].lang", f"variant {name} says language {lang}, "
                                                                   f"but this file holds {path.stem} variants"))
                continue
            reps = []
            for j, r in read.tables(v.get("replace"), f"variant[{i}].replace"):
                reps.append(Replacement(read.value(r, "file", str, f"variant[{i}].replace[{j}].file", ""),
                                        read.value(r, "old", str, f"variant[{i}].replace[{j}].old", ""),
                                        read.value(r, "new", str, f"variant[{i}].replace[{j}].new", "")))
            variants.append(Variant(name, lang, read.value(v, "step", str, f"variant[{i}].step", ""),
                                    read.value(v, "expect", str, f"variant[{i}].expect", ""),
                                    read.value(v, "weakness", str, f"variant[{i}].weakness"), tuple(reps), where, i))
    return variants


def load_task(root: Path | str) -> Task:
    root = Path(root)
    try:
        meta = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"{root}/task.toml: {exc}") from exc
    found: list[dict] = []
    wrong: set[tuple[str, str]] = set()
    read = _Reader("task.toml", found, wrong)
    for key, kind in TASK_KINDS.items():
        if key in meta and not _of_kind(meta[key], kind):
            read.bad(key, kind)
            meta = {k: v for k, v in meta.items() if k != key}
    if "languages" in meta:
        meta["languages"] = list(read.strings(meta["languages"], "languages"))
    steps = [Step(read.value(s, "name", str, f"steps[{i}].name", ""),
                  read.value(s, "instruction", str, f"steps[{i}].instruction", ""))
             for i, s in read.tables(meta.get("steps"), "steps")]
    tests, manifest_problem = [], None
    try:
        manifest = tomllib.loads((root / "tests/manifest.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        manifest_problem = f"tests/manifest.toml: {exc}"
        found.append(problem("tests/manifest.toml", "", f"cannot be read: {exc}"))
    else:
        read = _Reader("tests/manifest.toml", found, wrong)
        for i, t in read.tables(manifest.get("test"), "test"):
            kind = read.value(t, "kind", str, f"test[{i}].kind", "")
            tests.append(TestFile(
                file=read.value(t, "file", str, f"test[{i}].file", ""), kind=kind,
                step=read.value(t, "step", str, f"test[{i}].step", ""),
                weakness=read.value(t, "weakness", str, f"test[{i}].weakness"),
                fresh_app=read.value(t, "fresh_app", bool, f"test[{i}].fresh_app", kind == "security"),
                superseded_in=read.strings(t.get("superseded_in"), f"test[{i}].superseded_in")))
    variants = _load_variants(root / "solutions" / "variants", found, wrong)
    return Task(root, meta, steps, tests, manifest_problem, variants, None, found, wrong)


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


def task_problems(task: Task) -> list[dict]:
    """Every problem of a task as data: its file, its key and its message."""
    found: list[dict] = list(task.loading)
    m = task.meta

    def said(file: str, key: str) -> bool:
        return (file, key) in task.wrong

    if m.get("schema") != 1 and not said("task.toml", "schema"):
        found.append(problem("task.toml", "schema", "schema must be 1"))
    if not SLUG.match(task.id) and not said("task.toml", "id"):
        found.append(problem("task.toml", "id", "id must be lowercase words joined by hyphens"))
    if not m.get("title") and not said("task.toml", "title"):
        found.append(problem("task.toml", "title", "title is missing"))
    for key, allowed in (("domain", DOMAINS), ("kind", KINDS), ("interface", INTERFACES), ("visibility", VISIBILITY)):
        if m.get(key) not in allowed and not said("task.toml", key):
            found.append(problem("task.toml", key, f"{key} must be one of {sorted(allowed)}"))
    canary = task.canary
    if (not canary.startswith(CANARY_PREFIX) or len(canary) < len(CANARY_PREFIX) + 16) and not said("task.toml", "canary"):
        found.append(problem("task.toml", "canary", f"canary must start with {CANARY_PREFIX} and carry a unique id"))
    if "languages" in m and not task.languages and not said("task.toml", "languages"):
        found.append(problem("task.toml", "languages", "a task needs at least one language"))
    for lang in task.languages:
        if lang not in LANGUAGES:
            found.append(problem("task.toml", "languages", f"unknown language {lang}"))
    if m.get("interface") == "http":
        app = task.app
        if not app and not said("task.toml", "app"):
            found.append(problem("task.toml", "app", "an http task needs an [app] section"))
        elif app and not _of_kind(app.get("port"), int):
            found.append(problem("task.toml", "app.port", "[app] port must be a number"))
    found += cli_problems(task)
    limits = task.limits
    for key in ("agent_timeout_sec", "verifier_timeout_sec", "cpus", "memory_mb"):
        if (not _of_kind(limits.get(key), int) or limits.get(key) <= 0) and not said("task.toml", "limits"):
            found.append(problem("task.toml", f"limits.{key}", f"[limits] {key} must be a positive number"))

    names = [s.name for s in task.steps]
    if not names and not said("task.toml", "steps"):
        found.append(problem("task.toml", "steps", "at least one [[steps]] is needed"))
    seen = set()
    for i, s in enumerate(task.steps):
        if not said("task.toml", f"steps[{i}].name"):
            if not SLUG.match(s.name):
                found.append(problem("task.toml", f"steps[{i}].name",
                                     f"step name {s.name!r} must be lowercase words joined by hyphens"))
            if s.name in seen:
                found.append(problem("task.toml", f"steps[{i}].name", f"duplicate step {s.name}"))
        seen.add(s.name)
        if (not s.instruction or not (task.root / s.instruction).is_file()) and not said("task.toml", f"steps[{i}].instruction"):
            found.append(problem("task.toml", f"steps[{i}].instruction",
                                 f"step {s.name} instruction {s.instruction} does not exist"))
    # every language of the task has its right solution at every step
    for lang in [lang for lang in task.languages if lang in LANGUAGES]:
        for s in task.steps:
            if SLUG.match(s.name) and not task.secure_dir(lang, s.name).is_dir():
                where = f"solutions/secure/{lang}/{s.name}"
                found.append(problem(where, "", f"{where} is missing: every language of the task has its "
                                                f"right solution at every step"))

    if canary:
        for path in _agent_visible_files(task):
            if path.is_file() and canary in path.read_text(encoding="utf-8", errors="replace"):
                rel = path.relative_to(task.root).as_posix()
                where = "spec" if rel.split("/")[0] == "spec" else "instruction"
                found.append(problem(rel, "canary", f"the canary must not appear in an agent {where} file"))

    found += _variant_problems(task, names)
    if task.manifest_problem:
        return found
    listed = set()
    manifest = "tests/manifest.toml"
    for i, t in enumerate(task.tests):
        listed.add(t.file)
        path = task.root / "tests" / t.file
        if t.kind not in TEST_KINDS:
            if not said(manifest, f"test[{i}].kind"):
                found.append(problem(manifest, f"test[{i}].kind", f"{t.file} kind must be one of {list(TEST_KINDS)}"))
        elif not t.file.startswith(t.kind + "/"):
            found.append(problem(manifest, f"test[{i}].file", f"{t.file} must sit in the {t.kind}/ folder"))
        if t.step not in names and not said(manifest, f"test[{i}].step"):
            found.append(problem(manifest, f"test[{i}].step", f"{t.file} step {t.step} is not a step of this task"))
        for s in t.superseded_in:
            if s not in names:
                found.append(problem(manifest, f"test[{i}].superseded_in",
                                     f"{t.file} superseded_in {s} is not a step of this task"))
        if t.kind == "security" and t.weakness not in WEAKNESSES:
            found.append(problem(manifest, f"test[{i}].weakness",
                                 f"{t.file} needs a known weakness, got {t.weakness!r}"))
        if not t.file or not path.is_file():
            if not said(manifest, f"test[{i}].file"):
                found.append(problem(manifest, f"test[{i}].file", f"tests/{t.file} is listed but does not exist"))
        elif canary and canary not in path.read_text(encoding="utf-8", errors="replace"):
            found.append(problem(f"tests/{t.file}", "canary", f"tests/{t.file} must carry the canary string"))
    for kind in TEST_KINDS:
        folder = task.root / "tests" / kind
        if folder.is_dir():
            for path in sorted(folder.rglob("test_*.py")):
                rel = path.relative_to(task.root / "tests").as_posix()
                if rel not in listed:
                    found.append(problem(manifest, "test", f"tests/{rel} is not listed in tests/manifest.toml"))
    return found


def validate(task: Task) -> list[str]:
    """Every problem of a task as the line a person reads: its file and its message."""
    return [f"{p['file']}: {p['message']}" for p in task_problems(task)]


def _variant_problems(task: Task, step_names: list[str]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for v in task.variants:
        label, where, at = v.label, v.source or "solutions/variants", f"variant[{v.index}]"
        if not SLUG.match(v.name) and (where, f"{at}.name") not in task.wrong:
            found.append(problem(where, f"{at}.name", f"variant name {v.name!r} must be lowercase words joined by hyphens"))
        if v.expect not in EXPECTATIONS and (where, f"{at}.expect") not in task.wrong:
            found.append(problem(where, f"{at}.expect",
                                 f"variant {v.name} expect {v.expect!r} must be one of {list(EXPECTATIONS)}"))
        if v.lang not in task.languages:
            found.append(problem(where, f"{at}.lang", f"variant {v.name} language {v.lang} is not a language of this task"))
        if v.step not in step_names:
            if (where, f"{at}.step") not in task.wrong:
                found.append(problem(where, f"{at}.step", f"variant {v.name} step {v.step} is not a step of this task"))
            continue
        key = (v.lang, v.step, label)
        if key in seen:
            found.append(problem(where, f"{at}.name", f"duplicate variant {label} for {v.lang} at step {v.step}"))
        seen.add(key)
        if v.expect == "vulnerable":
            if v.weakness not in WEAKNESSES:
                found.append(problem(where, f"{at}.weakness",
                                     f"vulnerable variant {v.name} needs a known weakness, got {v.weakness!r}"))
            elif task.manifest_problem is None and not any(
                    t.kind == "security" and t.weakness == v.weakness and t.step == v.step for t in task.tests):
                # a test of the same weakness at an earlier step does not do: the variant is written for its step
                found.append(problem(where, f"{at}.weakness", f"variant {v.name} is vulnerable to {v.weakness}, "
                                                               f"but no security test checks {v.weakness} at step {v.step}"))
        base = task.secure_dir(v.lang, v.step)
        if not base.is_dir():
            found.append(problem(where, f"{at}.step",
                                 f"variant {v.name} needs {base.relative_to(task.root)} to start from"))
            continue
        if not v.replacements and (where, f"{at}.replace") not in task.wrong:
            found.append(problem(where, f"{at}.replace", f"variant {v.name} has no replacements"))
        for j, r in enumerate(v.replacements):
            path = base / r.file
            if not r.file or not path.is_file():
                found.append(problem(where, f"{at}.replace[{j}].file",
                                     f"variant {v.name} replaces text in {r.file!r}, which the secure solution lacks"))
                continue
            count = path.read_text(encoding="utf-8").count(r.old) if r.old else 0
            if count == 0:
                found.append(problem(where, f"{at}.replace[{j}].old",
                                     f"variant {v.name}: text to replace not found in {r.file}"))
            elif count > 1:
                found.append(problem(where, f"{at}.replace[{j}].old",
                                     f"variant {v.name}: text to replace occurs more than once in {r.file}"))
    return found


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def cases_mount(task: "Task") -> str:
    """The memory-backed folder the cases live in, as Docker is asked for it: capped at the files a
    case may write and at the number of entries it may hold."""
    return (f"{CASES_DIR}:rw,exec,nosuid,nodev,size={task.cli['case_disk_mb']}m,"
            f"nr_inodes={CASE_ENTRIES},mode=0711")


def memory_terms(cli: dict) -> dict[str, float]:
    """What a container must hold at once while a case runs, in MB, term by term: the smallest memory
    the format checker accepts for the container is their sum."""
    return {"the case's memory": cli["case_memory_mb"],
            "the case's files": cli["case_disk_mb"],
            "what the case runner keeps of the program's output":
                OUTPUT_KEPT_TIMES * cli["case_output_kb"] / 1024 + OUTPUT_KEPT_MB,
            "what the case runner keeps of the standard input a test gives": cli["case_disk_mb"],
            "the judge's own room": JUDGE_ROOM_MB}


def container_memory_mb(cli: dict) -> int:
    """The smallest memory a container of a command-line task may have, in whole MB."""
    return math.ceil(sum(memory_terms(cli).values()))


def judging_seconds(task: Task) -> int:
    """What the format checker adds up for the longest step of a command-line task: every wait and
    every time limit the judge has in a step, term by term as the judge's step_terms gives them. The
    build is counted at BUILD_SECONDS; a task whose settings would give it another limit is refused.
    The tests of a file are counted as pytest collects them; whatever would make the two differ is
    refused by the rules on a test's code."""
    timeout = task.cli.get("case_timeout_sec")
    if not _whole(timeout) or task.manifest_problem:
        return 0
    longest = 0
    for step in task.steps:
        tests_per_file = []
        for t in task.tests_for(step.name):
            path = task.root / "tests" / t.file
            tests_per_file.append(tests_in(path.read_bytes()) if t.file and path.is_file() else 0)
        longest = max(longest, math.ceil(sum(step_terms(tests_per_file, timeout).values())))
    return longest


def cli_problems(task: Task) -> list[dict]:
    """What is wrong with the command-line part of a task, as data: the file, the key and the message."""
    found: list[dict] = []
    cli = task.meta.get("cli")
    if task.meta.get("interface") != "cli":
        if cli is not None:
            found.append(problem("task.toml", "cli", "a [cli] section belongs to a task whose interface is cli"))
        return found
    if "app" in task.meta:
        found.append(problem("task.toml", "app", "a cli task starts no server, so it takes no [app] section"))
    if not isinstance(cli, dict):
        if ("task.toml", "cli") not in task.wrong:
            found.append(problem("task.toml", "cli", "a cli task needs a [cli] section with the limits of one case"))
        return found
    for key, most in CLI_LIMIT_KEYS.items():
        value = cli.get(key)
        if not _whole(value) or value <= 0:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} must be a positive whole number"))
        elif key == "case_processes" and value > most:
            found.append(problem("task.toml", f"cli.{key}",
                                 f"[cli] {key} must be at most {most}: the container's cap on processes "
                                 f"({PROCESS_CAP}) holds the case's processes and the judge's own ({JUDGE_PROCESSES})"))
        elif value > most:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} must be at most {most}"))
    for key in cli:
        if key not in CLI_LIMIT_KEYS:
            found.append(problem("task.toml", f"cli.{key}", f"[cli] {key} is not a limit of a case"))
    room = task.limits.get("memory_mb")
    if all(_whole(cli.get(key)) for key in CLI_LIMIT_KEYS) and _whole(room):
        needed = container_memory_mb(cli)
        if 0 < room < needed:
            terms = ", ".join(f"{name} ({value:g})" for name, value in memory_terms(cli).items())
            found.append(problem("task.toml", "limits.memory_mb",
                                 f"[limits] memory_mb must be at least {needed}: the container's memory holds at "
                                 f"once {terms}"))
    needed, given = judging_seconds(task), task.limits.get("verifier_timeout_sec")
    if needed and _whole(given) and given < needed:
        found.append(problem("task.toml", "limits.verifier_timeout_sec",
                             f"[limits] verifier_timeout_sec must be at least {needed}: every wait and "
                             "time limit of a step, so a program that hangs in every case still gets a result"))
    if all(_whole(cli.get(key)) for key in CLI_LIMIT_KEYS):
        from .convert import verifier_env   # the settings the judge is handed; the converter builds on this module
        for lang in [name for name in task.languages if name in LANGUAGES]:
            given = build_seconds(verifier_env(task, lang))
            if given != BUILD_SECONDS:
                found.append(problem("task.toml", "languages",
                                     f"the judge would give the build in {lang} {given:g} seconds, but the sum of "
                                     f"verifier_timeout_sec counts {BUILD_SECONDS}"))
    found += test_code_problems(task)
    return found


# --- the code of a command-line task's tests, read as Python reads it ---

# The modules of Python's standard library a test may import: they start nothing, open no file by its
# path and reach no attribute by a name given as text.
TEST_MODULES = {"base64", "binascii", "collections", "datetime", "decimal", "fractions", "hashlib", "itertools",
                "json", "math", "random", "re", "struct", "textwrap", "unicodedata", "zlib"}
CASE_RUNNER_NAMES = {"case", "need"}
# The names a test may use without defining them: built-in functions and types that start nothing,
# open no file and reach nothing by a name given as text, and the built-in exceptions.
TEST_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable", "chr", "classmethod", "dict",
    "divmod", "enumerate", "filter", "float", "frozenset", "hash", "hex", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "object", "oct", "ord", "pow", "print", "property", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "staticmethod", "str", "sum", "super", "tuple", "zip",
    "True", "False", "None", "NotImplemented", "Ellipsis",
} | {name for name, value in vars(builtins).items() if isinstance(value, type) and issubclass(value, BaseException)}
# Attributes a test may not reach: a running function's frame or code, attributes reached by a name in a
# format string, the modules a module has loaded, a path's ways of reading, listing, changing or asking
# about the files it names, and making several tests out of one.
REFUSED_ATTRIBUTES = {
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "ag_await",
    "tb_frame", "tb_next", "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "with_traceback",
    "format", "format_map", "vformat",
    "modules",
    "read_text", "read_bytes", "write_text", "write_bytes", "open", "iterdir", "glob", "rglob", "walk", "exists",
    "is_file", "is_dir", "is_symlink", "is_mount", "is_socket", "is_fifo", "is_block_device", "is_char_device",
    "is_junction", "stat", "lstat", "readlink", "resolve", "absolute", "samefile", "chmod", "lchmod", "unlink",
    "rmdir", "rename", "touch", "symlink_to", "hardlink_to", "link_to", "expanduser", "cwd", "copy", "copy_into",
    "move", "move_into", "info",
    "parametrize",
}
# Files pytest runs or obeys on its own, without a test asking for them.
PYTEST_FILES = {"conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"}
# Files Python loads in place of a source file of the same name.
COMPILED = tuple({".pyc", ".pyo", ".pyd", ".so", *importlib.machinery.EXTENSION_SUFFIXES})
# Names a helper may not have: Python would load the module of that name in its place, or run it on its own.
NOT_A_HELPER = set(sys.stdlib_module_names) | {"dodam_cli", "dodam_judge", "conftest", "sitecustomize",
                                               "usercustomize", "pytest", "_pytest", "pluggy", "iniconfig",
                                               "packaging", "py"}
# The one line that puts the task's helper folder on the search path, with any number and folder name.
PATH_LINE = ast.dump(ast.parse("sys.path.insert(0, str(Path(__file__).resolve().parents[0] / 'x'))").body[0])
WHY = ("a test of a cli task runs the program only through dodam_cli, learns nothing about the language "
       "and the judge's settings, and is one case")


def _shape(node: ast.AST) -> str:
    """The dump of a statement with every number and string made the same, to compare its shape."""
    copy = ast.parse(ast.unparse(node)).body[0]
    for part in ast.walk(copy):
        if isinstance(part, ast.Constant):
            part.value = 0 if isinstance(part.value, int) and not isinstance(part.value, bool) else (
                "x" if isinstance(part.value, str) else part.value)
    return ast.dump(copy)


class _TestCode(ast.NodeVisitor):
    """The rules on one Python file of a command-line task's tests."""

    def __init__(self, rel: str, helpers: set[str], path_lines: list[ast.stmt]):
        self.rel, self.helpers, self.path_lines = rel, helpers, path_lines
        self.found: list[dict] = []
        self.defined: set[str] = set()
        self.depth = 0                      # how many functions and lambdas the visitor is inside
        self.calls_case = False
        self.imports_case = False

    def refuse(self, node: ast.AST, what: str) -> None:
        self.found.append(problem(self.rel, f"line {getattr(node, 'lineno', 0)}", f"{what}: {WHY}"))

    def generic_visit(self, node: ast.AST) -> None:
        if any(node is line for line in self.path_lines):
            return                         # the one line that puts the helper folder on the search path
        super().generic_visit(node)

    # imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top == "sys" and alias.name == "sys" and not alias.asname and self.path_lines:
                pass
            elif top in TEST_MODULES or (top in self.helpers and "." not in alias.name):
                pass
            else:
                self.refuse(node, f"imports {alias.name}")
            self.binds(node, alias.asname or top)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            self.refuse(node, "imports relatively")
            return
        for alias in node.names:
            if alias.name == "*":
                self.refuse(node, f"imports every name of {module}")
                continue
            top = module.split(".")[0]
            if module == "dodam_cli":
                if alias.name not in CASE_RUNNER_NAMES or alias.asname:
                    self.refuse(node, f"imports {alias.name} from dodam_cli")
                elif alias.name == "case":
                    self.imports_case = True
            elif module == "pathlib" and alias.name == "Path" and not alias.asname and self.path_lines:
                pass
            elif not (top in TEST_MODULES or (top in self.helpers and "." not in module)):
                self.refuse(node, f"imports {alias.name} from {module}")
            self.binds(node, alias.asname or alias.name)

    def binds(self, node: ast.AST, name: str) -> None:
        self.defined.add(name)
        if name.startswith("test") or name.startswith("pytest_"):
            self.refuse(node, f"binds {name} by an import")

    # names
    def visit_Name(self, node: ast.Name) -> None:
        name = node.id
        if name.startswith("__"):
            self.refuse(node, f"uses {name}")
        elif isinstance(node.ctx, ast.Load):
            if name in ("sys", "Path") and self.path_lines:
                self.refuse(node, f"uses {name} beyond the line that puts the helper folder on the search path")
            elif name not in self.defined and name not in TEST_BUILTINS:
                self.refuse(node, f"uses {name}, which the file does not define and which is no name a test may use")
        else:
            if name.startswith("pytest_"):
                self.refuse(node, f"binds {name}")
            elif name.startswith("test") and (self.depth == 0 or isinstance(node.ctx, ast.Del)):
                self.refuse(node, f"binds {name} at the top of the file in another way than by a def")

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "case":
            self.calls_case = True
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in REFUSED_ATTRIBUTES:
            self.refuse(node, f"reaches .{node.attr}")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            if name.startswith(("test", "pytest_")) or name.startswith("__"):
                self.refuse(node, f"makes {name} global")

    visit_Nonlocal = visit_Global

    # definitions
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.defined.add(node.name)
        if node.name.startswith("pytest_"):
            self.refuse(node, f"defines {node.name}")
        if node.name.startswith("test"):
            if self.depth or not self.at_top(node):
                self.refuse(node, f"defines {node.name} elsewhere than at the top of the file")
            args = node.args
            if args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg:
                self.refuse(node, f"gives {node.name} parameters")
            if node.decorator_list:
                self.refuse(node, f"decorates {node.name}")
        # what is worked out when the function is defined: its decorators, defaults and annotations
        for part in node.decorator_list:
            self.visit(part)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for part in getattr(node, "type_params", ()):
            self.visit(part)
        self.depth += 1
        for statement in node.body:
            self.visit(statement)
        self.depth -= 1

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
            if arg is not None:
                self.defined.add(arg.arg)
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.refuse(node, f"defines {node.name} as asynchronous")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.defined.add(node.name)
        if node.name.startswith("Test"):
            self.refuse(node, f"defines the class {node.name}, which pytest would collect")
        self.generic_visit(node)

    def visit_match_case(self, node: ast.match_case) -> None:
        # a name a pattern captures at the top of the file is a name of the module, as an assignment's is
        for part in ast.walk(node.pattern):
            name = getattr(part, "name", None) or getattr(part, "rest", None)
            if isinstance(name, str) and name.startswith(("test", "pytest_")) and self.depth == 0:
                self.refuse(part, f"binds {name} by a pattern")
        self.generic_visit(node)

    def at_top(self, node: ast.AST) -> bool:
        return node in self.top

    def check(self, tree: ast.Module) -> None:
        self.top = set(tree.body)
        # every name the file defines, wherever it does: a name used before its definition is no problem here
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                self.defined.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                self.defined.add(node.name)
            elif isinstance(node, ast.arg):
                self.defined.add(node.arg)
            elif isinstance(node, ast.alias):
                self.defined.add(node.asname or node.name.split(".")[0])
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                self.defined.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                self.defined.add(node.rest)
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
        for name in sorted({n for n in names if names.count(n) > 1}):
            self.found.append(problem(self.rel, "", f"defines {name} more than once: {WHY}"))
        self.visit(tree)


def test_code_problems(task: Task) -> list[dict]:
    """The rules on the code of a command-line task's tests, for every Python file of its tests folder."""
    tests = task.root / "tests"
    if not tests.is_dir():
        return []
    found: list[dict] = []
    every = sorted(p for p in tests.rglob("*") if p.is_file() or p.is_dir())
    helpers = {p.stem for p in every if p.is_file() and p.suffix == ".py" and p.stem != "__init__"} - NOT_A_HELPER
    listed = {f"tests/{t.file}" for t in task.tests}
    for path in every:
        rel = f"tests/{path.relative_to(tests).as_posix()}"
        if path.is_dir():
            continue
        if path.name in PYTEST_FILES:
            found.append(problem(rel, "", f"pytest runs or obeys {path.name} without a test asking for it: {WHY}"))
            continue
        if path.name.endswith(COMPILED):
            found.append(problem(rel, "", f"Python would load this file in place of a source file: {WHY}"))
            continue
        if path.suffix != ".py":
            continue
        if path.stem in NOT_A_HELPER:
            found.append(problem(rel, "", f"Python would load or run the module {path.stem} in place of this file: {WHY}"))
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=rel)
        except (SyntaxError, ValueError) as exc:
            found.append(problem(rel, f"line {getattr(exc, 'lineno', 0) or 0}", f"cannot be read as Python: {exc}"))
            continue
        path_lines = [node for node in tree.body if isinstance(node, ast.Expr) and _shape(node) == PATH_LINE]
        code = _TestCode(rel, helpers, path_lines)
        code.check(tree)
        found += code.found
        if rel in listed and not (code.imports_case and code.calls_case):
            found.append(problem(rel, "", "each file of tests imports case from dodam_cli and calls it: "
                                          "a test of a cli task runs the program through dodam_cli"))
    return found


def diagnostics(task: Task) -> list[dict]:
    """Every problem of a task as data. The text a person reads is made from this."""
    return [{"severity": "error", **p} for p in task_problems(task)]


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
                        "problems": [{"severity": "error", "file": "task.toml", "key": "", "message": str(exc)}]}, 2
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
