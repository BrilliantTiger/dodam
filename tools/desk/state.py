"""Every file and folder the desk reads or writes in its state folder is looked at before it is used.

A file must be a regular file: not a link, not a pipe, not a device, not a folder. A folder must be
a real folder, not a link to one. Both must belong to this account and be closed to every other
account. Anything else is refused in one sentence, before anything is opened: a pipe would make
the desk wait for ever, a link would send what the desk writes somewhere else.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class StateProblem(Exception):
    """Something in the state folder is not what the desk made or the owner was asked to make."""


def _closed(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def folder(path: Path, what: str, make: bool = False) -> Path:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not make:
            raise StateProblem(f"{what} is missing.") from None
        os.mkdir(path, 0o700)
        info = os.lstat(path)
    except OSError:
        raise StateProblem(f"{what} could not be looked at.") from None
    if not stat.S_ISDIR(info.st_mode) or not _closed(info):
        raise StateProblem(f"{what} must be a real folder that belongs to this account and is closed to every other account.")
    return path


def file(path: Path, what: str) -> bool:
    """Whether the file is there. Refuses what is there but is not a closed regular file of this account."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise StateProblem(f"{what} could not be looked at.") from None
    if not stat.S_ISREG(info.st_mode) or not _closed(info):
        raise StateProblem(f"{what} must be a regular file that belongs to this account and is closed to every other account.")
    return True


def read(path: Path, what: str) -> str | None:
    """The text of the file, or None when it is not there."""
    if not file(path, what):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError):
        raise StateProblem(f"{what} could not be read.") from None


def opened(path: Path, what: str, append: bool = False):
    """Open the file for writing, making it closed to others when it is not there yet."""
    file(path, what)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    try:
        return os.fdopen(os.open(path, flags, 0o600), "a" if append else "w", encoding="utf-8")
    except OSError:
        raise StateProblem(f"{what} could not be written.") from None


def write(path: Path, what: str, text: str) -> None:
    """Replace the file's text in one step, so that a reader never sees half of it."""
    file(path, what)
    temporary = path.with_name(path.name + ".tmp")
    with opened(temporary, what) as handle:
        handle.write(text)
    os.replace(temporary, path)
