"""Source task format: load a language-neutral task folder and check it against the format rules."""
from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
