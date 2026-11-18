#!/usr/bin/env python3
"""Classifies a pull request's changed files from GitHub's pulls/<n>/files JSON.

    gh api --paginate repos/BrilliantTiger/dodam/pulls/<n>/files | python3 tools/desk/pr_files.py [--draft]

Standard library only, with no network access. Reads GitHub's JSON list of pull
request file entries (including concatenated JSON arrays emitted by
`gh api --paginate`) from standard input or through `classify_pr_files()`, looks
at both `filename` and `previous_filename` for every entry, learns existing
benchmark task folders from `bench/tasks/` in the local checkout (at `main`),
and prints one JSON object on standard output:

    {
      "protected_folders": [...],
      "adds_new_test": true | false,
      "ready_for_review": true | false
    }

File names are outside text and never appear in the output.
"""

from __future__ import annotations

import argparse
from collections.abc import Collection
import json
import os
import posixpath
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

PROTECTED_FOLDERS: tuple[str, ...] = (
    "bench/dodambench/",
    "bench/images/",
    ".github/",
    "bench/tests/",
    "bench/tasks/",
)
ALWAYS_PROTECTED_FOLDERS: tuple[str, ...] = (
    "bench/dodambench/",
    "bench/images/",
    ".github/",
)


class PRFilesError(Exception):
    """Raised when the input JSON or local benchmark task folder cannot be read."""


def existing_task_folders(repo_root: Path | None = None) -> set[str]:
    """Return existing benchmark task directory names under bench/tasks/ on main."""
    tasks_dir = (ROOT if repo_root is None else repo_root) / "bench" / "tasks"
    try:
        scanner = os.scandir(tasks_dir)
    except OSError as exc:
        raise PRFilesError("Cannot read bench/tasks directory in local checkout.") from exc
    with scanner:
        try:
            return {
                entry.name
                for entry in scanner
                if not entry.name.startswith(".") and entry.name != "__pycache__" and entry.is_dir()
            }
        except OSError as exc:
            raise PRFilesError("Cannot list bench/tasks directory in local checkout.") from exc


def _decode_github_file_list(raw: object) -> list[dict[str, object]]:
    """Accept a parsed list of file dicts or JSON text/bytes (including paginated arrays)."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeError as exc:
            raise PRFilesError("Input is not valid UTF-8 JSON.") from exc

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise PRFilesError("Expected JSON list of pull request files on standard input.")
        decoder = json.JSONDecoder()
        pos = 0
        length = len(text)
        pages: list[object] = []
        try:
            while pos < length:
                while pos < length and text[pos].isspace():
                    pos += 1
                if pos >= length:
                    break
                value, pos = decoder.raw_decode(text, pos)
                pages.append(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise PRFilesError("Input is not valid JSON.") from exc

        combined: list[object] = []
        for page in pages:
            if not isinstance(page, list):
                raise PRFilesError("Expected a JSON array of pull request file objects.")
            combined.extend(page)
        raw_list = combined
    elif isinstance(raw, list):
        raw_list = raw
    else:
        raise PRFilesError("Expected a list of pull request file objects.")

    validated: list[dict[str, object]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            raise PRFilesError("Each pull request file entry must be a JSON object.")
        validated.append(item)
    return validated


def _has_control_or_backslash(path: str) -> bool:
    return "\\" in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)


def _candidate_paths(raw_path: str) -> list[str]:
    """Return both raw and normalized slash-trimmed forms so '..' traversal cannot hide a folder."""
    trimmed = raw_path.lstrip("/")
    normed = posixpath.normpath(raw_path).lstrip("/") if raw_path else ""
    if normed and normed != trimmed:
        return [trimmed, normed]
    return [trimmed]


def _matches_folder(path: str, folder: str) -> bool:
    bare = folder.rstrip("/")
    for cand in _candidate_paths(path):
        if cand == bare or cand.startswith(folder):
            return True
        parts = cand.split("/")
        if folder == ".github/" and ".github" in parts[:1]:
            return True
    return False


def _is_canonical_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/") or _has_control_or_backslash(path):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _is_unprotected_new_test(path: str) -> bool:
    """True only for test_<something>.py directly inside bench/tests/."""
    if not _is_canonical_repo_path(path) or not path.startswith("bench/tests/"):
        return False
    rel = path[len("bench/tests/") :]
    if "/" in rel or not (rel.startswith("test_") and rel.endswith(".py")):
        return False
    middle = rel[len("test_") : -len(".py")]
    return len(middle) >= 1


def _is_unprotected_new_task_file(path: str, known_tasks: set[str]) -> bool:
    """True only for bench/tasks/<new_folder>/... where <new_folder> does not exist on main."""
    if not _is_canonical_repo_path(path) or not path.startswith("bench/tasks/"):
        return False
    rel = path[len("bench/tasks/") :]
    parts = rel.split("/")
    if len(parts) < 2:
        return False
    task_folder = parts[0]
    if task_folder.startswith(".") or task_folder == "__pycache__":
        return False
    return task_folder not in known_tasks


def classify_pr_files(
    files: object,
    *,
    draft: bool = False,
    existing_tasks: Collection[str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, object]:
    """Classify GitHub pulls/<n>/files JSON into protected_folders, adds_new_test, ready_for_review."""
    if not isinstance(draft, bool):
        raise PRFilesError("draft must be a boolean.")
    entries = _decode_github_file_list(files)
    known_tasks = (
        existing_task_folders(repo_root=repo_root)
        if existing_tasks is None
        else set(existing_tasks)
    )

    touched: set[str] = set()
    adds_new_test = False

    for entry in entries:
        status = entry.get("status")
        filename = entry.get("filename")
        prev_filename = entry.get("previous_filename")

        if not isinstance(status, str) or not status:
            raise PRFilesError("File entry is missing a string status.")
        if not isinstance(filename, str) or not filename:
            raise PRFilesError("File entry is missing a string filename.")
        if prev_filename is not None and not isinstance(prev_filename, str):
            raise PRFilesError("File entry has an invalid previous_filename.")

        has_earlier_path = isinstance(prev_filename, str) and prev_filename != ""
        # Only status == "added" with no earlier path counts as adding a brand-new file;
        # every other status (including copied, renamed, or unknown status words) counts
        # as a change of something that exists.
        is_pure_add = status == "added" and not has_earlier_path

        # 1. Check the earlier path (if any): any touch of any fixed folder on an earlier path
        #    moves or copies something that existed there, so it is always protected.
        if has_earlier_path:
            assert isinstance(prev_filename, str)
            for folder in PROTECTED_FOLDERS:
                if _matches_folder(prev_filename, folder):
                    touched.add(folder)

        # 2. Check the current path.
        for folder in ALWAYS_PROTECTED_FOLDERS:
            if _matches_folder(filename, folder):
                touched.add(folder)

        if _matches_folder(filename, "bench/tests/"):
            if is_pure_add and _is_unprotected_new_test(filename):
                adds_new_test = True
            else:
                touched.add("bench/tests/")

        if _matches_folder(filename, "bench/tasks/"):
            if not (is_pure_add and _is_unprotected_new_task_file(filename, known_tasks)):
                touched.add("bench/tasks/")

    ordered_protected = [folder for folder in PROTECTED_FOLDERS if folder in touched]
    ready_for_review = (not draft) and adds_new_test and (len(ordered_protected) == 0)
    return {
        "protected_folders": ordered_protected,
        "adds_new_test": adds_new_test,
        "ready_for_review": ready_for_review,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PRFilesError("Invalid command-line arguments; only --draft is accepted.")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="pr_files.py",
        description="Classify GitHub pull request files JSON without emitting file names.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Mark the pull request as a draft (never ready for review).",
    )
    try:
        ns = parser.parse_args(argv)
        raw_stdin = sys.stdin.buffer.read()
        verdict = classify_pr_files(raw_stdin, draft=ns.draft)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except PRFilesError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 1
    except Exception:
        sys.stderr.write("Unexpected internal error classifying pull request files.\n")
        sys.stderr.flush()
        return 1

    sys.stdout.write(json.dumps(verdict, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
