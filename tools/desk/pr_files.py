#!/usr/bin/env python3
"""Classifies a pull request's changed files from GitHub's JSON answers.

    {
      base=$(gh api repos/BrilliantTiger/dodam/pulls/<n> --jq '.base.sha | select(type == "string" and test("^[0-9a-f]{40}$")) // error("missing base sha")') &&
      gh api repos/BrilliantTiger/dodam/pulls/<n> --jq '{changed_files, draft, base_ref: .base.ref}' &&
      gh api "repos/BrilliantTiger/dodam/contents/bench/tasks?ref=$base" --jq '{task_folders: (if length >= 1000 then [.[] | .name] else [.[] | select(.type == "dir") | .name] end)}' &&
      gh api "repos/BrilliantTiger/dodam/contents/bench/tasks?ref=main" --jq '{main_task_folders: (if length >= 1000 then [.[] | .name] else [.[] | select(.type == "dir") | .name] end)}' &&
      gh api "repos/BrilliantTiger/dodam/contents/bench/tests?ref=main" --jq '{main_test_entries: [.[] | .name]}' &&
      gh api --paginate repos/BrilliantTiger/dodam/pulls/<n>/files
    } | python3 tools/desk/pr_files.py

Standard library only, with no network access. Checks that `changed_files`,
`draft`, and `base_ref` from GitHub's pull request answer (`base_ref` must be
`"main"`), together with the three directory listings from GitHub (`task_folders`
at the pull request's base commit, `main_task_folders` at the tip of `main`, and
`main_test_entries` directly in `bench/tests/` at the tip of `main`), are all
present, well-formed, and below GitHub's truncation caps (`1,000` entries per
directory listing and `3,000` files per pull request), and that the number of
file entries received equals `changed_files`. Compares folder names, task-folder
names, and test entry names after Unicode NFC normalisation without regard to
case, and counts only the exact status word `"added"` (with no earlier path) as
adding a new file.

On valid complete input, prints one JSON object and exits 0:

    {"protected_folders": [...], "adds_new_test": bool, "ready_for_review": bool}

On incomplete, missing, non-main, capped, or malformed input, prints one refusal
JSON object (`"ready_for_review": false`, `"refused": true`, `"reason"`) with no
file names and exits 2.
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
MAX_GITHUB_DIRECTORY_ENTRIES = 1_000
TARGET_BASE_BRANCH = "main"
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
    """Normalise to Unicode NFC and fold case for case-insensitive comparison."""
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
    """Return NFC+casefolded entry names directly inside bench/tests/ in the local checkout."""
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


def _validate_github_name_list(raw_list: object, label: str) -> set[str]:
    if raw_list is None:
        raise PRFilesError(f"{label} from GitHub is missing.")
    if not isinstance(raw_list, list):
        raise PRFilesError(f"{label} from GitHub must be a JSON list of names.")
    if len(raw_list) >= MAX_GITHUB_DIRECTORY_ENTRIES:
        raise PRFilesError(
            f"{label} from GitHub has 1,000 or more entries; "
            "GitHub truncates directory listings at 1,000."
        )
    folded: set[str] = set()
    for item in raw_list:
        if (
            not isinstance(item, str)
            or not item
            or "/" in item
            or item in (".", "..")
            or _has_control_or_backslash(item)
        ):
            raise PRFilesError(f"{label} from GitHub contains an invalid entry name.")
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
    # Guard against existing entries at the tip of main or in the local copy regardless of case or NFC/NFD.
    if _norm_segment(rel) in existing_tests_folded:
        return False
    return True


def _is_unprotected_new_task_file(path: str, known_tasks_folded: set[str]) -> bool:
    """True only for canonical bench/tasks/<new_folder>/... absent from tip of main, base, and local copy."""
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
    base_ref: object = None,
    task_folders: object = None,
    main_task_folders: object = None,
    main_test_entries: object = None,
    repo_root: Path | None = None,
) -> dict[str, object]:
    """Classify GitHub pulls/<n>/files JSON, refusing incomplete, non-main, capped, or missing inputs."""
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

    if base_ref is None:
        return _refusal("Pull request base_ref branch name is missing.")
    if not isinstance(base_ref, str) or not base_ref:
        return _refusal("Pull request base_ref branch name is invalid.")
    if base_ref != TARGET_BASE_BRANCH:
        return _refusal("Pull request targets a base branch other than main.")

    try:
        base_tasks_folded = _validate_github_name_list(task_folders, "Base-commit bench/tasks listing")
        main_tasks_folded = _validate_github_name_list(main_task_folders, "Tip-of-main bench/tasks listing")
        main_tests_folded = _validate_github_name_list(main_test_entries, "Tip-of-main bench/tests listing")
        local_tasks_folded = {_norm_segment(name) for name in existing_task_folders(repo_root=repo_root)}
        local_tests_folded = _existing_test_filenames_folded(repo_root=repo_root)
        entries = _decode_github_file_list(files)
    except PRFilesError as exc:
        return _refusal(str(exc))

    if len(entries) != changed_files:
        return _refusal(
            f"Incomplete or mismatched pull request file list: received {len(entries)} entries, "
            f"expected changed_files={changed_files}."
        )

    known_tasks_folded = main_tasks_folded | base_tasks_folded | local_tasks_folded
    existing_tests_folded = main_tests_folded | local_tests_folded
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


_UNSET = object()


def _split_streamed_input(
    raw_stdin: bytes,
    cli_changed_files: object,
    cli_draft: object,
    cli_base_ref: object,
    cli_task_folders: object,
    cli_main_task_folders: object,
    cli_main_test_entries: object,
) -> tuple[list[dict[str, object]], object, object, object, object, object, object]:
    values = _decode_json_values(raw_stdin)
    meta: dict[str, object] = {
        "changed_files": cli_changed_files,
        "draft": cli_draft,
        "base_ref": cli_base_ref,
        "task_folders": cli_task_folders,
        "main_task_folders": cli_main_task_folders,
        "main_test_entries": cli_main_test_entries,
    }

    idx = 0
    allowed_header_keys = set(meta)
    while idx < len(values) and isinstance(values[idx], dict):
        hdr = values[idx]
        if not hdr or not set(hdr).issubset(allowed_header_keys):
            raise PRFilesError("Unexpected JSON object in pull request check input.")
        for key, val in hdr.items():
            if meta[key] is not _UNSET:
                raise PRFilesError(f"Duplicate {key} value in input.")
            meta[key] = val
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
    return (
        entries,
        None if meta["changed_files"] is _UNSET else meta["changed_files"],
        None if meta["draft"] is _UNSET else meta["draft"],
        None if meta["base_ref"] is _UNSET else meta["base_ref"],
        None if meta["task_folders"] is _UNSET else meta["task_folders"],
        None if meta["main_task_folders"] is _UNSET else meta["main_task_folders"],
        None if meta["main_test_entries"] is _UNSET else meta["main_test_entries"],
    )


def _emit_verdict(verdict: dict[str, object]) -> int:
    sys.stdout.write(json.dumps(verdict, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    if verdict.get("refused") is True:
        reason = str(verdict.get("reason", "Refused pull request file check."))
        sys.stderr.write(f"{reason}\n")
        sys.stderr.flush()
        return EXIT_REFUSED
    return EXIT_OK


def _parse_json_arg(raw: str | None, flag_name: str) -> object:
    if raw is None:
        return _UNSET
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise PRFilesError(f"{flag_name} is not valid JSON.") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="pr_files.py",
        description="Classify GitHub pull request files JSON without emitting file names.",
    )
    parser.add_argument("--changed-files", type=int, default=None)
    parser.add_argument("--draft", type=str, default=None)
    parser.add_argument("--base-ref", type=str, default=None)
    parser.add_argument("--task-folders", type=str, default=None)
    parser.add_argument("--main-task-folders", type=str, default=None)
    parser.add_argument("--main-test-entries", type=str, default=None)
    try:
        ns = parser.parse_args(argv)
        cli_cf: object = _UNSET if ns.changed_files is None else ns.changed_files
        cli_draft: object = _UNSET if ns.draft is None else _parse_bool_arg(ns.draft)
        cli_base_ref: object = _UNSET if ns.base_ref is None else ns.base_ref
        cli_tasks = _parse_json_arg(ns.task_folders, "--task-folders")
        cli_main_tasks = _parse_json_arg(ns.main_task_folders, "--main-task-folders")
        cli_main_tests = _parse_json_arg(ns.main_test_entries, "--main-test-entries")
        raw_stdin = sys.stdin.buffer.read()
        (
            entries,
            changed_files,
            draft,
            base_ref,
            task_folders,
            main_task_folders,
            main_test_entries,
        ) = _split_streamed_input(
            raw_stdin,
            cli_cf,
            cli_draft,
            cli_base_ref,
            cli_tasks,
            cli_main_tasks,
            cli_main_tests,
        )
        verdict = classify_pr_files(
            entries,
            changed_files=changed_files,
            draft=draft,
            base_ref=base_ref,
            task_folders=task_folders,
            main_task_folders=main_task_folders,
            main_test_entries=main_test_entries,
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
