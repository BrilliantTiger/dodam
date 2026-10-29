"""Source task format: load a language-neutral task folder and check it against the format rules."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .languages import LANGUAGES

DOMAINS = {"web", "cli", "data", "net", "sys"}
KINDS = {"new", "add-feature", "schema-change", "concurrency", "package-lure"}
INTERFACES = {"http", "cli", "file"}
VISIBILITY = {"public", "private"}
TEST_KINDS = ("functional", "boundary", "security")
WEAKNESSES = {
    "sql-injection", "xss", "command-injection", "code-injection", "path-traversal", "ssrf",
    "broken-access-control", "broken-authentication", "token-forgery", "plaintext-credentials",
    "secret-exposure", "log-injection", "resource-exhaustion", "unsafe-upload", "weak-crypto",
    "missing-rate-limit", "input-validation", "error-handling", "race-condition",
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


@dataclass
class Task:
    root: Path
    meta: dict
    steps: list[Step]
    tests: list[TestFile]
    manifest_problem: str | None

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
    return Task(root, meta, steps, tests, problem)


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
