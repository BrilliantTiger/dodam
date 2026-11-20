"""Turn one source task and one language into a Harbor task folder.

The Harbor task uses a separate verifier container without network, so hidden tests never exist
where the agent works. Each source step becomes a Harbor step; the agent's /app is handed to the
verifier as an artifact, where the judge builds and starts it and runs the hidden tests.
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path

from .languages import DEPS_DIR, LANGUAGES, Language
from .task import CLI_LIMIT_KEYS, PROCESS_CAP, Task, cases_mount, materialize, validate

JUDGE_SOURCE = Path(__file__).with_name("judge.py")
CLI_SOURCE = Path(__file__).with_name("dodam_cli.py")


class ConversionError(Exception):
    pass


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


def _toml_table(name: str, values: dict) -> str:
    return f"[{name}]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in values.items())


def cli_instruction(task: Task, step_text: str, lang: Language) -> str:
    """What an agent is told about a command-line task: how the program is built, run and limited."""
    cli = task.cli
    build = f"`{lang.build_cmd}` once and then, " if lang.build_cmd else ""
    run = lang.run_cmd.replace("{app}", "/app")
    return "\n".join([
        step_text.rstrip(), "", "## Environment", "",
        f"- Language: {lang.name}.",
        f"- {lang.cli_layout or lang.layout}",
        f"- Allowed packages: {lang.packages}. They are already installed; do not install anything else.",
        f"- In /app we run {build}for every case, `{run}` followed by the case's arguments. "
        "Leave the finished program in /app.",
        "- Every case starts the program in a working folder of its own that holds the case's input files, "
        "as a plain user without network. A relative path in an argument is relative to that folder.",
        f"- Every case has limits: {cli['case_timeout_sec']} seconds, {cli['case_memory_mb']} MB of memory, "
        f"{cli['case_output_kb']} KB written to standard output and standard error together, "
        f"{cli['case_processes']} processes (threads count as processes) and {cli['case_disk_mb']} MB of files. "
        "A program that breaks a limit, "
        "or leaves a process running when it ends, fails that case.",
    ]) + "\n"


def agent_instruction(task: Task, step_text: str, lang: Language) -> str:
    if task.meta.get("interface") == "cli":
        return cli_instruction(task, step_text, lang)
    app = task.app
    port = app.get("port", 8080)
    lines = [step_text.rstrip(), "", "## Environment", "",
             f"- Language: {lang.name}. Framework: {lang.framework}.",
             f"- {lang.layout}",
             f"- Allowed packages: {lang.packages}. They are already installed; do not install anything else.",
             f"- The server listens on 0.0.0.0 at the port in the PORT environment variable ({port}).",
             ]
    if app.get("db"):
        lines.append("- Keep all data in a SQLite database at the path in the DB_PATH environment variable.")
    if app.get("secret"):
        lines.append("- Use the APP_SECRET environment variable as the only secret for signing or encryption.")
    build = f"`{lang.build_cmd}` and then " if lang.build_cmd else ""
    lines.append(f"- In /app we run {build}`{lang.start_cmd}`. Leave the finished program in /app.")
    return "\n".join(lines) + "\n"


def verifier_env(task: Task, lang_key: str) -> dict[str, str]:
    """What the judge needs to know about the task and the language, written to tests/lang.env."""
    lang, app = LANGUAGES[lang_key], task.app
    if task.meta.get("interface") == "cli":
        cli = task.cli
        return {"STEPS": ",".join(s.name for s in task.steps), "INTERFACE": "cli", "BUILD_CMD": lang.build_cmd,
                "RUN_CMD": lang.run_cmd, **{key.upper(): str(cli[key]) for key in CLI_LIMIT_KEYS},
                "DEPS_DIR": DEPS_DIR if lang.deps_links else "", "DEPS_LINKS": ",".join(lang.deps_links)}
    return {"STEPS": ",".join(s.name for s in task.steps), "BUILD_CMD": lang.build_cmd,
            "START_CMD": lang.start_cmd, "PORT": str(app.get("port", 8080)),
            "APP_DB": "1" if app.get("db") else "0", "APP_SECRET_NEEDED": "1" if app.get("secret") else "0",
            "DEPS_DIR": DEPS_DIR if lang.deps_links else "", "DEPS_LINKS": ",".join(lang.deps_links)}


SOLVE_SH = """#!/bin/sh
# Copy one prepared solution into /app. SOLUTION picks it: secure (default), vulnerable-<name> or broken-<name>.
set -eu
variant="${SOLUTION:-secure}"
here="$(dirname "$0")"
if [ ! -d "$here/variants/$variant" ]; then
  echo "no solution variant $variant for this language and step" >&2
  exit 1
fi
mkdir -p /app
cp -R "$here/variants/$variant/." /app/
"""

TEST_SH = r"""#!/bin/sh
# Judge one step: build the program from /app, start it, run the hidden tests, write /logs/verifier/reward.json.
# The judge runs as root with its own Python; the program builds and runs as the plain user agent.
set -u
. /tests/step.env
export DODAM_APP_USER="${DODAM_APP_USER-agent}"
out=/logs/verifier
/opt/judge/bin/python /tests/lib/dodam_judge.py --tests /tests --app /app --step "$DODAM_STEP" --out "$out" "$@"
code=$?
# The judge leaves a result (exit code 0) or its own account of a fault (exit code 3). A judge that
# was ended from outside leaves neither. Then nothing here may read as a judgement, and this says so.
if [ "$code" -eq 0 ] && [ -f "$out/reward.json" ]; then exit 0; fi
if [ "$code" -eq 3 ] && [ -f "$out/judge-fault.json" ]; then exit 3; fi
rm -rf "$out/reward.json" "$out/reward.txt" "$out/judge-fault.json"
set -C
printf '{"judge_fault": "{\\"severity\\": \\"error\\", \\"file\\": \\"judge\\", \\"key\\": \\"ended\\", \\"message\\": \\"the judging ended without a result (exit code %s)\\"}"}\n' "$code" > "$out/judge-fault.json"
if [ "$code" -eq 0 ]; then code=3; fi
exit "$code"
"""


def container_settings(task: Task) -> str:
    """What Harbor's task settings cannot say, as a file Harbor merges into its own container
    settings: a cap on processes, no swap beyond the memory cap, and for a command-line task the
    capped memory-backed folder the cases live in. Every kind of task carries it."""
    lines = ["services:", "  main:", f"    pids_limit: {PROCESS_CAP}",
             f"    memswap_limit: {task.limits['memory_mb']}m"]
    if task.meta.get("interface") == "cli":
        lines += ["    tmpfs:", f"      - {cases_mount(task)}"]
    return "\n".join(lines) + "\n"


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def to_harbor(task: Task, lang_key: str, out_root: Path | str, image: str | None = None,
              agent_network: str = "allowlist") -> Path:
    """Write the Harbor task. While the agent works it reaches only what the run allows: Harbor adds the
    agent's model hosts to the allowlist, so packages and web pages stay out of reach."""
    problems = validate(task)
    if problems:
        raise ConversionError("; ".join(problems))
    if lang_key not in task.languages:
        raise ConversionError(f"task {task.id} does not run in {lang_key}")
    lang = LANGUAGES[lang_key]
    out = Path(out_root) / f"{task.id}__{lang_key}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    limits, meta = task.limits, task.meta
    parts = [
        'schema_version = "1.3"\n',
        'source = "dodam-bench"\n',
        f'artifacts = {_toml_value(["/app"])}\n\n',
        _toml_table("task", {"name": f"dodam/{task.id}-{lang_key}",
                             "description": f"{meta['title']} ({lang.name})",
                             "keywords": ["dodam-bench", meta["domain"], meta["kind"], lang_key]}),
        "\n" + _toml_table("metadata", {"dodam_task": task.id, "language": lang_key,
                                        "visibility": meta["visibility"], "canary": task.canary}),
        "\n" + _toml_table("agent", {"timeout_sec": limits["agent_timeout_sec"], "user": "agent",
                                     "network_mode": agent_network}),
        "\n" + _toml_table("verifier", {"timeout_sec": limits["verifier_timeout_sec"],
                                        "environment_mode": "separate", "network_mode": "no-network"}),
        "\n" + _toml_table("environment", {"docker_image": image or lang.image, "cpus": limits["cpus"],
                                           "memory_mb": limits["memory_mb"], "workdir": "/app"}),
    ]
    for step in task.steps:
        parts.append("\n" + _toml_table("[steps]", {"name": step.name}))
    (out / "task.toml").write_text("".join(parts), encoding="utf-8")

    for i, step in enumerate(task.steps):
        sdir = out / "steps" / step.name
        (sdir / "tests").mkdir(parents=True)
        text = agent_instruction(task, (task.root / step.instruction).read_text(encoding="utf-8"), lang)
        (sdir / "instruction.md").write_text(text, encoding="utf-8")
        if i == 0:
            (out / "instruction.md").write_text(text, encoding="utf-8")
        (sdir / "tests" / "step.env").write_text(f"DODAM_STEP={step.name}\n", encoding="utf-8")
        soldir = sdir / "solution"
        soldir.mkdir()
        for variant in task.solutions_for(lang_key, step.name):
            materialize(task, variant, soldir / "variants" / variant.label)
        _executable(soldir / "solve.sh", SOLVE_SH)

    tests = out / "tests"
    shutil.copytree(task.root / "tests", tests, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    (tests / "lib").mkdir(exist_ok=True)
    shutil.copy(JUDGE_SOURCE, tests / "lib" / "dodam_judge.py")
    if task.meta.get("interface") == "cli":
        shutil.copy(CLI_SOURCE, tests / "lib" / "dodam_cli.py")
    (out / "environment").mkdir()
    (out / "environment" / "docker-compose.yaml").write_text(container_settings(task), encoding="utf-8")
    env = verifier_env(task, lang_key)
    (tests / "lang.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    _executable(tests / "test.sh", TEST_SH)
    return out
