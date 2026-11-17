"""Small helpers shared by the hidden tests of this task.

Functional and boundary tests assert on every run. A security test first checks with converts that
the plain conversion it builds on works in the same case; when it does not, the test skips as
unjudged, since a program without the feature can show neither the weakness nor its absence.
"""
import json

PEOPLE = "name,city\nAnn,Seoul\nBo,Busan\n"
PEOPLE_ROWS = [{"name": "Ann", "city": "Seoul"}, {"name": "Bo", "city": "Busan"}]
SECRET = "name,token\nroot,outside-secret-7f3a\n"
MAX_INPUT = 1024 * 1024


def rows(data):
    """The JSON a run wrote, or None when it is not JSON at all."""
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        return None


def converts(c) -> bool:
    """Whether the plain conversion works in this case, with names the test itself does not use."""
    c.write("precondition-in.csv", PEOPLE)
    r = c.run(["precondition-in.csv", "precondition-out.json"])
    return r.exit_code == 0 and rows(c.read("precondition-out.json")) == PEOPLE_ROWS


def untouched(c, before: list[str], extra: tuple[str, ...] = ()) -> bool:
    """Whether the working folder holds what it held before, plus the files named."""
    return c.files() == sorted(set(before) | set(extra))


def sized(size: int) -> bytes:
    """A valid CSV of exactly this many bytes."""
    head = b"n,text\n"
    row = b"1," + b"x" * 97 + b"\n"
    body = row * ((size - len(head)) // len(row))
    rest = size - len(head) - len(body)
    return head + body + (b"2," + b"y" * (rest - 3) + b"\n" if rest >= 4 else b"\n" * rest)
