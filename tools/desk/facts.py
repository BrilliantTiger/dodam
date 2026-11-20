"""The one place that reads the desk's public facts.

The facts (which ways in are open, and the limits) live in one file, tools/desk/desk.toml.
Every tool of the desk and the command that starts the desk read them through load() and
nowhere else. A missing file, a missing name or a value of the wrong kind is a refusal:
nothing is assumed, so a damaged file closes the desk instead of opening it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

LIMIT_NAMES = (
    "answers_per_item_per_hour",
    "answers_per_day",
    "code_uses",
    "code_minutes",
    "codes_per_author_per_day",
    "live_sessions",
)
WAY_NAMES = ("github", "live")


class FactsProblem(Exception):
    """The file of facts is missing or cannot be trusted."""


@dataclass(frozen=True)
class Facts:
    name: str
    github_open: bool
    live_open: bool
    answers_per_item_per_hour: int
    answers_per_day: int
    code_uses: int
    code_minutes: int
    codes_per_author_per_day: int
    live_sessions: int
    coworker_min_version: str

    def way_open(self, way: str) -> bool:
        if way not in WAY_NAMES:
            raise FactsProblem(f"There is no way in called {way!r}.")
        return self.github_open if way == "github" else self.live_open


def _table(data: dict, name: str) -> dict:
    value = data.get(name)
    if not isinstance(value, dict):
        raise FactsProblem(f"The facts have no section [{name}].")
    return value


def load(path: Path | str) -> Facts:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FactsProblem("The file of the desk's facts is missing, so every way in counts as closed.") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise FactsProblem("The file of the desk's facts cannot be read, so every way in counts as closed.") from None

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FactsProblem("The facts have no name for the desk.")

    ways = _table(data, "ways")
    opened = {}
    for way in WAY_NAMES:
        value = _table(ways, way).get("open")
        # Only the word true opens a way. "yes", 1 or anything else is a damaged file.
        if not isinstance(value, bool):
            raise FactsProblem(f"The facts do not say true or false for ways.{way}.open.")
        opened[way] = value

    limits = _table(data, "limits")
    numbers = {}
    for key in LIMIT_NAMES:
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FactsProblem(f"The facts have no whole number of at least 1 for limits.{key}.")
        numbers[key] = value

    version = _table(data, "coworker").get("min_version")
    if not isinstance(version, str) or not version.strip():
        raise FactsProblem("The facts have no coworker.min_version.")

    return Facts(
        name=name.strip(),
        github_open=opened["github"],
        live_open=opened["live"],
        coworker_min_version=version.strip(),
        **numbers,
    )
