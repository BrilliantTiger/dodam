"""The format rules of a command-line task, and the checker's JSON-first diagnostics."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CANARY, edit, write
from dodambench.task import load_task, validate

BENCH = Path(__file__).resolve().parents[1]
SAMPLE = BENCH / "tasks" / "cli-csv-json"


def errors_of(root: Path) -> list[str]:
    return validate(load_task(root))


def check(root: Path, *flags: str) -> subprocess.CompletedProcess:
    """Run the format checker as a command, the way a person or a script runs it."""
    return subprocess.run([sys.executable, "-m", "dodambench.task", str(root), *flags],
                          capture_output=True, text=True, cwd=BENCH)


def report_of(done: subprocess.CompletedProcess) -> dict:
    assert done.stdout.lstrip().startswith("{"), f"the checker printed no JSON: {done.stdout!r} {done.stderr[-300:]!r}"
    return json.loads(done.stdout)


def test_a_valid_command_line_task_has_no_errors(cli_task_dir):
    assert errors_of(cli_task_dir) == []


def test_the_sample_command_line_task_is_accepted():
    assert errors_of(SAMPLE) == []
    task = load_task(SAMPLE)
    assert task.meta["interface"] == "cli"
    assert [s.name for s in task.steps] == ["build", "change-1"]
    assert {t.kind for t in task.tests} == {"functional", "boundary", "security"}


def test_a_command_line_task_needs_the_limits_of_a_case(cli_task_dir):
    toml = cli_task_dir / "task.toml"
    text = toml.read_text()
    start, end = text.index("[cli]"), text.index("[limits]")
    toml.write_text(text[:start] + text[end:])
    assert any("[cli]" in e for e in errors_of(cli_task_dir))


def test_every_limit_of_a_case_must_be_a_positive_whole_number(cli_task_dir):
    for key in ("case_timeout_sec", "case_memory_mb", "case_output_kb", "case_processes", "case_disk_mb"):
        for bad in ("0", "-3", '"ten"', "1.5", "true"):
            root = cli_task_dir.parent / f"{key}-{abs(hash(bad))}"
            if not root.exists():
                import shutil
                shutil.copytree(cli_task_dir, root)
            toml = root / "task.toml"
            lines = [f"{key} = {bad}" if line.startswith(key + " =") else line for line in toml.read_text().splitlines()]
            toml.write_text("\n".join(lines) + "\n")
            assert any(key in e for e in errors_of(root)), (key, bad)


def test_a_missing_limit_and_an_unknown_one_are_reported(cli_task_dir):
    edit(cli_task_dir / "task.toml", "case_processes = 8\n", "case_threads = 8\n")
    errors = errors_of(cli_task_dir)
    assert any("case_processes" in e for e in errors) and any("case_threads" in e for e in errors)


def test_the_containers_memory_must_hold_the_case_its_files_and_the_judge(cli_task_dir):
    """The case's files live in memory, so the container's memory is the case's memory, its files and the judge."""
    assert errors_of(cli_task_dir) == []
    edit(cli_task_dir / "task.toml", "case_memory_mb = 256", "case_memory_mb = 1785")   # 1785 + 8 + 256 > 2048
    errors = errors_of(cli_task_dir)
    assert any("memory_mb must be at least 2049" in e for e in errors), errors


def test_a_command_line_task_takes_no_server_settings(cli_task_dir):
    edit(cli_task_dir / "task.toml", "[limits]", "[app]\nport = 8080\n\n[limits]")
    assert any("[app]" in e for e in errors_of(cli_task_dir))


def test_a_web_task_takes_no_case_limits(task_dir):
    edit(task_dir / "task.toml", "[limits]", "[cli]\ncase_timeout_sec = 5\n\n[limits]")
    assert any("[cli]" in e for e in errors_of(task_dir))


def test_the_judge_must_have_time_for_a_program_that_hangs_in_every_case(cli_task_dir):
    """Every test may use its whole time limit, so the verifier's time must cover the build and all of them."""
    edit(cli_task_dir / "task.toml", "verifier_timeout_sec = 1800", "verifier_timeout_sec = 900")
    assert any("verifier_timeout_sec" in e and "hangs" in e for e in errors_of(cli_task_dir))


def test_a_hidden_test_may_not_start_the_program_itself(cli_task_dir):
    for word in ("import subprocess", "os.system('x')", "os.execv('x', [])", "os.fork()", "os.popen('x')"):
        path = cli_task_dir / "tests/functional/test_upper.py"
        original = path.read_text()
        path.write_text(original + f"\n{word}\n")
        assert any("test_upper.py" in e and "dodam_cli" in e for e in errors_of(cli_task_dir)), word
        path.write_text(original)
    assert errors_of(cli_task_dir) == []


def test_a_hidden_test_may_not_ask_which_language_it_judges(cli_task_dir):
    path = cli_task_dir / "tests/functional/test_upper.py"
    original = path.read_text()
    for word in ("open('/tests/lang.env')", "os.environ['DODAM_CLI_CONFIG']"):
        path.write_text(original + f"\n{word}\n")
        assert any("test_upper.py" in e and "language" in e for e in errors_of(cli_task_dir)), word


def test_a_hidden_test_must_use_the_case_runner_and_may_not_multiply_itself(cli_task_dir):
    write(cli_task_dir, "tests/boundary/test_usage.py", f"# {CANARY}\ndef test_nothing():\n    assert True\n")
    assert any("test_usage.py" in e and "dodam_cli" in e for e in errors_of(cli_task_dir))
    path = cli_task_dir / "tests/functional/test_upper.py"
    path.write_text(path.read_text() + "\nimport pytest\npytest.mark.parametrize\n")
    assert any("test_upper.py" in e and "parametrize" in e for e in errors_of(cli_task_dir))


def test_a_security_test_of_overwriting_is_a_known_weakness(cli_task_dir):
    manifest = cli_task_dir / "tests/manifest.toml"
    manifest.write_text(manifest.read_text().replace('weakness = "path-traversal"', 'weakness = "file-overwrite"'))
    assert not any("weakness" in e and "manifest" in e for e in errors_of(cli_task_dir))


def test_the_checker_prints_json_and_accepts_the_sample():
    done = check(SAMPLE)
    report = report_of(done)
    assert done.returncode == 0, done.stdout
    assert report == {"task": "cli-csv-json", "ok": True, "problems": []}


def test_the_checker_refuses_a_malformed_command_line_task_with_json_first(cli_task_dir):
    edit(cli_task_dir / "task.toml", "case_timeout_sec = 2", "case_timeout_sec = 0")
    edit(cli_task_dir / "task.toml", "case_disk_mb = 8\n", "")
    done = check(cli_task_dir)
    assert done.returncode == 1
    report = report_of(done)
    assert report["task"] == "cli-demo" and report["ok"] is False
    by_key = {p.get("key"): p for p in report["problems"]}
    assert by_key["cli.case_timeout_sec"]["file"] == "task.toml"
    assert by_key["cli.case_timeout_sec"]["severity"] == "error"
    assert "positive whole number" in by_key["cli.case_timeout_sec"]["message"]
    assert "cli.case_disk_mb" in by_key
    # the lines a person reads are made from the same data
    text = check(cli_task_dir, "--text")
    assert text.returncode == 1
    for problem in report["problems"]:
        assert problem["message"] in text.stdout
    assert not text.stdout.lstrip().startswith("{")


def test_the_checker_reports_a_task_it_cannot_read_as_json(tmp_path):
    write(tmp_path, "broken/task.toml", "schema = = 1\n")
    done = check(tmp_path / "broken")
    assert done.returncode == 2
    report = report_of(done)
    assert report["ok"] is False and report["problems"][0]["file"] == "task.toml"


# --- every problem in the form the document gives: its file, its key and its message ---

def test_every_problem_has_its_file_key_and_message(cli_task_dir):
    """A task with many problems at once: each comes out as a problem of its own, with the file and
    the key it is about, and no problem's message holds another's."""
    toml = cli_task_dir / "task.toml"
    edit(toml, "schema = 1", "schema = 2")
    edit(toml, 'title = "Demo tool"\n', "")
    edit(toml, 'domain = "cli"', 'domain = "games"')
    edit(toml, "agent_timeout_sec = 1800", "agent_timeout_sec = 0")
    (cli_task_dir / "steps/change-1/instruction.md").unlink()
    write(cli_task_dir, "tests/boundary/test_more.py", f"# {CANARY}\n")
    write(cli_task_dir, "solutions/variants/go.toml", '''
        [[variant]]
        name = "one"
        lang = "python"

        [[variant]]
        name = "two"
        lang = "rust"
    ''')
    report = report_of(check(cli_task_dir))
    found = {(p["file"], p.get("key")): p["message"] for p in report["problems"]}
    for where in [("task.toml", "schema"), ("task.toml", "title"), ("task.toml", "domain"),
                  ("task.toml", "limits.agent_timeout_sec"), ("task.toml", "steps[1].instruction"),
                  ("tests/manifest.toml", "test")]:
        assert where in found, (where, sorted(found))
    variants = [p for p in report["problems"] if p["file"] == "solutions/variants/go.toml"]
    assert len(variants) == 2 and {p["key"] for p in variants} == {"variant[0].lang", "variant[1].lang"}, variants
    assert all("key" in p and p["key"] is not None and p["message"] for p in report["problems"]), report
    messages = [p["message"] for p in report["problems"]]
    for one in messages:
        assert not [other for other in messages if other is not one and one in other and one != other], one


WRONG_KIND = [
    ("task.toml", 'title = "Demo tool"', "title = 5", "task.toml", "title"),
    ("task.toml", 'domain = "cli"', 'domain = ["cli"]', "task.toml", "domain"),
    ("task.toml", 'languages = ["python", "go"]', "languages = 123", "task.toml", "languages"),
    ("task.toml", 'languages = ["python", "go"]', 'languages = ["python", 7]', "task.toml", "languages[1]"),
    ("task.toml", "[limits]\n", "limits = 1\n[unused]\n", "task.toml", "limits"),
    ("task.toml", "[cli]\n", "cli = 1\n[unused]\n", "task.toml", "cli"),
    ("task.toml", '[[steps]]\nname = "build"', 'steps = 1\n[[unused]]\nname = "build"', "task.toml", "steps"),
    ("task.toml", 'name = "build"', "name = 1", "task.toml", "steps[0].name"),
    ("tests/manifest.toml", 'kind = "boundary"', "kind = 3", "tests/manifest.toml", "test[1].kind"),
    ("tests/manifest.toml", 'step = "change-1"\n\n', 'step = "change-1"\nsuperseded_in = "build"\n\n',
     "tests/manifest.toml", "test[2].superseded_in"),
    ("tests/manifest.toml", 'file = "functional/test_upper.py"', 'file = "functional/test_upper.py"\nfresh_app = "yes"',
     "tests/manifest.toml", "test[0].fresh_app"),
    ("solutions/variants/python.toml", 'name = "lower"', "name = 9", "solutions/variants/python.toml", "variant[1].name"),
    ("solutions/variants/python.toml", 'old = ".upper()"', "old = 1", "solutions/variants/python.toml",
     "variant[1].replace[0].old"),
]


@pytest.mark.parametrize("rel, old, new, file, key", WRONG_KIND, ids=[f"{w[4]}" for w in WRONG_KIND])
def test_a_value_of_the_wrong_kind_is_a_problem_and_no_trace_of_an_error(cli_task_dir, rel, old, new, file, key):
    edit(cli_task_dir / rel, old, new)
    done = check(cli_task_dir)
    assert "Traceback" not in done.stderr, done.stderr[-1500:]
    assert done.returncode == 1, (done.returncode, done.stdout[-500:], done.stderr[-500:])
    report = report_of(done)
    assert [p for p in report["problems"] if p["file"] == file and p.get("key") == key], report["problems"]


@pytest.mark.parametrize("whole, new", [("test", 'test = "bad"\n'), ("variant", 'variant = "bad"\n')])
def test_a_table_of_the_wrong_kind_is_a_problem_and_no_trace_of_an_error(cli_task_dir, whole, new):
    rel = "tests/manifest.toml" if whole == "test" else "solutions/variants/python.toml"
    (cli_task_dir / rel).write_text(new, encoding="utf-8")
    done = check(cli_task_dir)
    assert "Traceback" not in done.stderr and done.returncode == 1, done.stderr[-1500:]
    assert [p for p in report_of(done)["problems"] if p["file"] == rel and p.get("key") == whole]


def test_a_replacement_of_the_wrong_kind_is_a_problem_and_no_trace_of_an_error(cli_task_dir):
    edit(cli_task_dir / "solutions/variants/python.toml", '[[variant.replace]]\n        file = "main.py"\n        old = ".upper()"',
         'replace = "bad"\n\n        [[variant.unused]]\n        file = "main.py"\n        old = ".upper()"')
    done = check(cli_task_dir)
    assert "Traceback" not in done.stderr and done.returncode == 1, done.stderr[-1500:]
    assert [p for p in report_of(done)["problems"] if p.get("key") == "variant[1].replace"], report_of(done)


def test_a_task_the_checker_cannot_read_still_says_which_key(tmp_path):
    write(tmp_path, "broken/task.toml", "schema = = 1\n")
    report = report_of(check(tmp_path / "broken"))
    assert report["problems"][0]["file"] == "task.toml" and "key" in report["problems"][0]


# --- what the document says a task must have ---

def test_a_task_without_a_language_is_refused(cli_task_dir):
    edit(cli_task_dir / "task.toml", 'languages = ["python", "go"]', "languages = []")
    assert [p for p in report_of(check(cli_task_dir))["problems"] if p.get("key") == "languages"]


@pytest.mark.parametrize("missing", ["solutions/secure/go/change-1", "solutions/secure/python/build",
                                     "solutions/secure/go"])
def test_a_task_without_the_right_solution_of_a_language_and_step_is_refused(cli_task_dir, missing):
    import shutil
    shutil.rmtree(cli_task_dir / missing)
    report = report_of(check(cli_task_dir))
    assert [p for p in report["problems"] if missing.split("/")[2] in p["message"] and "secure" in p["message"]], report


def test_a_vulnerable_variant_needs_a_security_test_of_its_weakness_at_its_own_step(cli_task_dir):
    """A test of the same weakness at an earlier step does not do: the variant is written for its step."""
    assert errors_of(cli_task_dir) == []
    edit(cli_task_dir / "tests/manifest.toml", 'file = "security/test_copy_outside.py"\nkind = "security"\nstep = "change-1"',
         'file = "security/test_copy_outside.py"\nkind = "security"\nstep = "build"')
    errors = errors_of(cli_task_dir)
    assert any("copies-anywhere" in e and "change-1" in e for e in errors), errors


# --- the smallest container the checker accepts, term by term ---

# What a container must hold at once while a case runs, in MB, as the code has it: the case's memory;
# the case's files, which live in memory; what the case runner keeps of the program's output, at most
# four times the limit on output and 1 MB; the standard input a test gives, at most the case's file
# limit; and the judge's own room of 256 MB, half of which is the line below which the judge ends a
# program.
def memory_terms(memory_mb: int, disk_mb: int, output_kb: int) -> dict[str, float]:
    return {"the case's memory": memory_mb,
            "the case's files": disk_mb,
            "what the case runner keeps of the program's output": 4 * output_kb / 1024 + 1,
            "what the case runner keeps of the standard input a test gives": disk_mb,
            "the judge's own room": 256}


def memory_refusals(root: Path) -> list[dict]:
    from dodambench.task import cli_problems
    return [p for p in cli_problems(load_task(root)) if p["key"] == "limits.memory_mb"]


@pytest.mark.parametrize("term", list(memory_terms(1, 1, 1)))
def test_the_checker_refuses_a_container_memory_that_leaves_out_any_term(cli_task_dir, term):
    """The smallest memory the checker accepts for a container holds everything a case and the judge
    can hold at once. One term short is refused, whichever term it is; the whole sum is accepted."""
    toml = cli_task_dir / "task.toml"
    edit(toml, "case_output_kb = 64", "case_output_kb = 1024")
    terms = memory_terms(256, 8, 1024)
    whole = sum(terms.values())
    given = toml.read_text(encoding="utf-8")
    toml.write_text(given.replace("memory_mb = 2048", f"memory_mb = {int(whole - terms[term])}"), encoding="utf-8")
    assert memory_refusals(cli_task_dir), f"a container without {term!r} ({terms[term]} MB) was accepted"
    toml.write_text(given.replace("memory_mb = 2048", f"memory_mb = {int(whole)}"), encoding="utf-8")
    assert not memory_refusals(cli_task_dir), terms


@pytest.mark.parametrize("term", list(memory_terms(1, 1, 1)))
def test_vetting_refuses_a_machine_whose_container_leaves_out_any_term(cli_task_dir, term):
    import json as json_module

    import dodambench.vet as vetting
    edit(cli_task_dir / "task.toml", "case_output_kb = 64", "case_output_kb = 1024")
    terms = memory_terms(256, 8, 1024)
    whole = int(sum(terms.values()))
    task = load_task(cli_task_dir)
    assert vetting.container_room(task, whole * 4 // 3 + 2)["memory_mb"] >= whole
    with pytest.raises(vetting.VetError) as refused:
        vetting.container_room(task, int((whole - terms[term]) * 4 / 3))
    assert json_module.loads(str(refused.value))["key"] == "limits.memory_mb"


def test_the_container_leaves_the_judge_room_above_the_cases_processes(cli_task_dir):
    """The container's cap on processes holds a case's processes and the judge's own: three while a
    case runs (the verifier's script, the judge and the case runner) and one more to end what a
    program left. The checker keeps twice those four, eight."""
    from dodambench.task import PROCESS_CAP, cli_problems
    toml = cli_task_dir / "task.toml"
    given = toml.read_text(encoding="utf-8")
    for processes, refused in ((PROCESS_CAP, True), (PROCESS_CAP - 7, True), (PROCESS_CAP - 8, False), (64, False)):
        toml.write_text(given.replace("case_processes = 8", f"case_processes = {processes}"), encoding="utf-8")
        said = [p for p in cli_problems(load_task(cli_task_dir)) if p["key"] == "cli.case_processes"]
        assert bool(said) == refused, (processes, said)


def test_every_task_of_the_repository_fits_both_sums():
    for root in sorted((BENCH / "tasks").iterdir()):
        if (root / "task.toml").is_file():
            assert errors_of(root) == [], root.name
