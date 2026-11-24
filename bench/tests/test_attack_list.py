"""The attack list and the list of protections are files in the repository; these keep them honest."""
import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1]
DOCS = BENCH.parent / "docs/benchmark"


def listed(name: str, key: str) -> list[dict]:
    path = BENCH / "experiments" / name
    assert path.is_file(), f"{name} is not in the repository"
    return tomllib.loads(path.read_text(encoding="utf-8"))[key]


def test_exists(node: str) -> bool:
    """Whether a test named like pytest names it is in its file: the function and, if given, the case."""
    file, _, rest = node.partition("::")
    function, _, case = rest.partition("[")
    path = BENCH / file
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return f"def {function}(" in text and (not case or f'"{case[:-1]}"' in text)


test_exists.__test__ = False


def test_every_attack_on_the_list_is_a_test():
    attacks = listed("attacks.toml", "attack")
    assert len(attacks) >= 60
    for attack in attacks:
        assert attack["class"] and attack["tries"] and attack["must"], attack["name"]
        assert attack["tests"], f"{attack['name']} is no test"
        for node in attack["tests"]:
            assert test_exists(node), f"{attack['name']}: there is no test {node}"
    classes = {attack["class"] for attack in attacks}
    for needed in ("learning the judging", "memory", "files", "processes", "the judge's own clean-up",
                   "passing as unjudged", "a container without bounds", "a kill while a case runs"):
        assert needed in classes, needed


def test_every_protection_has_a_broken_variant_that_names_a_test():
    protections = listed("protections.toml", "protection")
    assert len(protections) >= 80
    assert len({p["what"] for p in protections}) == len(protections)
    for protection in protections:
        text = (BENCH / protection["file"]).read_text(encoding="utf-8")
        assert protection["change"], protection["what"]
        for change in protection["change"]:
            assert change["find"] in text, f"{protection['what']}: its variant no longer fits the code"
            assert change["find"] != change["put"]
        assert protection["tests"] and all(test_exists(node) for node in protection["tests"]), protection["what"]
        # a second line of defence says which other protections already stop the attack
        assert protection.get("second_line", "x").strip(), protection["what"]


def test_the_document_names_exactly_the_protections_that_have_a_variant():
    """Each protection the document names has a broken variant, and each variant is named in the document."""
    protections = {p["what"] for p in listed("protections.toml", "protection")}
    text = (DOCS / "command-line-tasks.en.md").read_text(encoding="utf-8")
    heading = "## Every protection, and the test that fails without it"
    assert heading in text, "the document does not list the protections"
    start = text.index(heading)
    section = text[start:text.index("\n## ", start + 10)]
    named = {cells[0].strip() for cells in (line.strip("|").split("|") for line in section.splitlines()
                                             if line.startswith("|")) if cells[0].strip() not in ("Protection", "")
             and not set(cells[0].strip()) <= {"-", " "}}
    assert named == protections, (sorted(named - protections), sorted(protections - named))
    for protection in listed("protections.toml", "protection"):
        if protection.get("second_line"):
            row = next(line for line in section.splitlines() if line.startswith(f"| {protection['what']} |"))
            assert "second line" in row.lower(), f"the document does not say that this is a second line: {protection['what']}"
    korean = (DOCS / "command-line-tasks.ko.md").read_text(encoding="utf-8")
    assert "## 보호 장치 하나하나와" in korean, "the Korean document does not list the protections"
    rows = [line for line in korean[korean.index("## 보호 장치 하나하나와"):].split("\n## ")[0].splitlines()
            if line.startswith("|")]
    assert len(rows) - 2 == len(protections), "the Korean document lists a different number of protections"


def test_every_variant_takes_out_one_place():
    """A protection that sits in two places of the code is two entries. A variant whose text
    occurs twice would take both out together, and taking out one alone would go unnoticed."""
    for protection in listed("protections.toml", "protection"):
        text = (BENCH / protection["file"]).read_text(encoding="utf-8")
        for change in protection["change"]:
            assert text.count(change["find"]) == 1, (
                f"{protection['what']}: the text its variant takes out occurs {text.count(change['find'])} times "
                f"in {protection['file']}; each place is an entry of its own")


def test_the_check_refuses_a_variant_whose_text_does_not_occur_exactly_once(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("check_protections", BENCH / "experiments/check_protections.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    bench = tmp_path / "bench"
    bench.mkdir()
    (bench / "twice.py").write_text("stop()\nwork()\nstop()\n", encoding="utf-8")
    monkeypatch.setattr(check, "BENCH", bench)
    entry = {"what": "made up", "file": "twice.py", "tests": [], "change": [{"find": "stop()\n", "put": ""}]}
    with pytest.raises(SystemExit, match="exactly once"):
        check.broken_copy(entry, tmp_path / "twice")
    entry["change"] = [{"find": "rest()\n", "put": ""}]
    with pytest.raises(SystemExit, match="exactly once"):
        check.broken_copy(entry, tmp_path / "never")
    entry["change"] = [{"find": "work()\n", "put": ""}]
    copy = check.broken_copy(entry, tmp_path / "once")
    assert (copy / "twice.py").read_text(encoding="utf-8") == "stop()\nstop()\n"


def test_a_second_line_whose_variant_leaves_the_tests_unlisted_does_not_hold(tmp_path):
    """A variant after which the tests of the judge and of the vetting tool cannot even be listed shows
    nothing: the second line does not hold, rather than holding against no test at all."""
    spec = importlib.util.spec_from_file_location("check_protections", BENCH / "experiments/check_protections.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    entry = {"what": "made up", "file": "dodambench/judge.py", "tests": [], "second_line": "made up",
             "change": [{"find": "FILE_SECONDS, CASE_SECONDS = 30, 2\n", "put": "FILE_SECONDS, CASE_SECONDS = (30, 2\n"}]}
    result = check.against_judge_tests(entry)
    assert result["verdict"] != "second line", result


def load_check():
    spec = importlib.util.spec_from_file_location("check_protections", BENCH / "experiments/check_protections.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    return check


def test_a_second_line_holds_only_where_every_run_of_its_tests_passed():
    """A second line holds only where its tests were listed, ran, and every run of them ended passed:
    not where no run was started, nor where a run ended otherwise than passed or failed."""
    check = load_check()
    assert check.verdict_of({"listed": True, "codes": [0, 0], "failed": []}) == "second line"
    for run in ({"listed": True, "codes": [], "failed": []}, {"listed": True, "codes": [0, 5], "failed": []},
                {"listed": True, "codes": [0, 2], "failed": []}, {"listed": True, "codes": [1, 0], "failed": ["t"]},
                {"listed": False, "codes": [0], "failed": []}):
        assert check.verdict_of(run) != "second line", run


def test_no_second_line_is_judged_by_tests_that_fail_with_nothing_taken_out(monkeypatch):
    """The job first runs its tests with nothing taken out, as it runs them against a second line: where
    one fails there, the job fails, and no second line is judged by them."""
    check = load_check()
    seen = []

    def run_judge_tests(cwd):
        seen.append(cwd)
        return {"listed": True, "codes": [1], "failed": ["tests/test_cli_judge.py::test_x"], "said": ["failed"], "tests": 1}
    monkeypatch.setattr(check, "run_judge_tests", run_judge_tests)
    monkeypatch.setattr(check, "against_judge_tests", lambda protection: pytest.fail("a second line was judged"))
    assert check.second_lines() == 1 and len(seen) == 1


def test_the_second_lines_are_checked_against_the_tests_of_the_formats_and_the_converters():
    """The job's tests take in, besides the judge's and the vetting tool's, the light files that reach
    the judge's sum of time and the formats: the format of command-line tasks, the task format and the
    two converters."""
    check = load_check()
    for name in ("tests/test_cli_format.py", "tests/test_task_format.py", "tests/test_convert.py", "tests/test_cli_convert.py"):
        assert name in check.JUDGE_TESTS, name
