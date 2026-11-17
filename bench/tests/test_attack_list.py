"""The attack list and the list of protections are files in the repository; these keep them honest."""
import re
import tomllib
from pathlib import Path

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
    assert len(attacks) >= 40
    for attack in attacks:
        assert attack["class"] and attack["tries"] and attack["must"], attack["name"]
        assert attack["tests"], f"{attack['name']} is no test"
        for node in attack["tests"]:
            assert test_exists(node), f"{attack['name']}: there is no test {node}"
    classes = {attack["class"] for attack in attacks}
    for needed in ("learning the judging", "memory", "files", "processes", "the judge's own clean-up",
                   "passing as unjudged", "a container without bounds"):
        assert needed in classes, needed


def test_every_protection_has_a_broken_variant_that_names_a_test():
    protections = listed("protections.toml", "protection")
    assert len(protections) >= 40
    assert len({p["what"] for p in protections}) == len(protections)
    for protection in protections:
        text = (BENCH / protection["file"]).read_text(encoding="utf-8")
        assert protection["change"], protection["what"]
        for change in protection["change"]:
            assert change["find"] in text, f"{protection['what']}: its variant no longer fits the code"
            assert change["find"] != change["put"]
        assert protection["tests"] and all(test_exists(node) for node in protection["tests"]), protection["what"]


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
    korean = (DOCS / "command-line-tasks.ko.md").read_text(encoding="utf-8")
    assert "## 보호 장치 하나하나와" in korean, "the Korean document does not list the protections"
    rows = [line for line in korean[korean.index("## 보호 장치 하나하나와"):].split("\n## ")[0].splitlines()
            if line.startswith("|")]
    assert len(rows) - 2 == len(protections), "the Korean document lists a different number of protections"
