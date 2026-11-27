"""Tests of the check on every commit of a pull request or a push.

Each test builds a small history in a temporary repository, runs check.py on it as the workflow does,
and reads its exit code and what it prints. The made-up offending texts are put together from parts, so
that this file does not trip the check itself.

Run: python -m unittest discover -s .github/commit-check
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parent / "check.py"

# The project's kind of address, and the address GitHub uses for the merge commits it makes.
OWN_LOGIN = "someone-made-up"
OWN = f"1234567+{OWN_LOGIN}@users.noreply.github.com"
OLD_STYLE = "another-made-up@users.noreply.github.com"
GITHUB = "noreply@github.com"
PERSONAL = "someone" + "@" + "example.org"

# Offending texts, from parts.
TRACKER_CODE = "D" + "OD-" + "17"
TRACKER_CODE_LOWER = "d" + "od-" + "4"
TRACKER_ADDRESS = "https://" + "linear" + ".app/made-up/issue/x"
HOME_PATH = "/" + "Users/" + "somebody-local/work/notes.md"
TOOL_PATH = "/" + "private/tmp/" + "claude-" + "501/x/scratchpad/notes.txt"
PROJECT_SLUG = "~/." + "claude/" + "projects/-" + "Users-somebody-orca-projects-x/memory"
WORKSPACE_PATH = "x/" + "orca/" + "workspaces/" + "dodam/commit-check"
TOOLING_ID = "term" + "_" + "69feae12-f296-4431"
TASK_ID = "task" + "_" + "a96f649fe6a9"
AI_COAUTHOR = "Co-" + "Authored-" + "By: " + "Cl" + "aude <noreply@" + "anthropic.com>"
AI_COAUTHOR_LOWER = "co-" + "authored-" + "by: " + "Some Codex <someone@example.org>"
HUMAN_COAUTHOR_PERSONAL = "Co-" + "authored-" + "by: A Person <" + PERSONAL + ">"
HUMAN_COAUTHOR_NOREPLY = "Co-" + "authored-" + "by: A Person <" + OWN + ">"
GENERATED = "\N{ROBOT FACE} " + "Gener" + "ated with " + "[Cl" + "aude Code](https://example.org)"
GENERATED_PLAIN = "Gener" + "ated by " + "Chat" + "GPT"
PRIVATE_WORD = "Zorb" + "latt"  # stands for a name kept out of public text
PRIVATE_PATTERN = "(?i)zorbl" + "att|quix" + "otl"


class Repo:
    """A temporary repository with commits made under chosen names and addresses."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.counter = 0

    def git(self, *args, env=None, input_text=None):
        full = dict(os.environ)
        for key in list(full):
            if key.startswith("GIT_"):
                del full[key]
        full.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        if env:
            full.update(env)
        done = subprocess.run(["git", *args], cwd=self.root, env=full, capture_output=True, text=True,
                              input=input_text)
        if done.returncode != 0:
            raise AssertionError(f"git {args} failed: {done.stderr}")
        return done.stdout.strip()

    def commit(self, message="Add a file", files=None, author=(OWN_LOGIN, OWN), committer=None,
               remove=()):
        self.counter += 1
        if files is None:
            files = {f"file{self.counter}.txt": f"line {self.counter}\n"}
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git("add", "--", name)
        for name in remove:
            self.git("rm", "-q", "--", name)
        committer = committer or author
        env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD")


class CheckTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = Repo(self.dir / "repo")
        # The commit before which everything is exempt.
        self.base = self.repo.commit("The base commit")

    def settings(self, expected=False, base=None):
        path = self.dir / "settings.json"
        path.write_text(json.dumps({"exempt_through": base or self.base,
                                    "private_pattern_expected": expected}))
        return path

    def run_check(self, start, head=None, *, context="own", pattern=None, expected=False, event=None,
                  verified=(), extra=()):
        head = head or self.repo.head()
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", start,
                "--head", head, "--settings", str(self.settings(expected))]
        args += ["--context", context]
        if event is not None:
            event_path = self.dir / "event.json"
            event_path.write_text(json.dumps(event))
            args += ["--event", str(event_path)]
        verified_path = self.dir / "verified.txt"
        verified_path.write_text("".join(f"{sha}\n" for sha in verified))
        args += ["--verified-shas", str(verified_path), *extra]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GITHUB_", "COMMIT_CHECK_", "GIT_"))}
        if pattern is not None:
            env["COMMIT_CHECK_PRIVATE_PATTERN"] = pattern
        done = subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)
        return done.returncode, done.stdout + done.stderr

    def assert_fails(self, result, sha, prop, hidden=()):
        code, out = result
        self.assertEqual(code, 1, out)
        self.assertIn(f"commit {sha[:12]}: {prop}", out)
        for text in hidden:
            self.assertNotIn(text, out)

    def assert_passes(self, result):
        code, out = result
        self.assertEqual(code, 0, out)

    # A clean history passes, and the count of commits checked is printed.

    def test_a_clean_history_passes_and_says_how_many_commits_it_checked(self):
        self.repo.commit("First change")
        self.repo.commit("Second change", author=("another-made-up", OLD_STYLE))
        code, out = self.run_check(self.base)
        self.assertEqual(code, 0, out)
        self.assertIn("2 commits checked", out)

    # Addresses.

    def test_a_personal_author_address_fails(self):
        sha = self.repo.commit("A change", author=("someone", PERSONAL),
                               committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(self.base), sha, "author address", hidden=[PERSONAL])

    def test_a_personal_committer_address_fails(self):
        sha = self.repo.commit("A change", committer=("someone", PERSONAL))
        self.assert_fails(self.run_check(self.base), sha, "committer address", hidden=[PERSONAL])

    def test_an_address_that_only_ends_like_the_no_reply_address_fails(self):
        address = "1+x@users.noreply.github.com.example.org"
        sha = self.repo.commit("A change", author=("x", address), committer=("x", address))
        self.assert_fails(self.run_check(self.base), sha, "author address")

    def test_github_as_author_fails(self):
        sha = self.repo.commit("A change", author=("GitHub", GITHUB), committer=("GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[]), sha, "author address")

    def test_github_as_committer_without_github_s_signature_fails(self):
        sha = self.repo.commit("Merge a change", committer=("GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[]), sha, "committer address")

    def test_github_as_committer_fails_when_github_s_api_cannot_be_asked(self):
        sha = self.repo.commit("Merge a change", committer=("GitHub", GITHUB))
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", self.base,
                "--head", sha, "--settings", str(self.settings()), "--context", "own"]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GITHUB_", "COMMIT_CHECK_", "GIT_"))}
        env.update({"GITHUB_TOKEN": "made-up", "GITHUB_REPOSITORY": "made-up/repo",
                    "GITHUB_API_URL": "http://127.0.0.1:9"})
        done = subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)
        self.assert_fails((done.returncode, done.stdout + done.stderr), sha, "committer address")

    def test_github_as_committer_with_github_s_verified_signature_passes(self):
        sha = self.repo.commit("Merge a change", author=("Display Name", OWN), committer=("GitHub", GITHUB))
        self.assert_passes(self.run_check(self.base, verified=[sha]))

    # Names.

    def test_a_name_that_is_not_the_login_of_its_no_reply_address_and_names_an_ai_fails(self):
        sha = self.repo.commit("A change", author=("Cl" + "aude", OWN), committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(self.base), sha, "author name")

    def test_a_committer_name_that_matches_the_private_pattern_fails_without_showing_it(self):
        sha = self.repo.commit("A change", author=(OWN_LOGIN, OWN), committer=(PRIVATE_WORD + " X", OWN))
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha, "committer name",
                          hidden=[PRIVATE_WORD])

    def test_the_login_of_the_address_passes_as_the_name_even_when_the_private_pattern_holds_it(self):
        self.repo.commit("A change", author=(OWN_LOGIN, OWN))
        self.assert_passes(self.run_check(self.base, pattern="(?i)someone-made-" + "up"))

    def test_the_author_name_github_puts_in_its_own_verified_commit_is_left_alone(self):
        sha = self.repo.commit("Merge a change", author=(PRIVATE_WORD, OWN), committer=("GitHub", GITHUB))
        self.assert_passes(self.run_check(self.base, pattern=PRIVATE_PATTERN, verified=[sha]))

    def test_a_commit_github_signed_must_carry_the_committer_name_github(self):
        sha = self.repo.commit("Merge a change", author=(OWN_LOGIN, OWN), committer=("Not GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[sha]), sha, "committer name")

    # Lines about an AI in the message.

    def test_an_ai_co_author_line_fails(self):
        sha = self.repo.commit("A change\n\n" + AI_COAUTHOR)
        self.assert_fails(self.run_check(self.base), sha, "AI co-author line", hidden=[AI_COAUTHOR])

    def test_an_ai_co_author_line_in_lower_case_fails(self):
        sha = self.repo.commit("A change\n\n" + AI_COAUTHOR_LOWER)
        self.assert_fails(self.run_check(self.base), sha, "AI co-author line")

    def test_a_human_co_author_with_a_personal_address_fails(self):
        sha = self.repo.commit("A change\n\n" + HUMAN_COAUTHOR_PERSONAL)
        self.assert_fails(self.run_check(self.base), sha, "co-author address", hidden=[PERSONAL])

    def test_a_human_co_author_with_a_no_reply_address_passes(self):
        self.repo.commit("A change\n\n" + HUMAN_COAUTHOR_NOREPLY)
        self.assert_passes(self.run_check(self.base))

    def test_a_generated_with_line_fails(self):
        sha = self.repo.commit("A change\n\n" + GENERATED)
        self.assert_fails(self.run_check(self.base), sha, "generated-with line")

    def test_a_generated_by_line_naming_an_ai_fails(self):
        sha = self.repo.commit("A change\n\n" + GENERATED_PLAIN)
        self.assert_fails(self.run_check(self.base), sha, "generated-with line")

    def test_generated_by_a_tool_that_is_not_an_ai_passes(self):
        self.repo.commit("Declare it once, with the code generated by the compiler")
        self.assert_passes(self.run_check(self.base))

    # Work item numbers, the tracker's address, paths of this computer, ids of the sessions' tooling.

    def test_a_tracker_code_in_the_message_fails(self):
        sha = self.repo.commit(f"Keep the list short ({TRACKER_CODE})")
        self.assert_fails(self.run_check(self.base), sha, "tracker code", hidden=[TRACKER_CODE])

    def test_a_tracker_code_in_lower_case_in_an_added_line_fails(self):
        sha = self.repo.commit("A change", files={"notes.md": f"one\nsee {TRACKER_CODE_LOWER}\n"})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file notes.md line 2)")

    def test_the_tracker_s_address_in_the_message_fails(self):
        sha = self.repo.commit(f"A change\n\nSee {TRACKER_ADDRESS}")
        self.assert_fails(self.run_check(self.base), sha, "tracker address (message line 3)")

    def test_the_tracker_s_address_in_an_added_line_fails(self):
        sha = self.repo.commit("A change", files={"a.py": f"URL = '{TRACKER_ADDRESS}'\n"})
        self.assert_fails(self.run_check(self.base), sha, "tracker address (file a.py line 1)")

    def test_a_home_folder_path_in_the_message_fails(self):
        sha = self.repo.commit(f"A change\n\nThe notes are in {HOME_PATH}")
        self.assert_fails(self.run_check(self.base), sha, "local path", hidden=[HOME_PATH])

    def test_a_folder_the_project_names_under_the_home_folder_passes_in_the_message(self):
        self.repo.commit("Keep the guard's file in ~/.dodam, one per user")
        self.assert_passes(self.run_check(self.base))

    def test_a_path_the_session_tools_leave_fails_in_an_added_line(self):
        for text in (TOOL_PATH, PROJECT_SLUG, WORKSPACE_PATH):
            with self.subTest(text=text):
                sha = self.repo.commit("A change", files={"b.md": f"see {text}\n"})
                self.assert_fails(self.run_check(sha + "~1"), sha, "local path (file b.md line 1)",
                                  hidden=[text])

    def test_a_temporary_folder_in_an_added_line_of_a_test_passes(self):
        self.repo.commit("A change", files={"test_x.py": "ROOT = '/tmp/x'\nHOME = '/home/user'\n"})
        self.assert_passes(self.run_check(self.base))

    def test_an_id_of_the_session_tooling_fails_in_the_message_and_in_an_added_line(self):
        first = self.repo.commit(f"A change for {TOOLING_ID}")
        second = self.repo.commit("A change", files={"c.txt": f"{TASK_ID}\n"})
        code, out = self.run_check(self.base)
        self.assert_fails((code, out), first, "tooling id (message line 1)")
        self.assert_fails((code, out), second, "tooling id (file c.txt line 1)")

    def test_a_tracker_code_in_the_path_of_an_empty_new_file_fails(self):
        sha = self.repo.commit("A change", files={f"notes/{TRACKER_CODE}.md": ""})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file path)", hidden=[TRACKER_CODE])

    def test_a_tracker_code_in_the_new_path_of_a_renamed_file_fails(self):
        self.repo.commit("A change", files={"plain.md": "some text that stays the same\n"})
        start = self.repo.head()
        self.repo.git("mv", "plain.md", f"{TRACKER_CODE}.md")
        sha = self.repo.commit("Rename", files={})
        self.assert_fails(self.run_check(start), sha, "tracker code (file path)")

    # The private pattern.

    def test_the_private_pattern_in_the_message_fails_without_showing_the_text(self):
        sha = self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha,
                          "private pattern (message line 1)", hidden=[PRIVATE_WORD, PRIVATE_PATTERN])

    def test_the_private_pattern_in_an_added_line_fails_without_showing_the_file(self):
        sha = self.repo.commit("A change", files={"docs/where.md": f"text\n{PRIVATE_WORD.lower()}\n"})
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha,
                          "private pattern (an added line)", hidden=[PRIVATE_WORD.lower(), "where.md"])

    def test_the_private_pattern_in_a_file_path_fails_without_showing_the_path(self):
        sha = self.repo.commit("A change", files={f"{PRIVATE_WORD}.md": ""})
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha,
                          "private pattern (a file path)", hidden=[PRIVATE_WORD])

    def test_without_the_secret_and_while_it_is_not_expected_the_check_passes_with_a_notice(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern=None, expected=False)
        self.assertEqual(code, 0, out)
        self.assertIn("::notice", out)
        self.assertIn("private pattern", out)

    def test_without_the_secret_once_it_is_expected_the_check_fails_on_the_project_s_own_branches(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern="", expected=True, context="own")
        self.assertEqual(code, 1, out)
        self.assertIn("private pattern", out)

    def test_while_the_secret_is_not_expected_a_fork_s_pull_request_passes_with_a_notice(self):
        self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        code, out = self.run_check(self.base, pattern=None, expected=False, context="fork")
        self.assertEqual(code, 0, out)
        self.assertIn("::notice", out)

    def test_once_the_secret_is_expected_a_fork_s_pull_request_fails_in_a_run_its_label_did_not_start(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern=PRIVATE_PATTERN, expected=True, context="fork")
        self.assertEqual(code, 1, out)
        self.assertIn("private pattern", out)

    def test_a_fork_s_pull_request_is_never_held_to_the_private_pattern_outside_the_labelled_run(self):
        self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        self.assert_passes(self.run_check(self.base, pattern=PRIVATE_PATTERN, expected=False, context="fork"))

    def test_in_the_run_the_project_s_label_started_a_fork_s_pull_request_is_held_to_the_private_pattern(self):
        sha = self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN, expected=True, context="fork-labeled"),
                          sha, "private pattern (message line 1)", hidden=[PRIVATE_WORD])

    def test_in_the_labelled_run_a_missing_secret_fails_once_it_is_expected(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern=None, expected=True, context="fork-labeled")
        self.assertEqual(code, 1, out)

    def test_a_private_pattern_that_is_no_regular_expression_fails_without_showing_it(self):
        self.repo.commit("A change")
        broken = "(unclosed" + "secretpart"
        code, out = self.run_check(self.base, pattern=broken)
        self.assertEqual(code, 1, out)
        self.assertNotIn("secretpart", out)

    # Merges, the exempt history and the range as a whole.

    def test_a_line_that_a_merge_adds_beyond_both_parents_fails(self):
        start = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side", files={"s.txt": "side\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", files={"m.txt": "main\n"})
        env = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN,
               "GIT_COMMITTER_NAME": OWN_LOGIN, "GIT_COMMITTER_EMAIL": OWN}
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "side", env=env)
        (self.repo.root / "m.txt").write_text(f"main\n{TRACKER_CODE}\n")
        self.repo.git("add", "m.txt")
        self.repo.git("commit", "-q", "-m", "Merge side", env=env)
        merge = self.repo.head()
        self.assert_fails(self.run_check(start), merge, "tracker code (file m.txt line 2)")

    def test_a_merge_that_brings_back_an_offending_line_from_the_exempt_history_fails(self):
        old = self.repo.commit("Old", files={"old.txt": f"{TRACKER_CODE}\n"})
        self.repo.commit("Take the old line out", files={}, remove=["old.txt"])
        base = self.repo.head()
        env = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN,
               "GIT_COMMITTER_NAME": OWN_LOGIN, "GIT_COMMITTER_EMAIL": OWN}
        self.repo.git("checkout", "-q", "-b", "old-side", old)
        self.repo.git("checkout", "-q", "main")
        # A merge that keeps the old side's file: no line is new to both parents.
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "-s", "ours", "old-side", env=env)
        self.repo.git("checkout", "old-side", "--", "old.txt")
        self.repo.git("commit", "-q", "-m", "Merge the old side", env=env)
        merge = self.repo.head()
        path = self.dir / "settings2.json"
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", base,
                "--head", merge, "--settings", str(path), "--own", "--verified-shas", os.devnull]
        path.write_text(json.dumps({"exempt_through": base, "private_pattern_expected": False}))
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, out)
        self.assertIn("tracker code (the range's difference, file old.txt line 1)", out)

    def test_an_offending_commit_up_to_the_exempt_commit_is_not_checked(self):
        self.repo.commit(f"Old work ({TRACKER_CODE})", author=("someone", PERSONAL))
        exempt = self.repo.head()
        self.repo.commit("New work")
        args_settings = self.dir / "settings3.json"
        args_settings.write_text(json.dumps({"exempt_through": exempt, "private_pattern_expected": False}))
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root),
                               "--from", self.base, "--head", self.repo.head(),
                               "--settings", str(args_settings), "--own", "--verified-shas", os.devnull],
                              capture_output=True, text=True, timeout=60)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        self.assertIn("1 commits checked", out)

    def test_an_exempt_commit_that_is_not_in_the_history_fails(self):
        self.repo.commit("A change")
        path = self.dir / "settings4.json"
        path.write_text(json.dumps({"exempt_through": "0" * 40, "private_pattern_expected": False}))
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root),
                               "--from", self.base, "--head", self.repo.head(), "--settings", str(path),
                               "--own", "--verified-shas", os.devnull],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    # The pull request's title and description.

    def test_the_pull_request_s_title_and_description_are_checked(self):
        self.repo.commit("A change")
        cases = [({"title": f"Fix ({TRACKER_CODE})", "body": ""}, "tracker code (pull request title)"),
                 ({"title": "Fix", "body": f"one\n{HOME_PATH}"}, "local path (pull request description line 2)"),
                 ({"title": "Fix", "body": GENERATED}, "generated-with line (pull request description line 1)"),
                 ({"title": "Fix", "body": TRACKER_ADDRESS}, "tracker address (pull request description line 1)"),
                 ({"title": "Fix", "body": TASK_ID}, "tooling id (pull request description line 1)"),
                 ({"title": PRIVATE_WORD, "body": None}, "private pattern (pull request title)"),
                 # GitHub writes the branch's name into the message of the merge commit it makes on main.
                 ({"title": "Fix", "body": "", "head": {"ref": f"{TRACKER_CODE_LOWER}-notes"}},
                  "tracker code (pull request branch name)")]
        for pull, expected in cases:
            with self.subTest(expected=expected):
                code, out = self.run_check(self.base, pattern=PRIVATE_PATTERN,
                                           event={"pull_request": {"number": 7, **pull}})
                self.assertEqual(code, 1, out)
                self.assertIn(f"pull request: {expected}", out)
                self.assertNotIn(PRIVATE_WORD, out)

    def test_a_pull_request_with_no_commit_to_check_fails(self):
        code, out = self.run_check(self.base, self.base, event={"pull_request": {"number": 7, "title": "x",
                                                                                 "body": ""}})
        self.assertEqual(code, 1, out)
        self.assertIn("no commit", out)

    def test_several_problems_are_all_named(self):
        first = self.repo.commit(f"One ({TRACKER_CODE})")
        second = self.repo.commit("Two", author=("someone", PERSONAL), committer=("someone", PERSONAL))
        code, out = self.run_check(self.base)
        self.assertEqual(code, 1, out)
        self.assertIn(f"commit {first[:12]}: tracker code", out)
        self.assertIn(f"commit {second[:12]}: author address", out)
        self.assertIn(f"commit {second[:12]}: committer address", out)

    # The check's own files do not trip it.

    def test_the_check_s_own_files_added_in_a_commit_do_not_trip_it(self):
        here = Path(__file__).resolve().parent
        files = {f"x/{path.name}": path.read_text() for path in sorted(here.iterdir())
                 if path.suffix in (".py", ".json")}
        for name in ("pull-requests.yml", "ci.yml"):
            files[f"x/{name}"] = (here.parent / "workflows" / name).read_text()
        self.repo.commit("Add the check", files=files)
        self.assert_passes(self.run_check(self.base, pattern=PRIVATE_PATTERN))


if __name__ == "__main__":
    unittest.main()
