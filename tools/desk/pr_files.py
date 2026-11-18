#!/usr/bin/env python3
"""Classifies a pull request's changed files from GitHub's JSON answers.

    {
      base=$(gh api repos/BrilliantTiger/dodam/pulls/<n> --jq '.base.sha | select(type == "string" and test("^[0-9a-f]{40}$")) // error("missing base sha")') &&
      gh api repos/BrilliantTiger/dodam/pulls/<n> --jq '{changed_files, draft}' &&
      gh api "repos/BrilliantTiger/dodam/contents/bench/tasks?ref=$base" --jq '{task_folders: [.[] | select(.type == "dir") | .name]}' &&
      gh api --paginate repos/BrilliantTiger/dodam/pulls/<n>/files
    } | python3 tools/desk/pr_files.py

Standard library only, with no network access. Checks that `changed_files`,
`draft`, and the base-commit `task_folders` list from GitHub are all present and
well-formed, that `changed_files` is below GitHub's 3,000-file listing cap, and
that the number of file entries received equals `changed_files`. Compares folder
names and benchmark task folder names after Unicode NFC normalisation without
regard to case, and counts only the exact status word `"added"` (with no earlier
path) as adding a new file.

On valid complete input, prints one JSON object and exits 0:

    {"protected_folders": [...], "adds_new_test": bool, "ready_for_review": bool}

On incomplete, missing, capped (>= 3,000), or malformed input, prints one
refusal JSON object (`"ready_for_review": false`, `"refused": true`, `"reason"`)
with no file names and exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
from pathlib import Path
import sys
import unicodedata

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

MAX_GITHUB_LISTED_FILES = 3_000
EXIT_OK = 0
EXIT_REFUSED = 2

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
    """Raised internally when input JSON or local benchmark folders cannot be read."""


def _norm_segment(text: str) -> str:
    """Normalise to Unicode NFC and fold case for case-insensitive folder comparison."""
    return unicodedata.normalize("NFC", text).casefold()


_FOLDER_SEGMENTS: dict[str, tuple[str, ...]] = {
    folder: tuple(_norm_segment(seg) for seg in folder.rstrip("/").split("/"))
    for folder in PROTECTED_FOLDERS
}


def _refusal(reason: str) -> dict[str, object]:
    return {
        "protected_folders": [],
        "adds_new_test": False,
        "ready_for_review": False,
        "refused": True,
        "reason": reason,
    }


def existing_task_folders(repo_root: Path | None = None) -> set[str]:
    """Return existing benchmark task directory names under bench/tasks/ in the local checkout."""
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


def _existing_test_filenames_folded(repo_root: Path | None = None) -> set[str]:
    """Return NFC+casefolded file names directly inside bench/tests/ in the local checkout."""
    tests_dir = (ROOT if repo_root is None else repo_root) / "bench" / "tests"
    try:
        scanner = os.scandir(tests_dir)
    except OSError as exc:
        raise PRFilesError("Cannot read bench/tests directory in local checkout.") from exc
    with scanner:
        try:
            return {
                _norm_segment(entry.name)
                for entry in scanner
                if not entry.name.startswith(".") and entry.name != "__pycache__"
            }
        except OSError as exc:
            raise PRFilesError("Cannot list bench/tests directory in local checkout.") from exc


def _has_control_or_backslash(path: str) -> bool:
    return "\\" in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)


def _decode_json_values(raw: str | bytes) -> list[object]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise PRFilesError("Input is not valid UTF-8 JSON.") from exc
    else:
        text = raw

    stripped = text.strip()
    if not stripped:
        raise PRFilesError("No JSON input received.")

    decoder = json.JSONDecoder()
    pos = 0
    length = len(stripped)
    values: list[object] = []
    try:
        while pos < length:
            while pos < length and stripped[pos].isspace():
                pos += 1
            if pos >= length:
                break
            value, pos = decoder.raw_decode(stripped, pos)
            values.append(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise PRFilesError("Input is not valid JSON.") from exc
    return values


def _decode_github_file_list(raw: object) -> list[dict[str, object]]:
    """Accept a parsed list of file dicts or JSON text/bytes (including paginated arrays)."""
    if isinstance(raw, (str, bytes)):
        pages = _decode_json_values(raw)
        if not pages:
            raise PRFilesError("Pull request file list is missing.")
        combined: list[object] = []
        for page in pages:
            if not isinstance(page, list):
                raise PRFilesError("Expected a JSON list of pull request file objects.")
            combined.extend(page)
        raw_list = combined
    elif isinstance(raw, list):
        raw_list = raw
    else:
        raise PRFilesError("Expected a JSON list of pull request file objects.")

    validated: list[dict[str, object]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            raise PRFilesError("Each pull request file entry must be a JSON object.")
        validated.append(item)
    return validated


def _validate_task_folder_list(task_folders: object) -> set[str]:
    if task_folders is None:
        raise PRFilesError("Base-commit task_folders list from GitHub is missing.")
    if not isinstance(task_folders, list):
        raise PRFilesError("Base-commit task_folders must be a JSON list of folder names.")
    folded: set[str] = set()
    for item in task_folders:
        if (
            not isinstance(item, str)
            or not item
            or "/" in item
            or item in (".", "..")
            or _has_control_or_backslash(item)
        ):
            raise PRFilesError("Base-commit task_folders contains an invalid folder name.")
        folded.add(_norm_segment(item))
    return folded


def _candidate_paths(raw_path: str) -> list[str]:
    """Return both raw and normalized slash-trimmed forms so '..' traversal cannot hide a folder."""
    trimmed = raw_path.lstrip("/")
    normed = posixpath.normpath(raw_path).lstrip("/") if raw_path else ""
    if normed and normed != trimmed:
        return [trimmed, normed]
    return [trimmed]


def _matches_folder(path: str, folder: str) -> bool:
    """Compare folder segments after Unicode NFC normalisation without regard to case."""
    want_segs = _FOLDER_SEGMENTS[folder]
    want_len = len(want_segs)
    for cand in _candidate_paths(path):
        segs = tuple(_norm_segment(seg) for seg in cand.split("/") if seg != "")
        if len(segs) >= want_len and segs[:want_len] == want_segs:
            return True
    return False


def _is_canonical_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/") or _has_control_or_backslash(path):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _is_unprotected_new_test(path: str, existing_tests_folded: set[str]) -> bool:
    """True only for a brand-new test_<something>.py directly in canonical bench/tests/."""
    if not _is_canonical_repo_path(path) or not path.startswith("bench/tests/"):
        return False
    rel = path[len("bench/tests/") :]
    if "/" in rel or not (rel.startswith("test_") and rel.endswith(".py")):
        return False
    middle = rel[len("test_") : -len(".py")]
    if len(middle) < 1:
        return False
    # Guard case-insensitive disks against adding TEST_DESK.py over an existing test_desk.py.
    if _norm_segment(rel) in existing_tests_folded:
        return False
    return True


def _is_unprotected_new_task_file(path: str, known_tasks_folded: set[str]) -> bool:
    """True only for canonical bench/tasks/<new_folder>/... absent from both base and local copy."""
    if not _is_canonical_repo_path(path) or not path.startswith("bench/tasks/"):
        return False
    rel = path[len("bench/tasks/") :]
    parts = rel.split("/")
    if len(parts) < 2:
        return False
    task_folder = parts[0]
    if task_folder.startswith(".") or _norm_segment(task_folder) == "__pycache__":
        return False
    return _norm_segment(task_folder) not in known_tasks_folded


def classify_pr_files(
    files: object,
    *,
    changed_files: object = None,
    draft: object = None,
    task_folders: object = None,
    repo_root: Path | None = None,
) -> dict[str, object]:
    """Classify GitHub pulls/<n>/files JSON, refusing incomplete, capped, or missing inputs."""
    if changed_files is None:
        return _refusal("Pull request changed_files count is missing.")
    if isinstance(changed_files, bool) or not isinstance(changed_files, int) or changed_files < 0:
        return _refusal("Pull request changed_files count is invalid.")
    if changed_files >= MAX_GITHUB_LISTED_FILES:
        return _refusal(
            f"Pull request changed_files ({changed_files}) is 3,000 or more; "
            "GitHub truncates pull request file lists at 3,000."
        )

    if draft is None:
        return _refusal("Pull request draft flag is missing.")
    if not isinstance(draft, bool):
        return _refusal("Pull request draft flag is invalid.")

    try:
        base_tasks_folded = _validate_task_folder_list(task_folders)
        local_tasks_folded = {_norm_segment(name) for name in existing_task_folders(repo_root=repo_root)}
        existing_tests_folded = _existing_test_filenames_folded(repo_root=repo_root)
        entries = _decode_github_file_list(files)
    except PRFilesError as exc:
        return _refusal(str(exc))

    if len(entries) != changed_files:
        return _refusal(
            f"Incomplete or mismatched pull request file list: received {len(entries)} entries, "
            f"expected changed_files={changed_files}."
        )

    known_tasks_folded = base_tasks_folded | local_tasks_folded
    touched: set[str] = set()
    adds_new_test = False

    for entry in entries:
        status = entry.get("status")
        filename = entry.get("filename")
        prev_filename = entry.get("previous_filename")

        if not isinstance(status, str) or not status:
            return _refusal("File entry is missing a string status.")
        if not isinstance(filename, str) or not filename:
            return _refusal("File entry is missing a string filename.")
        if prev_filename is not None and not isinstance(prev_filename, str):
            return _refusal("File entry has an invalid previous_filename.")

        has_earlier_path = isinstance(prev_filename, str) and prev_filename != ""
        # Strictly case-sensitive comparison on "added": any other spelling (such as "Added" or
        # "ADDED") or any other status word counts as a change of something that exists.
        is_pure_add = (status == "added") and (not has_earlier_path)

        if has_earlier_path:
            assert isinstance(prev_filename, str)
            for folder in PROTECTED_FOLDERS:
                if _matches_folder(prev_filename, folder):
                    touched.add(folder)

        for folder in ALWAYS_PROTECTED_FOLDERS:
            if _matches_folder(filename, folder):
                touched.add(folder)

        if _matches_folder(filename, "bench/tests/"):
            if is_pure_add and _is_unprotected_new_test(filename, existing_tests_folded):
                adds_new_test = True
            else:
                touched.add("bench/tests/")

        if _matches_folder(filename, "bench/tasks/"):
            if not (is_pure_add and _is_unprotected_new_task_file(filename, known_tasks_folded)):
                touched.add("bench/tasks/")

    ordered_protected = [folder for folder in PROTECTED_FOLDERS if folder in touched]
    ready_for_review = (not draft) and adds_new_test and (len(ordered_protected) == 0)
    return {
        "protected_folders": ordered_protected,
        "adds_new_test": adds_new_test,
        "ready_for_review": ready_for_review,
    }


def _parse_bool_arg(raw: str) -> bool:
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise PRFilesError("--draft must be 'true' or 'false'.")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PRFilesError("Invalid command-line arguments.")


def _split_streamed_input(
    raw_stdin: bytes,
    cli_changed_files: int | None,
    cli_draft: bool | None,
    cli_task_folders: object,
) -> tuple[list[dict[str, object]], object, object, object]:
    values = _decode_json_values(raw_stdin)
    changed_files: object = cli_changed_files
    draft: object = cli_draft
    task_folders: object = cli_task_folders

    idx = 0
    allowed_header_keys = {"changed_files", "draft", "task_folders"}
    while idx < len(values) and isinstance(values[idx], dict):
        hdr = values[idx]
        assert isinstance(hdr, dict)
        if not hdr or not set(hdr).issubset(allowed_header_keys):
            raise PRFilesError("Unexpected JSON object in pull request check input.")
        if "changed_files" in hdr:
            if changed_files is not None:
                raise PRFilesError("Duplicate changed_files value in input.")
            changed_files = hdr["changed_files"]
        if "draft" in hdr:
            if draft is not None:
                raise PRFilesError("Duplicate draft value in input.")
            draft = hdr["draft"]
        if "task_folders" in hdr:
            if task_folders is not None:
                raise PRFilesError("Duplicate task_folders value in input.")
            task_folders = hdr["task_folders"]
        idx += 1

    page_values = values[idx:]
    if not page_values:
        raise PRFilesError("Pull request file list is missing from input.")

    entries: list[dict[str, object]] = []
    for page in page_values:
        if not isinstance(page, list):
            raise PRFilesError("Expected JSON list pages of pull request file objects.")
        for item in page:
            if not isinstance(item, dict):
                raise PRFilesError("Each pull request file entry must be a JSON object.")
            entries.append(item)
    return (entries, changed_files, draft, task_folders)


def _emit_verdict(verdict: dict[str, object]) -> int:
    sys.stdout.write(json.dumps(verdict, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    if verdict.get("refused") is True:
        reason = str(verdict.get("reason", "Refused pull request file check."))
        sys.stderr.write(f"{reason}\n")
        sys.stderr.flush()
        return EXIT_REFUSED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="pr_files.py",
        description="Classify GitHub pull request files JSON without emitting file names.",
    )
    parser.add_argument("--changed-files", type=int, default=None)
    parser.add_argument("--draft", type=str, default=None)
    parser.add_argument("--task-folders", type=str, default=None)
    try:
        ns = parser.parse_args(argv)
        cli_draft = _parse_bool_arg(ns.draft) if ns.draft is not None else None
        cli_tasks: object = None
        if ns.task_folders is not None:
            try:
                cli_tasks = json.loads(ns.task_folders)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise PRFilesError("--task-folders is not valid JSON.") from exc
        raw_stdin = sys.stdin.buffer.read()
        entries, changed_files, draft, task_folders = _split_streamed_input(
            raw_stdin, ns.changed_files, cli_draft, cli_tasks
        )
        verdict = classify_pr_files(
            entries,
            changed_files=changed_files,
            draft=draft,
            task_folders=task_folders,
        )
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_OK
    except PRFilesError as exc:
        return _emit_verdict(_refusal(str(exc)))
    except Exception:
        return _emit_verdict(_refusal("Unexpected internal error classifying pull request files."))

    return _emit_verdict(verdict)


if __name__ == "__main__":
    sys.exit(main())
