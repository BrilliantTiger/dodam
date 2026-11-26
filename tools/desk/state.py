"""Every file and folder the desk reads or writes in its state folder is looked at before it is used.

A file must be a regular file: not a link, not a pipe, not a device, not a folder. A folder must be
a real folder, not a link to one. Both must belong to this account and be closed to every other
account. Anything else is refused in one sentence, before anything is opened: a pipe would make
the desk wait for ever, a link would send what the desk writes somewhere else.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

# Two threads of one program that write the same file take turns, so that neither finds the other's half-written copy.
# Programs that write the same file hold a lock of the state folder for it, or are the only one that writes it.
_WRITING = threading.Lock()


class StateProblem(Exception):
    """Something in the state folder is not what the desk made or the owner was asked to make."""


def _closed(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def program_holds(program: Path, source: str) -> bool:
    """Whether a copy of a program is what was checked, at the moment it is looked at: an ordinary file and no link,
    of this account, runnable by it and writable by no other, in a folder of this account that no other may write, with
    the bytes of the program it was copied from."""
    try:
        folder, info = os.lstat(program.parent), os.lstat(program)
    except OSError:
        return False
    if not stat.S_ISDIR(folder.st_mode) or not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o100:
        return False
    if any(what.st_uid != os.getuid() or what.st_mode & 0o022 for what in (folder, info)):
        return False
    try:
        descriptor = os.open(program, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(descriptor, "rb") as copy, open(source, "rb") as original:
            return copy.read() == original.read()
    except OSError:
        return False


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


def _open(path: Path, what: str, flags: int) -> int:
    """Open without following a link, and look at what was opened: nothing can change between the look and the use."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or not _closed(info):
        os.close(descriptor)
        raise StateProblem(f"{what} must be a regular file that belongs to this account and is closed to every other account.")
    return descriptor


def read(path: Path, what: str) -> str | None:
    """The text of the file, or None when it is not there."""
    if not file(path, what):
        return None
    try:
        with os.fdopen(_open(path, what, os.O_RDONLY), "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError):
        raise StateProblem(f"{what} could not be read.") from None


def opened(path: Path, what: str, append: bool = False):
    """Open the file for writing, making it closed to others when it is not there yet.

    Two callers that find the file missing at once both work: the one that does not make it opens the one the other
    made, after looking at it like every file.
    """
    for _ in range(2):
        try:
            if file(path, what):
                # Emptied only after it was looked at: a file that is not the desk's is never cut short.
                descriptor = _open(path, what, os.O_WRONLY | (os.O_APPEND if append else 0))
                if not append:
                    os.ftruncate(descriptor, 0)
            else:
                descriptor = _open(path, what, os.O_WRONLY | os.O_CREAT | os.O_EXCL | (os.O_APPEND if append else 0))
            return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")
        except FileExistsError:
            continue  # Made by another caller between the look and the making: looked at again and opened.
        except OSError:
            break
    raise StateProblem(f"{what} could not be written.")


def write(path: Path, what: str, text: str) -> None:
    """Replace the file's text in one step, so that a reader never sees half of it."""
    file(path, what)
    temporary = path.with_name(path.name + ".tmp")
    with _WRITING:
        try:
            if file(temporary, what):
                os.unlink(temporary)
            with os.fdopen(_open(temporary, what, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, path)
        except OSError:
            raise StateProblem(f"{what} could not be written.") from None


def real_folder(raw: Path, what: str) -> Path:
    """A folder given by the owner: no folder on the way to it is a link, and what is missing is made closed to others."""
    given = Path(os.path.abspath(raw))
    missing = []
    for part in (given, *given.parents):
        if part.is_symlink():
            raise StateProblem(f"{what} must be a real folder, and no folder above it may be a link. Give its real path.")
        if not part.exists():
            missing.append(part)
    try:
        for part in reversed(missing):
            os.mkdir(part, 0o700)
    except OSError:
        raise StateProblem(f"{what} could not be made.") from None
    return folder(given, what)
