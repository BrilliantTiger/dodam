"""Tests of the check on every commit of a pull request or a push, and of the check of a fork's paths.

Each test builds a small history in a temporary repository, runs check.py on it as the workflows do, and reads
its exit code and what it prints. The made-up offending texts are put together from parts, so that this file
does not trip the check itself.

Run: python3 -I -m unittest discover -s .github/commit-check
"""

import http.server
import json
import re
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE / "check.py"
FORK_PATHS = HERE / "fork_paths.py"

# The project's kind of address, and the address GitHub uses for the commits it makes.
OWN_LOGIN = "someone-made-up"
OWN = f"1234567+{OWN_LOGIN}@users.noreply.github.com"
OLD_STYLE = "another-made-up@users.noreply.github.com"
GITHUB = "noreply" + "@" + "github.com"
PERSONAL = "someone" + "@" + "example.org"

# Offending texts, from parts.
C = "Cl" + "aude"
T = "D" + "OD"
TRACKER_CODE = T + "-" + "17"
TRACKER_CODE_LOWER = T.lower() + "-" + "4"
TRACKER_ADDRESS = "https://" + "lin" + "ear" + ".app/made-up/issue/x"
HOME_PATH = "/" + "Us" + "ers/" + "somebody-local/work/notes.md"
TOOL_PATH = "/" + "private/tmp/" + "cla" + "ude-" + "501/x/scratchpad/notes.txt"
PROJECT_SLUG = "~/." + "cla" + "ude/" + "projects/-" + "Us" + "ers-somebody-or" + "ca-projects-x/memory"
WORKSPACE_PATH = "x/" + "or" + "ca/" + "work" + "spaces/" + "dodam/commit-check"
TOOLING_ID = "te" + "rm" + "_" + "69feae12-f296-4431"
TASK_ID = "ta" + "sk" + "_" + "a96f649fe6a9"
AI_COAUTHOR = "Co-" + "Authored-" + "By: " + C + " <noreply@" + "anthrop" + "ic.com>"
HUMAN_COAUTHOR_PERSONAL = "Co-" + "authored-" + "by: A Person <" + PERSONAL + ">"
HUMAN_COAUTHOR_NOREPLY = "Co-" + "authored-" + "by: A Person <" + OWN + ">"
GENERATED = "\N{ROBOT FACE} " + "Gener" + "ated with " + "[" + C + " Code](https://example.org)"
OWN_EVENT = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                              "head": {"ref": "fix", "repo": {"owner": {"login": "SomeOrg"}}}},
             "repository": {"owner": {"login": "SomeOrg"}}}
BUILT_AUTHOR_NAME = r"commit [0-9a-f]{12}: author name"
SILENT = "::error::GitHub's API did not answer"
PRIVATE_WORD = "Zorb" + "latt"  # stands for a name kept out of public text
PRIVATE_PATTERN = "(?i)zorbl" + "att|quix" + "otl"
FORK_WORD = "Quix" + "otl"
MARKER = "Mq" + "7xVr" + "Kp"  # a made-up word that the check must never print


class Repo:
    """A temporary repository with commits made under chosen names and addresses."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.counter = 0

    def git(self, *args, env=None):
        full = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        full.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        full.update(env or {})
        done = subprocess.run(["git", *args], cwd=self.root, env=full, capture_output=True)
        if done.returncode != 0:
            raise AssertionError(f"git {args} failed: {done.stderr!r}")
        return done.stdout.decode("utf-8", "replace").strip()

    def commit(self, message="Add a file", files=None, author=(OWN_LOGIN, OWN), committer=None, remove=()):
        self.counter += 1
        if files is None:
            files = {f"file{self.counter}.txt": f"line {self.counter}\n"}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
            self.git("add", "-f", "--", name)
        for name in remove:
            self.git("rm", "-q", "--", name)
        committer = committer or author
        env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD")


class FakeGitHub:
    """GitHub's API for the paths a test names: each answer is JSON, or a number for an HTTP error."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.asked = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fake.asked.append(self.path)
                answer = fake.answers.get(self.path, 404)
                if answer == "hang":  # GitHub that takes the question and does not answer in time
                    time.sleep(3)
                    answer = 502
                if isinstance(answer, int):
                    self.send_error(answer)
                    return
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {"GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
                    "GITHUB_REPOSITORY": "made-up/repo", "GITHUB_TOKEN": "made-up"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


REVIEWED = {"name": "private-check", "protection_rules": [{"type": "required_reviewers", "reviewers": [
    {"type": "User", "reviewer": {"login": "made-up"}}]}]}
NOBODY = {"name": "private-check", "protection_rules": [{"type": "required_reviewers", "reviewers": []}]}
UNREVIEWED = {"name": "private-check", "protection_rules": [{"type": "branch_policy"}]}
ENVIRONMENT_PATH = "/repos/made-up/repo/environments/private-check"


def clean_env(extra=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GITHUB_", "COMMIT_CHECK_", "FORK_", "GIT_"))}
    env.update(extra or {})
    return env


class RepoTestCase(unittest.TestCase):
    """A temporary repository and the check run on it as the workflows run it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = Repo(self.dir / "repo")
        self.base = self.repo.commit("The base commit")

    def settings(self, expected=False, base=None, open_way=False, held="2026-11-27"):
        path = self.dir / "settings.json"
        path.write_text(json.dumps({"exempt_through": base or self.base, "private_pattern_expected": expected,
                                    "fork_way_open": open_way, "outside_runs_held_since": held}))
        return path

    def run_check(self, start, head=None, *, context="own", pattern=None, fork_pattern=None, expected=False,
                  event=None, verified=(), extra=(), env_extra=None, open_way=False, held="2026-11-27",
                  compare=None):
        head = head or self.repo.head()
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", start, "--head", head,
                "--settings", str(self.settings(expected, open_way=open_way, held=held)), "--context", context,
                "--compare-settings", compare or start]
        if event is not None:
            event_path = self.dir / "event.json"
            event_path.write_text(json.dumps(event))
            args += ["--event", str(event_path)]
        verified_path = self.dir / "verified.txt"
        verified_path.write_text("".join(f"{sha}\n" for sha in verified))
        args += ["--verified-shas", str(verified_path), *extra]
        env = clean_env(env_extra)
        if pattern is not None:
            env["COMMIT_CHECK_PRIVATE_PATTERN"] = pattern
        if fork_pattern is not None:
            env["FORK_CHECK_PRIVATE_PATTERN"] = fork_pattern
        done = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def fake(self, answers):
        fake = FakeGitHub(answers)
        self.addCleanup(fake.close)
        return fake

    def assert_fails(self, result, sha, prop, hidden=()):
        code, out = result
        self.assertEqual(code, 1, out)
        self.assertIn(f"commit {sha[:12]}: {prop}", out)
        for text in hidden:
            self.assertNotIn(text, out)

    def assert_passes(self, result):
        code, out = result
        self.assertEqual(code, 0, out)


class CheckTest(RepoTestCase):

    def test_a_clean_history_passes_and_says_how_many_commits_it_checked(self):
        self.repo.commit("First change")
        self.repo.commit("Second change", author=("another-made-up", OLD_STYLE))
        code, out = self.run_check(self.base)
        self.assertEqual(code, 0, out)
        self.assertIn("2 commits checked", out)

    # Addresses.

    def test_a_personal_author_address_fails(self):
        sha = self.repo.commit("A change", author=("someone", PERSONAL), committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(self.base), sha, "author address", hidden=[PERSONAL])

    def test_a_personal_committer_address_fails(self):
        sha = self.repo.commit("A change", committer=("someone", PERSONAL))
        self.assert_fails(self.run_check(self.base), sha, "committer address", hidden=[PERSONAL])

    def test_an_address_that_only_ends_like_the_no_reply_address_fails(self):
        address = "1+x@users.noreply.github.com.example.org"
        sha = self.repo.commit("A change", author=("x", address), committer=("x", address))
        self.assert_fails(self.run_check(self.base), sha, "author address")

    def test_a_bot_s_no_reply_address_fails(self):
        address = "158243242+some-agent[bot]@users.noreply.github.com"
        sha = self.repo.commit("A change", author=("some-agent[bot]", address), committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(self.base), sha, "author address")

    def test_github_as_author_fails(self):
        sha = self.repo.commit("A change", author=("GitHub", GITHUB), committer=("GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[]), sha, "author address")

    def test_github_as_committer_without_github_s_signature_fails(self):
        sha = self.repo.commit("Merge a change", committer=("GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[]), sha, "committer address")

    def test_github_as_committer_fails_when_github_s_api_cannot_be_asked(self):
        sha = self.repo.commit("Merge a change", committer=("GitHub", GITHUB))
        check = load("check.py")
        args = ["--repo", str(self.repo.root), "--from", self.base, "--head", sha, "--settings", str(self.settings()),
                "--context", "own", "--compare-settings", self.base]
        env = {"GITHUB_TOKEN": "made-up", "GITHUB_REPOSITORY": "made-up/repo", "GITHUB_API_URL": "http://127.0.0.1:9"}
        code, out = in_process(check.main, args, env, check)
        self.assertEqual(code, 1, out)
        self.assertIn(SILENT, out)  # GitHub silent is said as such, never as a broken rule

    def test_github_as_committer_is_believed_only_on_what_github_s_api_says(self):
        sha = self.repo.commit("Merge a change", author=("Display Name", OWN), committer=("GitHub", GITHUB))
        cases = [({"sha": sha, "commit": {"verification": {"verified": True}, "committer": {"email": GITHUB}}}, 0),
                 ({"sha": sha, "commit": {"verification": {"verified": False}, "committer": {"email": GITHUB}}}, 1),
                 ({"sha": sha, "commit": {"verification": {"verified": True}, "committer": {"email": PERSONAL}}}, 1),
                 ({"sha": "0" * 40, "commit": {"verification": {"verified": True}, "committer": {"email": GITHUB}}}, 1),
                 ({"sha": sha, "commit": {"verification": "yes", "committer": {"email": GITHUB}}}, 1),
                 (404, 1)]
        for answer, expected in cases:
            with self.subTest(answer=answer):
                fake = FakeGitHub({f"/repos/made-up/repo/commits/{sha}": answer})
                args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", self.base,
                        "--head", sha, "--settings", str(self.settings()), "--context", "own",
                        "--compare-settings", self.base]
                done = subprocess.run(args, capture_output=True, text=True, env=clean_env(fake.env), timeout=120)
                fake.close()
                self.assertEqual(done.returncode, expected, done.stdout)
                if expected:
                    self.assertIn(f"commit {sha[:12]}: committer address", done.stdout)

    def test_github_as_committer_with_github_s_verified_signature_passes(self):
        sha = self.repo.commit("Merge a change", author=("Display Name", OWN), committer=("GitHub", GITHUB))
        self.assert_passes(self.run_check(self.base, verified=[sha]))

    # Names.

    def test_a_name_that_names_an_ai_fails(self):
        for name in (C, C.lower() + "_code", C + "Bot", "GPT-4o", "Some " + C + "와", "a-helper-b" + "ot"):
            with self.subTest(name=name):
                sha = self.repo.commit("A change", author=(name, OWN), committer=(OWN_LOGIN, OWN))
                self.assert_fails(self.run_check(sha + "~1"), sha, "author name")

    def test_a_login_that_names_an_ai_is_not_spared(self):
        address = "175728472+Co" + "pilot@users.noreply.github.com"
        sha = self.repo.commit("A change", author=("Co" + "pilot", address), committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(self.base), sha, "author name")

    def test_a_committer_name_that_matches_the_private_pattern_fails_without_showing_it(self):
        sha = self.repo.commit("A change", author=(OWN_LOGIN, OWN), committer=(PRIVATE_WORD + " X", OWN))
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha, "committer name",
                          hidden=[PRIVATE_WORD])

    def test_the_login_of_the_address_passes_as_the_name_even_when_the_private_pattern_holds_it(self):
        self.repo.commit("A change", author=(OWN_LOGIN, OWN))
        self.assert_passes(self.run_check(self.base, pattern="(?i)someone-made-" + "up"))

    def test_the_author_name_github_puts_on_a_commit_it_signed_is_held_to_the_rules_for_names(self):
        for name, pattern in ((PRIVATE_WORD, PRIVATE_PATTERN), (C + " Code", None)):
            with self.subTest(name=name):
                sha = self.repo.commit("Merge a change", author=(name, OWN), committer=("GitHub", GITHUB))
                self.assert_fails(self.run_check(sha + "~1", pattern=pattern, verified=[sha]), sha, "author name",
                                  hidden=[PRIVATE_WORD])

    def test_a_commit_github_signed_must_carry_the_committer_name_github(self):
        sha = self.repo.commit("Merge a change", author=(OWN_LOGIN, OWN), committer=("Not GitHub", GITHUB))
        self.assert_fails(self.run_check(self.base, verified=[sha]), sha, "committer name")

    # Attribution in the message.

    def test_an_ai_co_author_line_fails(self):
        sha = self.repo.commit("A change\n\n" + AI_COAUTHOR)
        self.assert_fails(self.run_check(self.base), sha, "attribution line (message line 3)", hidden=[AI_COAUTHOR])

    def test_a_human_co_author_with_a_personal_address_fails(self):
        sha = self.repo.commit("A change\n\n" + HUMAN_COAUTHOR_PERSONAL)
        self.assert_fails(self.run_check(self.base), sha, "attribution line (message line 3)", hidden=[PERSONAL])

    def test_a_human_co_author_with_a_no_reply_address_passes(self):
        self.repo.commit("A change\n\n" + HUMAN_COAUTHOR_NOREPLY)
        self.assert_passes(self.run_check(self.base))

    def test_a_generated_with_line_fails(self):
        sha = self.repo.commit("A change\n\n" + GENERATED)
        self.assert_fails(self.run_check(self.base), sha, "attribution line (message line 3)")

    def test_generated_by_a_tool_inside_a_sentence_passes(self):
        self.repo.commit("Declare it once, with the code generated by the compiler")
        self.assert_passes(self.run_check(self.base))

    def test_a_heading_of_two_words_passes_and_a_trailer_with_a_hyphen_at_the_end_fails(self):
        self.repo.commit("Case runner: the steps keep their order\n\nSecond-lines: one more test here\n\nMore words.")
        sha = self.repo.commit("A change\n\nMore words.\n\nTested-" + "with: a laptop")
        code, out = self.run_check(self.base)
        self.assertEqual(code, 1, out)
        self.assertIn(f"commit {sha[:12]}: attribution line (message line 5)", out)
        self.assertEqual(out.count("attribution line"), 1, out)

    # Work item numbers, the tracker's address, paths of this computer, ids of the sessions' tooling.

    def test_a_tracker_code_in_the_message_fails(self):
        sha = self.repo.commit(f"Keep the list short ({TRACKER_CODE})")
        self.assert_fails(self.run_check(self.base), sha, "tracker code (message line 1)", hidden=[TRACKER_CODE])

    def test_a_tracker_code_in_an_added_line_fails(self):
        sha = self.repo.commit("A change", files={"notes.md": f"one\nsee {TRACKER_CODE_LOWER}\n"})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file 1, line 2)")

    def test_the_tracker_s_address_in_the_message_and_in_an_added_line_fails(self):
        first = self.repo.commit(f"A change\n\nSee {TRACKER_ADDRESS}")
        second = self.repo.commit("A change", files={"a.py": f"URL = '{TRACKER_ADDRESS}'\n"})
        code, out = self.run_check(self.base)
        self.assert_fails((code, out), first, "tracker address (message line 3)")
        self.assert_fails((code, out), second, "tracker address (file 1, line 1)")

    def test_a_home_folder_path_in_the_message_fails(self):
        sha = self.repo.commit(f"A change\n\nThe notes are in {HOME_PATH}")
        self.assert_fails(self.run_check(self.base), sha, "local path (message line 3)", hidden=[HOME_PATH])

    def test_a_folder_the_project_names_under_the_home_folder_passes_in_the_message(self):
        self.repo.commit("Keep the guard's file in ~/.dodam, one per user")
        self.assert_passes(self.run_check(self.base))

    def test_a_path_the_session_tools_leave_fails_in_an_added_line_and_in_the_message(self):
        for text in (TOOL_PATH, PROJECT_SLUG, WORKSPACE_PATH):
            with self.subTest(text=text):
                sha = self.repo.commit(f"Seen at {text}", files={"b.md": f"see {text}\n"})
                code, out = self.run_check(sha + "~1")
                self.assert_fails((code, out), sha, "local path (file 1, line 1)", hidden=[text])
                self.assert_fails((code, out), sha, "local path (message line 1)", hidden=[text])

    def test_a_temporary_folder_and_a_container_s_home_in_an_added_line_of_a_test_pass(self):
        self.repo.commit("A change", files={"test_x.py": "ROOT = '/tmp/x'\nHOME = '/home/agent'\n"})
        self.assert_passes(self.run_check(self.base))

    def test_an_id_of_the_session_tooling_fails_in_the_message_and_in_an_added_line(self):
        first = self.repo.commit(f"A change for {TOOLING_ID}")
        second = self.repo.commit("A change", files={"c.txt": f"{TASK_ID}\n"})
        code, out = self.run_check(self.base)
        self.assert_fails((code, out), first, "tooling id (message line 1)")
        self.assert_fails((code, out), second, "tooling id (file 1, line 1)")

    def test_each_rule_holds_in_the_path_of_an_empty_new_file(self):
        names = {f"notes/{TRACKER_CODE}.md": "tracker code", f"lin" "ear.app-notes/x.md": "tracker address",
                 f"x/{TASK_ID}.md": "tooling id", f"or" "ca/work" "spaces/x/y.md": "local path"}
        for name, prop in names.items():
            with self.subTest(prop=prop):
                sha = self.repo.commit("A change", files={name: ""})
                self.assert_fails(self.run_check(sha + "~1"), sha, f"{prop} (path 1)", hidden=[TRACKER_CODE, TASK_ID])

    def test_a_tracker_code_in_the_new_path_of_a_renamed_file_fails(self):
        self.repo.commit("A change", files={"plain.md": "some text that stays the same\n"})
        start = self.repo.head()
        self.repo.git("mv", "plain.md", f"{TRACKER_CODE}.md")
        sha = self.repo.commit("Rename", files={})
        self.assert_fails(self.run_check(start), sha, "tracker code (path 1)")

    # The private pattern.

    def test_the_private_pattern_in_the_message_fails_without_showing_the_text(self):
        sha = self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha,
                          "private pattern (message line 1)", hidden=[PRIVATE_WORD, PRIVATE_PATTERN])

    def test_the_private_pattern_in_an_added_line_and_in_a_path_fails_without_showing_either(self):
        sha = self.repo.commit("A change", files={"docs/where.md": f"text\n{PRIVATE_WORD.lower()}\n",
                                                  f"{PRIVATE_WORD}-notes.md": f"{TRACKER_CODE}\n"})
        code, out = self.run_check(self.base, pattern=PRIVATE_PATTERN)
        self.assert_fails((code, out), sha, "private pattern (file 2, line 2)",
                          hidden=[PRIVATE_WORD, PRIVATE_WORD.lower(), "where.md", TRACKER_CODE])
        self.assert_fails((code, out), sha, "private pattern (path 1)")
        self.assert_fails((code, out), sha, "tracker code (file 1, line 1)")

    def test_the_private_pattern_is_also_matched_on_the_text_brought_to_one_form(self):
        sha = self.repo.commit("Thanks to Zorb\u200blatt")
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), sha, "private pattern (message line 1)")

    def test_without_the_secret_and_while_it_is_not_expected_the_check_passes_with_a_notice(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern=None, expected=False)
        self.assertEqual(code, 0, out)
        self.assertIn("::notice", out)

    def test_without_the_secret_once_it_is_expected_the_check_fails_on_the_project_s_own_branches(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, pattern="", expected=True, context="own")
        self.assertEqual(code, 1, out)
        self.assertIn("private pattern", out)

    def test_the_fork_s_secret_is_not_the_project_s_secret(self):
        # A run of the project's own branch reads its own secret only; the fork's secret has a name of its own.
        sha = self.repo.commit(f"Thanks to {PRIVATE_WORD}")
        self.assert_passes(self.run_check(sha + "~1", pattern=None, fork_pattern=PRIVATE_PATTERN))

    def test_a_private_pattern_that_is_no_regular_expression_fails_without_showing_it(self):
        self.repo.commit("A change")
        broken = "(unclosed" + "secretpart"
        code, out = self.run_check(self.base, pattern=broken)
        self.assertEqual(code, 1, out)
        self.assertNotIn("secretpart", out)

    # A branch judged by its own copy cannot loosen the settings it is judged by.

    def committed_settings(self, expected, exempt=None):
        path = self.repo.root / ".github" / "commit-check" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"exempt_through": exempt or self.base, "private_pattern_expected": expected,
                                    "fork_way_open": False, "outside_runs_held_since": None}))
        self.repo.git("add", "--", str(path))
        return self.repo.commit("Settings", files={}), path

    def run_with_settings(self, start, settings):
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", start,
                "--head", self.repo.head(), "--settings", str(settings), "--context", "own",
                "--compare-settings", start, "--verified-shas", os.devnull]
        done = subprocess.run(args, capture_output=True, text=True,
                              env=clean_env({"COMMIT_CHECK_PRIVATE_PATTERN": PRIVATE_PATTERN}), timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_a_branch_that_says_the_secret_is_no_longer_expected_fails(self):
        main, path = self.committed_settings(True)
        self.committed_settings(False)
        code, out = self.run_with_settings(main, path)
        self.assertEqual(code, 1, out)
        self.assertIn("settings: the private pattern is expected on the range's start", out)

    def test_a_branch_that_moves_the_exempt_commit_fails(self):
        main, path = self.committed_settings(False)
        later = self.repo.commit("Later")
        self.committed_settings(False, exempt=later)
        code, out = self.run_with_settings(main, path)
        self.assertEqual(code, 1, out)
        self.assertIn("settings: the exempt commit differs from the range's start", out)

    def test_a_branch_that_starts_to_expect_the_secret_passes(self):
        main, path = self.committed_settings(False)
        self.committed_settings(True)
        self.assert_passes(self.run_with_settings(main, path))

    def test_a_start_without_settings_is_the_first_merge_and_passes(self):
        start = self.repo.head()
        _, path = self.committed_settings(False)
        self.assert_passes(self.run_with_settings(start, path))

    def test_settings_of_the_wrong_shape_fail_with_a_fixed_message(self):
        self.repo.commit("A change")
        shapes = ["[]", "{}", '{"exempt_through": "x", "private_pattern_expected": false, "fork_way_open": false, '
                  '"outside_runs_held_since": null}', json.dumps({"exempt_through": self.base,
                  "private_pattern_expected": "no", "fork_way_open": False, "outside_runs_held_since": None}),
                  json.dumps({"exempt_through": self.base, "private_pattern_expected": False, "fork_way_open": False,
                              "outside_runs_held_since": None, "more": 1}), "not json", ""]
        for shape in shapes:
            with self.subTest(shape=shape):
                path = self.dir / "odd.json"
                path.write_text(shape)
                args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", self.base,
                        "--head", "HEAD", "--settings", str(path), "--context", "own", "--compare-settings", self.base]
                done = subprocess.run(args, capture_output=True, text=True, env=clean_env(), timeout=120)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("::error::settings.json", done.stdout)
                self.assertNotIn("Traceback", done.stdout + done.stderr)

    # Merges, the exempt history and the range as a whole.

    def as_owner(self):
        return {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
                "GIT_COMMITTER_EMAIL": OWN}

    def test_a_line_that_a_merge_adds_beyond_both_parents_fails(self):
        start = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side", files={"s.txt": "side\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Main", files={"m.txt": "main\n"})
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "side", env=self.as_owner())
        (self.repo.root / "m.txt").write_text(f"main\n{TRACKER_CODE}\n")
        self.repo.git("add", "m.txt")
        self.repo.git("commit", "-q", "-m", "Merge side", env=self.as_owner())
        merge = self.repo.head()
        self.assert_fails(self.run_check(start), merge, "tracker code (file 1, line 2)")

    def test_a_merge_that_brings_back_a_line_or_a_path_from_the_exempt_history_fails(self):
        old = self.repo.commit("Old", files={"old.txt": f"{TRACKER_CODE}\n", f"notes/{TRACKER_CODE}.md": ""})
        self.repo.commit("Take the old things out", files={}, remove=["old.txt", f"notes/{TRACKER_CODE}.md"])
        base = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "old-side", old)
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "-s", "ours", "old-side", env=self.as_owner())
        self.repo.git("checkout", "old-side", "--", "old.txt", f"notes/{TRACKER_CODE}.md")
        self.repo.git("commit", "-q", "-m", "Merge the old side", env=self.as_owner())
        merge = self.repo.head()
        path = self.dir / "settings2.json"
        path.write_text(json.dumps({"exempt_through": base, "private_pattern_expected": False, "fork_way_open": False,
                                    "outside_runs_held_since": None}))
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", base, "--head", merge,
                "--settings", str(path), "--context", "own", "--compare-settings", base, "--verified-shas", os.devnull]
        done = subprocess.run(args, capture_output=True, text=True, env=clean_env(), timeout=120)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, out)
        self.assertIn("the range: tracker code (file 2, line 1)", out)
        self.assertIn("the range: tracker code (path 1)", out)

    def test_an_offending_commit_up_to_the_exempt_commit_is_not_checked(self):
        self.repo.commit(f"Old work ({TRACKER_CODE})", author=("someone", PERSONAL))
        exempt = self.repo.head()
        self.repo.commit("New work")
        path = self.dir / "settings3.json"
        path.write_text(json.dumps({"exempt_through": exempt, "private_pattern_expected": False,
                                    "fork_way_open": False, "outside_runs_held_since": None}))
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", self.base, "--head",
                self.repo.head(), "--settings", str(path), "--context", "own", "--compare-settings", self.base,
                "--verified-shas", os.devnull]
        done = subprocess.run(args, capture_output=True, text=True, env=clean_env(), timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("1 commits checked", done.stdout)

    def test_an_exempt_commit_that_is_not_in_the_history_fails(self):
        self.repo.commit("A change")
        path = self.dir / "settings4.json"
        path.write_text(json.dumps({"exempt_through": "0" * 40, "private_pattern_expected": False,
                                    "fork_way_open": False, "outside_runs_held_since": None}))
        args = [sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", self.base, "--head",
                self.repo.head(), "--settings", str(path), "--context", "own", "--compare-settings", self.base]
        done = subprocess.run(args, capture_output=True, text=True, env=clean_env(), timeout=120)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("the exempt commit named in settings.json is not in this history", done.stdout)

    def github_merge(self, branch, title):
        """A merge commit in the shape GitHub makes it on main: the merger's no-reply address and display name as
        author, GitHub as committer, the pull request's number, branch and title in the message."""
        self.repo.git("checkout", "-q", "-b", branch)
        self.repo.commit(title)
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Meanwhile on main")
        before = self.repo.head()
        env = {"GIT_AUTHOR_NAME": "Someone-Made-Up", "GIT_AUTHOR_EMAIL": OWN,
               "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": GITHUB}
        self.repo.git("merge", "-q", "--no-ff", "-m", f"Merge pull request #41 from SomeOrg/{branch}\n\n{title}",
                      branch, env=env)
        return before, self.repo.head()

    def test_main_s_run_on_a_merge_github_made_passes(self):
        before, merge = self.github_merge("commit-check", "Check every commit of a pull request")
        code, out = self.run_check(before, merge, verified=[merge], extra=["--require-commits"])
        self.assertEqual(code, 0, out)
        self.assertIn("2 commits checked", out)

    def test_main_s_run_on_a_merge_whose_branch_name_carries_a_tracker_code_fails(self):
        before, merge = self.github_merge(f"{TRACKER_CODE_LOWER}-notes", "Notes")
        self.assert_fails(self.run_check(before, merge, verified=[merge], extra=["--require-commits"]), merge,
                          "tracker code (message line 1)")

    def test_main_s_run_with_nothing_in_its_range_fails(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.repo.head(), extra=["--require-commits"])
        self.assertEqual(code, 1, out)
        self.assertIn("no commit", out)

    def test_the_name_of_the_branch_a_push_went_to_is_checked(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, extra=["--branch", f"{TRACKER_CODE_LOWER}-notes"])
        self.assertEqual(code, 1, out)
        self.assertIn("push: tracker code (branch name)", out)
        code, out = self.run_check(self.base, extra=["--branch", C.lower() + "/fix-the-notes"])
        self.assertIn("push: AI name (branch name)", out)

    # The pull request's title, description and branch name.

    def test_the_pull_request_s_title_description_and_branch_name_are_checked(self):
        self.repo.commit("A change")
        cases = [({"title": f"Fix ({TRACKER_CODE})", "body": ""}, "tracker code (pull request title)"),
                 ({"title": "Fix", "body": f"one\n{HOME_PATH}"}, "local path (pull request description line 2)"),
                 ({"title": "Fix", "body": GENERATED}, "attribution line (pull request description line 1)"),
                 ({"title": "Fix", "body": TRACKER_ADDRESS}, "tracker address (pull request description line 1)"),
                 ({"title": "Fix", "body": TASK_ID}, "tooling id (pull request description line 1)"),
                 ({"title": PRIVATE_WORD, "body": None}, "private pattern (pull request title)"),
                 ({"title": "Fix", "body": "", "head": {"ref": f"{TRACKER_CODE_LOWER}-notes"}},
                  "tracker code (pull request branch name)")]
        for pull, expected in cases:
            with self.subTest(expected=expected):
                code, out = self.run_check(self.base, pattern=PRIVATE_PATTERN,
                                           event={"pull_request": {"number": 7, **pull}})
                self.assertEqual(code, 1, out)
                self.assertIn(f"pull request: {expected}", out)
                self.assertNotIn(PRIVATE_WORD, out)

    def test_a_text_that_breaks_a_rule_fails_its_run_at_once(self):
        # No clock: nothing waits, nothing is read again. The way back is a new head.
        self.repo.commit("A change")
        code, out = self.run_check(self.base, event={"pull_request": {"number": 7, "title": f"Fix {TRACKER_CODE}",
                                                                      "body": ""}})
        self.assertEqual(code, 1, out)
        self.assertNotIn("waiting", out)

    def test_a_pull_request_with_no_commit_to_check_fails(self):
        code, out = self.run_check(self.base, self.base, event={"pull_request": {"number": 7, "title": "x", "body": ""}})
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

    def test_the_check_s_own_files_added_in_a_commit_do_not_trip_it(self):
        files = {f"x/{path.name}": path.read_text() for path in sorted(HERE.iterdir())
                 if path.suffix in (".py", ".json", ".md")}
        for name in ("pull-requests.yml", "commits.yml"):
            files[f"x/{name}"] = (HERE.parent / "workflows" / name).read_text()
        self.repo.commit("Add the check", files=files)
        self.assert_passes(self.run_check(self.base, pattern=PRIVATE_PATTERN))


class OutputTest(RepoTestCase):
    """What is printed is made by the check alone: never a text of its input."""

    def test_a_marker_in_every_field_of_the_input_is_never_printed(self):
        branch = f"{MARKER}-{TRACKER_CODE_LOWER}"
        self.repo.git("checkout", "-q", "-b", branch)
        sha = self.repo.commit(
            f"{MARKER} {TRACKER_CODE}\n\n{MARKER} {GENERATED}\n\nCo-" "authored-" f"by: {MARKER} <{PERSONAL}>",
            files={f"{MARKER}/{TRACKER_CODE}-{MARKER}.md": f"{MARKER} {TRACKER_CODE} {HOME_PATH} {MARKER}\n",
                   f"{MARKER}-{PRIVATE_WORD}.txt": f"{PRIVATE_WORD} {MARKER}\n+++ {MARKER} {TRACKER_CODE}\n"},
            author=(f"{MARKER} {C}", f"{MARKER}@example.org"), committer=(f"{MARKER}", f"{MARKER}@example.org"))
        event = {"pull_request": {"number": 7, "title": f"{MARKER} {TRACKER_CODE}",
                                  "body": f"{MARKER}\n{HOME_PATH}{MARKER}\n{GENERATED}", "head": {"ref": branch}}}
        code, out = self.run_check(self.base, sha, pattern=PRIVATE_PATTERN, event=event,
                                   extra=["--branch", branch])
        self.assertEqual(code, 1, out)
        self.assertGreater(out.count("\n"), 15, out)  # it did report, and at length
        for text in (MARKER, MARKER.lower(), PRIVATE_WORD, TRACKER_CODE, HOME_PATH, PERSONAL):
            self.assertNotIn(text, out)

    def test_a_marker_in_the_settings_or_in_an_argument_is_never_printed_and_nothing_is_traced_back(self):
        self.repo.commit("A change")
        odd_settings = self.dir / f"{MARKER}.json"
        odd_settings.write_text(json.dumps({"exempt_through": MARKER, "private_pattern_expected": False,
                                            "fork_way_open": False, "outside_runs_held_since": None}))
        calls = [["--settings", str(odd_settings), "--from", self.base, "--head", "HEAD", "--context", "own",
                  "--compare-settings", self.base],
                 ["--settings", str(self.dir / MARKER / "none.json"), "--from", self.base, "--head", "HEAD",
                  "--context", "own", "--compare-settings", self.base],
                 ["--settings", str(self.settings()), "--from", MARKER, "--head", "HEAD", "--context", "own",
                  "--compare-settings", self.base],
                 ["--settings", str(self.settings()), "--from", self.base, "--head", "HEAD", "--context", MARKER,
                  "--compare-settings", self.base],
                 ["--settings", str(self.settings()), "--from", self.base, "--head", "HEAD", "--context", "own",
                  "--compare-settings", MARKER],
                 ["--settings", str(self.settings()), "--from", self.base, "--head", "HEAD", "--context", "own",
                  "--compare-settings", self.base, f"--{MARKER}"],
                 ["--settings", str(self.settings()), "--from", self.base, "--head", "HEAD", "--context", "own",
                  "--compare-settings", self.base, "--event", str(self.dir / f"{MARKER}.event")]]
        (self.dir / f"{MARKER}.event").write_text(json.dumps({"pull_request": {"title": [MARKER]}}))
        for call in calls:
            with self.subTest(call=call):
                done = subprocess.run([sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), *call],
                                      capture_output=True, text=True, env=clean_env(), timeout=120)
                out = done.stdout + done.stderr
                self.assertNotEqual(done.returncode, 0, out)
                self.assertNotIn(MARKER, out)
                self.assertNotIn("Traceback", out)


class StructureTest(RepoTestCase):
    """The difference is read by its structure, and every line it adds is read as text."""

    def test_an_added_line_that_begins_like_a_header_of_the_format_is_still_checked(self):
        for start in ("+++ ", "++ ", "--- ", "-- ", "@@ -1 +1 @@ ", "@@@ -1 -1 +1 @@@ ", "diff --git a/x b/x ",
                      "index 0000000..1111111 ", "\\ No newline ", "new file mode 100644 ", "rename to "):
            with self.subTest(start=start):
                sha = self.repo.commit("A change", files={"a.txt": f"plain\n{start}{TRACKER_CODE}\n"})
                self.assert_fails(self.run_check(sha + "~1"), sha, "tracker code (file 1, line 2)")

    def test_a_file_an_attributes_file_keeps_from_comparison_is_read_all_the_same(self):
        sha = self.repo.commit("A change", files={".gitattributes": "* -diff\n*.txt binary\n",
                                                  "z.txt": f"one\n{TRACKER_CODE}\n"})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file 2, line 2)")

    def test_a_file_with_a_zero_byte_is_read_all_the_same(self):
        sha = self.repo.commit("A change", files={"cache.pyc": b"\x00\x0d\x0a" + TRACKER_CODE.encode() + b"\n"})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file 1, line 2)")

    def test_an_outside_diff_program_named_in_the_repository_s_settings_is_not_used(self):
        script = self.dir / "hide.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.repo.git("config", "diff.external", str(script))
        self.repo.git("config", "color.diff", "always")
        self.repo.git("config", "diff.x.textconv", str(script))
        sha = self.repo.commit("A change", files={".gitattributes": "*.txt diff=x\n", "z.txt": f"one\n{TRACKER_CODE}\n"})
        code, out = self.run_check(self.base)
        self.assert_fails((code, out), sha, "tracker code (file 2, line 2)")
        self.assertIn("the range: tracker code (file 2, line 2)", out)

    def test_every_rule_holds_inside_a_file_in_each_listed_encoding(self):
        samples = [(f"see {TRACKER_CODE} here", "tracker code"), (f"see {TRACKER_ADDRESS} here", "tracker address"),
                   (f"see {TASK_ID} here", "tooling id"), (f"see {HOME_PATH} here", "local path"),
                   (f"thanks to {PRIVATE_WORD} here", "private pattern"),
                   ("gener" "ated with " + C + " Code", "attribution line")]
        encodings = [("utf-16-le", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b""), ("utf-16-be", b"\xfe\xff"),
                     ("utf-32-le", b""), ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b""),
                     ("utf-32-be", b"\x00\x00\xfe\xff")]
        files, expected = {}, {}
        for rule_index, (text, prop) in enumerate(samples):
            for encoding_index, (codec, mark) in enumerate(encodings):
                for shift in range(4 if "32" in codec else 2):
                    name = f"f{rule_index}{encoding_index}{shift}.bin"
                    files[name] = b"\x89PNG\x00" + b"\x01" * shift + mark + text.encode(codec)
                    expected[name] = prop
            name = f"f{rule_index}z.bin"
            files[name] = b"\x00".join(text[i:i + 3].encode() for i in range(0, len(text), 3)) + b"\x00\xff"
            expected[name] = prop
        sha = self.repo.commit("Files in other encodings", files=files)
        code, out = self.run_check(self.base, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        for number, name in enumerate(sorted(files), 1):
            with self.subTest(name=name):
                self.assertRegex(out, rf"commit {sha[:12]}: {expected[name]} \(path {number}, read as ")
        self.assertNotIn(PRIVATE_WORD, out)

    def test_a_file_in_utf_16_that_is_also_valid_utf_8_is_read_in_utf_16_too(self):
        name = "\u0141ukasz"  # in UTF-16 its bytes are all below 128, so they are valid UTF-8 as well
        sha = self.repo.commit("A change", files={"z.bin": ("hello " + name + "\nagain " + name).encode("utf-16-le")})
        code, out = self.run_check(self.base, pattern="(?i)\u0142ukasz")
        self.assert_fails((code, out), sha, "private pattern (path 1, read as UTF-16LE from byte 0, line 1)")
        self.assertEqual(out.count(f"commit {sha[:12]}: private pattern (path 1, read as UTF-16LE from byte 0"), 1)

    def test_a_file_in_utf_16_with_no_zero_byte_is_read_in_utf_16(self):
        word = "\ubdc1\uafbb"  # two made-up syllables whose UTF-16 bytes hold no zero and are not UTF-8
        sha = self.repo.commit("A change", files={"z.bin": word.encode("utf-16-be")})
        self.assert_fails(self.run_check(self.base, pattern=word), sha, "private pattern (path 1, read as UTF-16BE")

    def test_the_order_of_files_does_not_follow_a_setting_of_the_calling_user(self):
        home = self.dir / "home"
        home.mkdir()
        (home / "order.txt").write_text("z.txt\n")
        (home / ".gitconfig").write_text(f"[diff]\n\torderFile = {home / 'order.txt'}\n")
        sha = self.repo.commit("A change", files={"a.txt": "plain\n", "z.txt": f"{TRACKER_CODE}\n"})
        code, out = self.run_check(self.base, env_extra={"HOME": str(home)})
        self.assert_fails((code, out), sha, "tracker code (file 2, line 1)")
        self.assertIn("the range: tracker code (file 2, line 1)", out)

    def test_a_merge_that_brings_back_a_file_in_another_encoding_from_the_exempt_history_fails(self):
        old = self.repo.commit("Old", files={"z.bin": f"see {TRACKER_CODE}".encode("utf-16-be")})
        self.repo.commit("Take it out", files={}, remove=["z.bin"])
        base = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "old-side", old)
        self.repo.git("checkout", "-q", "main")
        owner = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
                 "GIT_COMMITTER_EMAIL": OWN}
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "-s", "ours", "old-side", env=owner)
        self.repo.git("checkout", "old-side", "--", "z.bin")
        self.repo.git("commit", "-q", "-m", "Merge the old side", env=owner)
        path = self.dir / "settings6.json"
        path.write_text(json.dumps({"exempt_through": base, "private_pattern_expected": False, "fork_way_open": False,
                                    "outside_runs_held_since": None}))
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), "--from", base, "--head",
                               self.repo.head(), "--settings", str(path), "--context", "own", "--compare-settings", base,
                               "--verified-shas", os.devnull], capture_output=True, text=True, env=clean_env(), timeout=120)
        self.assertIn("the range: tracker code (path 1, read as UTF-16BE", done.stdout)

    def test_a_line_without_a_newline_at_the_end_is_checked(self):
        sha = self.repo.commit("A change", files={"a.txt": f"one\n{TRACKER_CODE}"})
        self.assert_fails(self.run_check(self.base), sha, "tracker code (file 1, line 2)")

    def test_lines_are_counted_by_the_hunks_lengths_across_several_hunks(self):
        self.repo.commit("A change", files={"a.txt": "".join(f"line {n}\n" for n in range(1, 21))})
        start = self.repo.head()
        text = "".join(f"line {n}\n" for n in range(1, 21)).replace("line 3\n", "line three\n")
        text = text.replace("line 15\n", f"{TRACKER_CODE}\nline 15\n")
        sha = self.repo.commit("Another change", files={"a.txt": text})
        self.assert_fails(self.run_check(start), sha, "tracker code (file 1, line 15)")


class SampleTest(RepoTestCase):
    """The inputs of the two looks at the first version, each with its result written out. One set of rules holds
    for every place; where a row passes, the reason is written beside it."""

    PROSE = [
        # (text, property or None)
        ("Co-Authored-By: " + C + " Opus 5.5 <noreply@anthrop" "ic.com>", "attribution line"),
        ("Co-Authored-By:" + C + " <noreply@anthrop" "ic.com>", "attribution line"),
        ("Co-authored-by : " + C + " <x@y.z>", "attribution line"),
        ("Co-authored by: " + C + " <noreply@anthrop" "ic.com>", "attribution line"),
        ("Co\u2013authored\u2013by: " + C + " <noreply@anthrop" "ic.com>", "attribution line"),
        ("Co-Authored-By\uff1a " + C + " <noreply@anthrop" "ic.com>", "attribution line"),
        ("Co-authored-by: Co" "pilot <175728472+Co" "pilot@users.noreply.github.com>", "attribution line"),
        ("Co-authored-by: some-ai-agent[bot] <158243242+some-ai-agent[bot]@users.noreply.github.com>", "attribution line"),
        ("Co-authored-by: " + C + "Bot <1+" + C.lower() + "bot@users.noreply.github.com>", "attribution line"),
        ("Co-" "authored-" "by: GPT-4o <" + OWN + ">", "attribution line"),
        ("Co-authored-by: " + C.lower() + "_code <" + OWN + ">", "attribution line"),
        ("Co-authored-by: " + C + "와 <" + OWN + ">", "attribution line"),
        ("Assisted-by: " + C + " <noreply@anthrop" "ic.com>", "attribution line"),
        ("Signed-off-by: A Person <" + OWN + ">", "attribution line"),
        ("Reviewed by: A Person", "attribution line"),
        ("Change-Id: I0123456789abcdef", "attribution line"),
        ("Model: some model", "attribution line"),
        ("Contact: a@b.example", "attribution line"),
        (GENERATED, "attribution line"),
        ("Gener" "ated with " + C + " Code", "attribution line"),
        ("Gener" "ated with GitHub Co" "pilot", "attribution line"),
        ("Gener" "ated with [**" + C + " Code**](https://x)", "attribution line"),
        ("Gener" "ated using " + C, "attribution line"),
        ("Gener" "ated-with: " + C + " Code", "attribution line"),
        ("Gener" "ated with the help of " + C, "attribution line"),
        ("Gener" "ated by an AI assistant", "attribution line"),
        ("Gener" "ated with Google Gem" "ini", "attribution line"),
        ("Gener" "ated with Google Anti" "gravity", "attribution line"),
        ("Gener" "ated by o3", "attribution line"),
        ("Gener" "ated with Wind" "surf", "attribution line"),
        (T + "-12", "tracker code"), (T.lower() + "-12", "tracker code"), ("Do" "d-12", "tracker code"),
        (T + " 12", "tracker code"), (T + "_12", "tracker code"), (T + "12", "tracker code"),
        ("[" + T + "-12]", "tracker code"), (T + "\u201112", "tracker code"), ("\uff24\uff2f\uff24-12", "tracker code"),
        ("fix_" + T.lower() + "-12", "tracker code"),
        ("https://lin" "ear.app/dodam/issue/x", "tracker address"), ("LIN" "EAR.APP/x", "tracker address"),
        ("/Us" "ers/someone/notes.md", "local path"),
        ("~/or" "ca/workspaces/dodam/review-x", "local path"),
        ("~/.cla" "ude/projects/-Us" "ers-someone-or" "ca-projects-dodam/memory/x.md", "local path"),
        ("~/.cla" "ude/plans/x.md", "local path"),
        ("~/.gem" "ini/antigravity-cli/brain/e255af7d/notes.md", "local path"),
        ("/priv" "ate/tmp/cla" "ude-501/x/scratchpad/a.txt", "local path"),
        ("/tm" "p/cla" "ude-501/x/a.txt", "local path"),
        ("$HOME/or" "ca/workspaces/dodam/x", "local path"),
        ("/va" "r/folders/ab/xyz123/T/tmp.txt", "local path"),
        ("C:\\Us" "ers\\someone\\x", "local path"),
        ("file:///Us" "ers/someone/x", "local path"),
        ("/mnt/c/Us" "ers/alice/dodam/README.md", "local path"),
        ("te" "rm_69feae12-f296-4431-8a08-fb5168b4978d", "tooling id"),
        ("ta" "sk_803e541ba577", "tooling id"), ("dispatch:c" "tx_0febe70c6bd9", "tooling id"),
        ("TA" "SK_803E541BA577", "tooling id"), ("w" "f_abc123def456", "tooling id"),
        ("my_ta" "sk_803e541ba577", "tooling id"),
        ("See conver" "sation://e255af7d-f4b3-47c3-be96-2e5d38a5f8c0", "tooling id"),
        # Passing rows, with their reasons.
        ("Case runner: the steps keep their order", None),  # a heading of two words is no trailer
        ("Declare it once, with the code generated by the compiler", None),  # not at the head of a line
        ("Keep the guard's file in ~/.dodam, one per user", None),  # a folder the project names
        ("058979d9-1b93-4869-a023-a6d75f6e35bf", None),  # a bare id: main's tests use made-up ids of this form
        (C.lower() + "/fix-the-thing", None),  # in prose; as a branch's name it is refused (below)
    ]
    LINES = [
        ("see " + T + "_17", "tracker code"),
        ("/Us" "ers/somebody/.gem" "ini/antigravity-cli/brain/abc/x.md", "local path"),
        ("/Us" "ers/somebody/work/dodam/notes.md", "local path"),
        (".cla" "ude/worktrees/feat-1/notes.md", "local path"),
        ("Gener" "ated with " + C + " Code", "attribution line"),
        ("/ho" "me/somebody/work/dodam/notes.md", None),  # main's tests name a container's home
        ("/tmp/x/y", None),  # main's isolation tests use the temporary folder
        (".cla" "ude/worktrees/", "local path"),  # an entry directly in a tool's own folder
        (".co" "dex/config.toml", "local path"), ("~/.or" "ca/logs/x.log", "local path"),
        ("~/.cla" "ude.json", "local path"), (".gem" "ini/oauth_creds", "local path"),
        ("~/.co" "dex/sessions/2026/11/27/rollout-1.jsonl", "local path"),
        ("Code written by " + C + " is reviewed by another company's model", None),  # the charter's words
    ]
    PATHS = [
        ("or" "ca/workspaces/dodam/notes.md", "local path"),
        ("private/tmp/cla" "ude-501/notes.md", "local path"),
        (".cla" "ude/settings.local.json", "local path"),
        (".cla" "ude/worktrees/x/a.txt", "local path"),
        (".gem" "ini/antigravity-cli/x.md", "local path"),
        ("docs/" + T + "-12.md", "tracker code"),
        ("docs/ta" "sk_803e541ba577.md", "tooling id"),
        ("docs/notes.md", None),
    ]
    BRANCHES = [
        (C.lower() + "/fix-the-thing", "AI name"),
        ("co" "dex/try-it", "AI name"),
        ("someone/" + T.lower() + "-12-fix-the-thing", "tracker code"),
        ("gem" "ini-on-orca", None),  # a branch about the project's own Gemini session, as on main
        ("commit-check", None),
        ("fix" + T + "-17", "tracker code"),  # split where a small letter meets a capital, as a person's name is
    ]

    def text_only(self, texts, pattern=None):
        event = self.dir / "text.json"
        event.write_text(json.dumps({"pull_request": {"number": 1, **texts}}))
        env = clean_env({"COMMIT_CHECK_PRIVATE_PATTERN": pattern} if pattern else None)
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--settings", str(self.settings()), "--context", "own",
                               "--text-only", "--event", str(event)], capture_output=True, text=True, env=env,
                              timeout=120, cwd=self.dir)
        return done.returncode, done.stdout + done.stderr

    def test_every_prose_row_in_a_description(self):
        for text, prop in self.PROSE:
            with self.subTest(text=text):
                code, out = self.text_only({"title": "Plain", "body": f"Some words.\n\n{text}"})
                if prop is None:
                    self.assertEqual(code, 0, out)
                else:
                    self.assertEqual(code, 1, out)
                    self.assertIn(f"pull request: {prop} (pull request description line 3)", out)

    def test_every_prose_row_in_a_commit_message(self):
        expected = {}
        for text, prop in self.PROSE:
            expected[self.repo.commit(f"A plain subject\n\n{text}")] = prop
        code, out = self.run_check(self.base)
        for sha, prop in expected.items():
            with self.subTest(prop=prop, sha=sha):
                if prop is None:
                    self.assertNotIn(f"commit {sha[:12]}:", out)
                else:
                    self.assertIn(f"commit {sha[:12]}: {prop} (message line 3)", out)

    def test_every_line_row_in_an_added_line_and_every_path_row_in_a_path(self):
        lines = {f"f{index:02}.txt": f"see {text}\n" for index, (text, _) in enumerate(self.LINES)}
        sha = self.repo.commit("Lines", files=lines)
        code, out = self.run_check(self.base)
        for index, (text, prop) in enumerate(self.LINES):
            with self.subTest(text=text):
                if prop is None:
                    self.assertNotIn(f"(file {index + 1}, line 1)", out)
                else:
                    self.assertIn(f"commit {sha[:12]}: {prop} (file {index + 1}, line 1)", out)
        for text, prop in self.PATHS:
            with self.subTest(path=text):
                sha = self.repo.commit("A path", files={text: ""})
                code, out = self.run_check(sha + "~1")
                if prop is None:
                    self.assertEqual(code, 0, out)
                else:
                    self.assertIn(f"commit {sha[:12]}: {prop} (path 1)", out)

    def test_every_branch_row(self):
        for name, prop in self.BRANCHES:
            with self.subTest(name=name):
                code, out = self.text_only({"title": "Plain", "body": "", "head": {"ref": name}})
                if prop is None:
                    self.assertEqual(code, 0, out)
                else:
                    self.assertIn(f"pull request: {prop} (pull request branch name)", out)

    def test_the_text_check_before_publishing_needs_no_repository_and_holds_the_private_pattern(self):
        code, out = self.text_only({"title": f"Thanks to {PRIVATE_WORD}", "body": ""}, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertIn("pull request: private pattern (pull request title)", out)
        self.assertNotIn(PRIVATE_WORD, out)
        self.assertEqual(self.text_only({"title": "A plain title", "body": "Plain words."})[0], 0)


class ArgumentsTest(RepoTestCase):
    """No default decides: the context and the comparison of the settings are always given, and given once."""

    def call(self, *args):
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--repo", str(self.repo.root), *args],
                              capture_output=True, text=True, env=clean_env(), timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_without_a_context_the_check_refuses_to_run(self):
        code, out = self.call("--settings", str(self.settings()), "--from", self.base, "--head", "HEAD",
                              "--compare-settings", self.base)
        self.assertEqual(code, 2, out)

    def test_a_context_that_no_workflow_has_is_refused(self):
        for context in ("fork", "fork-labeled", "", "OWN"):
            with self.subTest(context=context):
                code, out = self.call("--settings", str(self.settings()), "--from", self.base, "--head", "HEAD",
                                      "--context", context, "--compare-settings", self.base)
                self.assertEqual(code, 2, out)

    def test_without_the_comparison_of_the_settings_the_check_refuses_to_run(self):
        code, out = self.call("--settings", str(self.settings()), "--from", self.base, "--head", "HEAD",
                              "--context", "own")
        self.assertEqual(code, 1, out)
        self.assertIn("--compare-settings", out)

    def test_a_second_comparison_of_the_settings_is_refused(self):
        code, out = self.call("--settings", str(self.settings()), "--from", self.base, "--head", "HEAD",
                              "--context", "own", "--compare-settings", self.base, "--compare-settings", "HEAD")
        self.assertEqual(code, 2, out)

    def test_no_argument_makes_a_run_wait(self):
        for argument in ("--wait-for-text", "--poll"):
            with self.subTest(argument=argument):
                code, out = self.call("--settings", str(self.settings()), "--from", self.base, "--head", "HEAD",
                                      "--context", "own", "--compare-settings", self.base, argument, "5")
                self.assertEqual(code, 2, out)


class ForkWayTest(RepoTestCase):
    """The way for pull requests from forks is shut until settings.json opens it, after the owner has set the
    environment with a named reviewer, its own secret, and GitHub's setting that holds outside contributors' runs."""

    def fork_run(self, *, open_way, held="2026-11-27", environment=REVIEWED, message="A change", fork_pattern=None,
                 expected=False, pattern=None):
        sha = self.repo.commit(message)
        fake = self.fake({ENVIRONMENT_PATH: environment} if environment is not None else {})
        code, out = self.run_check(self.base, context="fork-approved", open_way=open_way, held=held,
                                   fork_pattern=fork_pattern, pattern=pattern, expected=expected,
                                   event={"pull_request": {"number": 7, "title": "Fix", "body": ""}},
                                   env_extra=fake.env)
        return sha, code, out, fake

    def test_while_the_way_is_shut_a_fork_s_run_is_told_so_judges_nothing_and_fails(self):
        sha, code, out, fake = self.fork_run(open_way=False, message=f"A change {TRACKER_CODE}")
        self.assertEqual(code, 1, out)
        self.assertIn("The way for pull requests from forks is not open yet", out)
        self.assertNotIn("commits checked", out)
        self.assertNotIn(sha[:12], out)
        self.assertEqual(fake.asked, [])

    def test_without_the_date_since_when_outside_runs_are_held_the_way_stays_shut(self):
        _, code, out, _ = self.fork_run(open_way=True, held=None)
        self.assertEqual(code, 1, out)
        self.assertIn("The way for pull requests from forks is not open yet", out)

    def test_an_open_way_with_an_environment_that_names_no_reviewer_judges_nothing(self):
        for environment in (UNREVIEWED, NOBODY, None):
            with self.subTest(environment=environment):
                sha, code, out, _ = self.fork_run(open_way=True, environment=environment,
                                                  message=f"A change {TRACKER_CODE}")
                self.assertEqual(code, 1, out)
                self.assertIn("does not wait for a named reviewer", out)
                self.assertNotIn(sha[:12], out)

    def test_an_open_way_judges_with_the_fork_s_own_secret_only(self):
        sha, code, out, _ = self.fork_run(open_way=True, message=f"Thanks to {FORK_WORD}",
                                          fork_pattern="(?i)quix" "otl", pattern="(?i)nothing-like-it")
        self.assertEqual(code, 1, out)
        self.assertIn(f"commit {sha[:12]}: private pattern (message line 1)", out)
        self.assertNotIn(FORK_WORD, out)
        sha, code, out, _ = self.fork_run(open_way=True, message=f"Thanks to {FORK_WORD}",
                                          fork_pattern=None, pattern="(?i)quix" "otl")
        self.assertEqual(code, 0, out)

    def test_an_open_way_fails_without_the_fork_s_secret_once_it_is_expected(self):
        _, code, out, _ = self.fork_run(open_way=True, expected=True, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertIn("private pattern", out)

    def main_push(self, *, open_way, held="2026-11-27", environment=REVIEWED, repository_secret="false"):
        self.repo.commit("A change")
        before = self.base
        fake = self.fake({ENVIRONMENT_PATH: environment} if environment is not None else {})
        return self.run_check(before, open_way=open_way, held=held, extra=["--require-commits", "--check-fork-way"],
                              env_extra={**fake.env, "FORK_SECRET_AT_REPOSITORY_LEVEL": repository_secret})

    def test_main_s_push_passes_while_the_way_is_shut(self):
        self.assert_passes(self.main_push(open_way=False, environment=None))

    def test_main_s_push_passes_when_the_way_is_open_and_everything_a_token_can_read_is_set(self):
        self.assert_passes(self.main_push(open_way=True))

    def test_main_s_push_turns_red_when_the_way_is_open_and_something_is_missing(self):
        cases = [dict(environment=None), dict(environment=UNREVIEWED), dict(environment=NOBODY),
                 dict(held=None)]
        for case in cases:
            with self.subTest(case=case):
                self.tearDown_repo()
                code, out = self.main_push(open_way=True, **case)
                self.assertEqual(code, 1, out)
                self.assertIn("the way for forks is open", out)

    def test_a_fork_s_secret_at_the_repository_s_level_turns_every_own_run_red_open_or_shut(self):
        for open_way in (False, True):
            with self.subTest(open_way=open_way):
                self.tearDown_repo()
                code, out = self.main_push(open_way=open_way, repository_secret="true")
                self.assertEqual(code, 1, out)
                self.assertIn("the fork's secret is a secret of the whole repository", out)

    def gate(self, *, open_way, held="2026-11-27", environment=REVIEWED):
        fake = self.fake({ENVIRONMENT_PATH: environment} if environment is not None else {})
        outputs = self.dir / "outputs.txt"
        outputs.write_text("")
        code, out = self.call_gate(open_way, held, {**fake.env, "GITHUB_OUTPUT": str(outputs)})
        return code, out, outputs.read_text(), fake

    def call_gate(self, open_way, held, env):
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--fork-way-gate", "--context", "fork-approved",
                               "--settings", str(self.settings(open_way=open_way, held=held))],
                              capture_output=True, text=True, env=clean_env(env), timeout=120, cwd=self.dir)
        return done.returncode, done.stdout + done.stderr

    def test_the_first_job_of_a_fork_s_run_fails_while_the_way_is_shut_and_asks_nothing(self):
        for open_way, held in ((False, "2026-11-27"), (True, None)):
            with self.subTest(open_way=open_way, held=held):
                code, out, outputs, fake = self.gate(open_way=open_way, held=held)
                self.assertEqual(code, 1, out)
                self.assertIn("The way for pull requests from forks is not open yet", out)
                self.assertEqual(outputs, "")
                self.assertEqual(fake.asked, [])

    def test_the_first_job_of_a_fork_s_run_fails_when_no_reviewer_is_named_or_it_cannot_tell(self):
        for environment in (UNREVIEWED, NOBODY, None):
            with self.subTest(environment=environment):
                code, out, outputs, _ = self.gate(open_way=True, environment=environment)
                self.assertEqual(code, 1, out)
                self.assertIn("does not wait for a named reviewer", out)
                self.assertEqual(outputs, "")

    def test_the_first_job_of_a_fork_s_run_says_open_only_when_the_way_is_open_and_a_reviewer_is_named(self):
        code, out, outputs, _ = self.gate(open_way=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(outputs, "open=true\n")
        self.assertIn("The way for pull requests from forks is open and the environment names a reviewer.", out)

    def test_the_first_job_s_question_is_refused_outside_a_fork_s_run(self):
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--fork-way-gate", "--context", "own",
                               "--settings", str(self.settings(open_way=True))],
                              capture_output=True, text=True, env=clean_env(), timeout=120, cwd=self.dir)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("--fork-way-gate is for a fork's run", done.stdout)

    def test_the_fork_s_secret_at_the_repository_s_level_is_asked_only_where_the_workflow_asks(self):
        self.repo.commit("A change")
        self.assert_passes(self.run_check(self.base, env_extra={"FORK_SECRET_AT_REPOSITORY_LEVEL": "true"}))

    def tearDown_repo(self):
        self.repo = Repo(Path(tempfile.mkdtemp(dir=self.dir)) / "repo")
        self.base = self.repo.commit("The base commit")


class MergeTest(RepoTestCase):
    """What a merge on main will carry is held to the rules before the merge: the displayed name of the account that
    merges, and the first line of the merge's message; and a red main can be left by a repair on top of it."""

    def merged_main(self, author=("Someone-Made-Up", OWN)):
        """A main whose last merge GitHub made, with the merging account's no-reply address as its author."""
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side work")
        self.repo.git("checkout", "-q", "main")
        env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": GITHUB}
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #1 from SomeOrg/side\n\nSide work", "side",
                      env=env)
        return self.repo.head()

    def preview(self, user_answer, *, actor=None, actor_answer=None, pattern=None):
        start = self.merged_main()
        self.repo.commit("A change")
        answers = {f"/users/{OWN_LOGIN}": user_answer}
        if actor:
            answers[f"/users/{actor}"] = actor_answer
        fake = self.fake(answers)
        self.last_fake = fake
        env = dict(fake.env)
        if actor:
            env["GITHUB_ACTOR"] = actor
        return self.run_check(start, pattern=pattern, extra=["--build-merge"], event=OWN_EVENT, env_extra=env)

    def test_a_name_that_equals_its_login_but_for_case_is_spared_the_private_pattern_whole_or_not_at_all(self):
        self.repo.commit("A change", author=("Someone-Made-Up", OWN), committer=(OWN_LOGIN.upper(), OWN))
        self.assert_passes(self.run_check(self.base, pattern="(?i)someone-made-" "up"))
        sha = self.repo.commit("Another", author=("Someone-Made-Up Junior", OWN), committer=(OWN_LOGIN, OWN))
        self.assert_fails(self.run_check(sha + "~1", pattern="(?i)someone-made-" "up"), sha, "author name")

    def test_the_displayed_name_github_will_write_on_the_merge_commit_is_read_ahead(self):
        for answer, expected in (({"login": OWN_LOGIN, "id": 1234567, "name": None}, 0), ({"login": OWN_LOGIN, "id": 1234567, "name": ""}, 0),
                                 ({"login": OWN_LOGIN, "id": 1234567, "name": "SOMEONE-made-up"}, 0),
                                 ({"login": OWN_LOGIN, "id": 1234567, "name": "Some Real Person"}, 1),
                                 ({"login": OWN_LOGIN, "name": None}, 1),  # no id: the address cannot be written
                                 ({"login": OWN_LOGIN, "id": True, "name": None}, 1),
                                 ({"login": OWN_LOGIN, "id": 1234567, "name": C}, 1), (404, 1)):
            with self.subTest(answer=answer):
                self.tearDown_repo()
                code, out = self.preview(answer)
                self.assertEqual(code, expected, out)
                if expected:
                    self.assertRegex(out, BUILT_AUTHOR_NAME + "|merge commit: author name")
                    self.assertNotIn("Some Real Person", out)

    def test_a_displayed_name_github_does_not_answer_fails_after_every_try(self):
        start = self.merged_main()
        self.repo.commit("A change")
        fake = self.fake({f"/users/{OWN_LOGIN}": 502})
        check = load("check.py")
        event = self.dir / "event-silent.json"
        event.write_text(json.dumps(OWN_EVENT))
        args = ["--repo", str(self.repo.root), "--from", start, "--head", "HEAD", "--settings", str(self.settings()),
                "--context", "own", "--compare-settings", start, "--verified-shas", os.devnull, "--build-merge",
                "--event", str(event)]
        code, out = in_process(check.main, args, fake.env, check)
        self.assertEqual(code, 1, out)
        self.assertIn(SILENT, out)
        self.assertEqual(fake.asked.count(f"/users/{OWN_LOGIN}"), 5)

    def test_the_displayed_name_is_held_to_the_private_pattern_too(self):
        code, out = self.preview({"login": OWN_LOGIN, "id": 1234567, "name": OWN_LOGIN.upper() + " " + PRIVATE_WORD},
                                 pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertNotIn(PRIVATE_WORD, out)

    def test_the_account_that_pushed_is_read_ahead_as_well_when_it_is_another(self):
        code, out = self.preview({"login": OWN_LOGIN, "id": 1234567, "name": None}, actor="another-made-up",
                                 actor_answer={"login": "another-made-up", "id": 7654321, "name": C + " Code"})
        self.assertEqual(code, 1, out)
        self.assertRegex(out, BUILT_AUTHOR_NAME)

    def test_on_a_fork_s_run_only_the_name_of_whoever_merges_is_read_ahead(self):
        # On a fork's run the account that pushed is the outside contributor, who never makes the merge commit.
        start = self.merged_main()
        self.repo.commit("A change")
        fake = self.fake({ENVIRONMENT_PATH: REVIEWED, f"/users/{OWN_LOGIN}": {"login": OWN_LOGIN, "id": 1234567, "name": None},
                          "/users/an-outside-person": {"login": "an-outside-person", "id": 1234567, "name": "Quite Another Name"}})
        code, out = self.run_check(start, context="fork-approved", open_way=True, extra=["--build-merge"],
                                   event={"pull_request": {"number": 7, "title": "Fix", "body": ""}},
                                   env_extra={**fake.env, "GITHUB_ACTOR": "an-outside-person"})
        self.assertEqual(code, 0, out)
        self.assertIn(f"/users/{OWN_LOGIN}", fake.asked)
        self.assertNotIn("/users/an-outside-person", fake.asked)

    def test_only_a_merge_github_made_with_a_no_reply_author_names_the_merging_account(self):
        self.merged_main()
        self.repo.git("checkout", "-q", "-b", "later")
        self.repo.commit("Later work")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge a local branch", "later",
                      env={"GIT_AUTHOR_NAME": "another-made-up", "GIT_AUTHOR_EMAIL": OLD_STYLE,
                           "GIT_COMMITTER_NAME": "another-made-up", "GIT_COMMITTER_EMAIL": OLD_STYLE})
        self.repo.git("checkout", "-q", "-b", "side2")
        self.repo.commit("Side two")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #3 from SomeOrg/side2", "side2",
                      env={"GIT_AUTHOR_NAME": "someone", "GIT_AUTHOR_EMAIL": PERSONAL, "GIT_COMMITTER_NAME": "GitHub",
                           "GIT_COMMITTER_EMAIL": GITHUB})
        start = self.repo.head()
        self.repo.commit("A change")
        fake = self.fake({f"/users/{OWN_LOGIN}": {"login": OWN_LOGIN, "id": 1234567, "name": None}})
        code, out = self.run_check(start, extra=["--build-merge"], event=OWN_EVENT, env_extra=fake.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.asked, [f"/users/{OWN_LOGIN}"])

    def test_the_account_that_pushed_is_not_asked_twice_when_it_is_the_merging_one(self):
        code, out = self.preview({"login": OWN_LOGIN, "id": 1234567, "name": None}, actor=OWN_LOGIN.upper(),
                                 actor_answer={"login": OWN_LOGIN, "id": 1234567, "name": None})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.last_fake.asked, [f"/users/{OWN_LOGIN}"])

    def test_a_displayed_name_that_is_not_text_cannot_be_read(self):
        code, out = self.preview({"login": OWN_LOGIN, "id": 1234567, "name": 5})
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: author name (the displayed name of the account that merges cannot be read)", out)

    def test_a_merging_account_named_like_a_tool_is_refused_even_by_its_own_login(self):
        start = self.merged_main(author=("Co" "pilot", "175728472+Co" "pilot@users.noreply.github.com"))
        self.repo.commit("A change")
        fake = self.fake({"/users/Co" "pilot": {"login": "Co" "pilot", "id": 175728472, "name": "Co" "pilot"}})
        code, out = self.run_check(start, extra=["--build-merge"], event=OWN_EVENT, env_extra=fake.env)
        self.assertEqual(code, 1, out)
        self.assertRegex(out, BUILT_AUTHOR_NAME)

    def test_without_a_merge_on_main_and_without_an_account_nothing_is_asked(self):
        self.repo.commit("A change")
        fake = self.fake({})
        code, out = self.run_check(self.base, extra=["--build-merge"], event=OWN_EVENT, env_extra=fake.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.asked, [])

    def test_the_first_line_of_the_merge_s_message_is_held_to_the_rules_for_a_fork(self):
        self.repo.commit("A change")
        fork = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                 "head": {"ref": "fix", "repo": {"owner": {"login": T.lower() + "-12"}}}},
                "repository": {"owner": {"login": "SomeOrg"}}}
        code, out = self.run_check(self.base, event=fork)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: tracker code (message line 1)", out)
        own = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                "head": {"ref": "fix", "repo": {"owner": {"login": "someorg"}}}},
               "repository": {"owner": {"login": "SomeOrg"}}}
        self.assert_passes(self.run_check(self.base, event=own))

    def test_a_red_merge_on_main_is_left_by_a_repair_on_top_of_it(self):
        # A merge commit with a personal address turns main's push red; a repair branch from it passes its own
        # push, and the hand merge of the repair passes main's next push, with the exempt commit where it was.
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side work")
        self.repo.git("checkout", "-q", "main")
        before = self.repo.head()
        env = {"GIT_AUTHOR_NAME": "someone", "GIT_AUTHOR_EMAIL": PERSONAL,
               "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": GITHUB}
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #2 from SomeOrg/side", "side", env=env)
        red = self.repo.head()
        self.assert_fails(self.run_check(before, red, verified=[red], extra=["--require-commits"]), red,
                          "author address")
        self.repo.git("checkout", "-q", "-b", "repair")
        repair = self.repo.commit("Say what happened at the red merge")
        self.assert_passes(self.run_check(red, repair))
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge the repair", "repair",
                      env={"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
                           "GIT_COMMITTER_EMAIL": OWN})
        self.assert_passes(self.run_check(red, self.repo.head(), extra=["--require-commits"]))

    def tearDown_repo(self):
        self.repo = Repo(Path(tempfile.mkdtemp(dir=self.dir)) / "repo")
        self.base = self.repo.commit("The base commit")


class ForkPathsCase(unittest.TestCase):
    """Runs the check of a fork's paths against a made-up GitHub."""

    def run_paths(self, files, *, pull=None, tasks=None, extra_answers=None, quick=False):
        pull = pull or {"number": 7, "changed_files": len(files), "draft": False, "base": {"ref": "main"}}
        answers = {"/repos/made-up/repo/pulls/7": pull,
                   "/repos/made-up/repo/pulls/7/files?per_page=100&page=1": files,
                   "/repos/made-up/repo/contents/bench/tasks?ref=" + "a" * 40: tasks or [],
                   "/repos/made-up/repo/contents/bench/tasks?ref=main": tasks or [],
                   "/repos/made-up/repo/contents/bench/tests?ref=main": [{"name": "test_judge.py", "type": "file"}]}
        answers.update(extra_answers or {})
        fake = FakeGitHub(answers)
        self.addCleanup(fake.close)
        if quick:
            # In this process, with no wait between the tries, for answers that fail on purpose.
            paths = load("fork_paths.py")
            return in_process(paths.main, ["--number", "7", "--base-sha", "a" * 40], fake.env, paths.check)
        done = subprocess.run([sys.executable, "-I", str(FORK_PATHS), "--number", "7", "--base-sha", "a" * 40],
                              capture_output=True, text=True, env=clean_env(fake.env), timeout=120)
        return done.returncode, done.stdout + done.stderr

    def entry(self, name, status="modified"):
        return {"filename": name, "status": status}


class ForkPathsTest(ForkPathsCase):
    """The job of main's copy that fails a fork's pull request at once when it touches a closed path. The closed
    paths are the desk's tool's; these tests only show that the job asks it and acts on its verdict."""

    def test_a_change_of_a_workflow_fails_at_once(self):
        code, out = self.run_paths([self.entry(".github/workflows/ci.yml")])
        self.assertEqual(code, 1, out)
        self.assertIn(".github/", out)

    def test_a_change_of_an_existing_test_fails(self):
        code, out = self.run_paths([self.entry("bench/tests/test_judge.py")])
        self.assertEqual(code, 1, out)

    def test_a_change_of_the_page_s_text_only_passes(self):
        code, out = self.run_paths([self.entry("tools/site/strings/en.json")])
        self.assertEqual(code, 0, out)

    def test_a_new_test_passes(self):
        code, out = self.run_paths([self.entry("bench/tests/test_" + "brand_new.py", "added")])
        self.assertEqual(code, 0, out)

    def test_a_list_of_paths_that_cannot_be_read_completely_fails(self):
        code, out = self.run_paths([self.entry("README.md")],
                                   pull={"number": 7, "changed_files": 5, "draft": False, "base": {"ref": "main"}})
        self.assertEqual(code, 1, out)
        code, out = self.run_paths([], extra_answers={"/repos/made-up/repo/pulls/7": 502}, quick=True)
        self.assertEqual(code, 1, out)

    def test_it_prints_no_path_of_the_pull_request(self):
        code, out = self.run_paths([self.entry(f".github/{MARKER}.yml"), self.entry(f"{MARKER}.md")])
        self.assertEqual(code, 1, out)
        self.assertNotIn(MARKER, out)


def in_process(main, argv, env, check_module):
    """Calls a script's main in this process with the given environment and no wait between the tries of GitHub's API;
    returns its exit code and what it printed."""
    import contextlib
    import io
    printed = io.StringIO()
    saved = {key: os.environ.get(key) for key in env}
    sleep = check_module.time.sleep
    os.environ.update(env)
    check_module.time.sleep = lambda seconds: None
    try:
        with contextlib.redirect_stdout(printed):
            try:
                code = main(argv)
            except SystemExit as stop:  # a parser that refuses the call: an outcome the test's assertion reads
                code = stop.code
    finally:
        check_module.time.sleep = sleep
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, printed.getvalue()


def raised(call):
    """What a call raised, or None: so that a test of a refusal fails on its assertion whatever is raised instead."""
    try:
        call()
    except Exception as error:  # noqa: BLE001
        return error
    return None


def load(name):
    """A module of the check, loaded in this process for tests that call it directly."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), HERE / name)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HERE))
    spec.loader.exec_module(module)
    return module


class RowsTest(RepoTestCase):
    """One row for every part of every rule, written out, each in a line of its own of one description."""

    def described(self, rows, pattern=None):
        body = "\n".join(text for text, _ in rows)
        event = self.dir / "rows.json"
        event.write_text(json.dumps({"pull_request": {"number": 1, "title": "Plain", "body": body}}))
        env = clean_env({"COMMIT_CHECK_PRIVATE_PATTERN": pattern} if pattern else None)
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--settings", str(self.settings()), "--context", "own",
                               "--text-only", "--event", str(event)], capture_output=True, text=True, env=env,
                              timeout=120, cwd=self.dir)
        out = done.stdout + done.stderr
        for number, (text, prop) in enumerate(rows, 1):
            with self.subTest(text=text):
                if prop is None:
                    self.assertNotIn(f"(pull request description line {number})", out)
                else:
                    self.assertIn(f"pull request: {prop} (pull request description line {number})", out)

    def test_every_tool_s_name_as_a_co_author(self):
        names = ["Cl" "aude", "Anthrop" "ic", "Open" "AI", "Chat" "GPT", "GPT-4", "Co" "dex", "Gem" "ini", "Ba" "rd",
                 "Co" "pilot", "Cur" "sor", "Dev" "in", "Anti" "gravity", "Ll" "ama", "Mis" "tral", "Deep" "Seek",
                 "Gr" "ok", "Wind" "surf", "Ai" "der", "Cl" "ine", "Qw" "en", "Tab" "nine", "Code" "ium",
                 "Code" "Whisperer", "Ju" "les", "Perple" "xity", "o" "1", "o" "3", "o4-mini", "helper-b" "ot",
                 "chat" "gpt", "open" "ai", "deep" "seek"]
        rows = [("Co-" "authored-" f"by: {name} <{OWN}>", "attribution line") for name in names]
        self.described(rows + [("Co-" "authored-" f"by: A Person <{OWN}>", None)])

    def test_every_word_of_attribution_as_a_key(self):
        words = ["Author", "Authors", "Committer", "Agent", "Model", "Models", "Tool", "Tools", "Assistant", "Assistants",
                 "Bot", "Bots", "AI", "LLM", "Generator", "Generated", "Assisted", "Credit", "Credits", "Via", "Coauthor",
                 "Coauthors"]
        self.described([(f"{word}: something", "attribution line") for word in words]
                       + [("Contact: a" "@" "b.example", "attribution line"), ("Note: something", None)])

    def test_every_way_of_saying_how_a_text_was_made(self):
        rows = [("Co-auth" "ored with " + C, "attribution line"), ("Coauth" "ored by " + C, "attribution line"),
                ("Assi" "sted by " + C, "attribution line"), ("Auth" "ored by " + C, "attribution line"),
                ("Writ" "ten with " + C, "attribution line"), ("Draf" "ted with " + C, "attribution line"),
                ("Produ" "ced using " + C, "attribution line"), ("Gener" "ated via " + C, "attribution line"),
                ("Gener" "ated through " + C, "attribution line"), ("Gener" "ated by " + C, "attribution line"),
                ("\N{ROBOT FACE} done", "attribution line"), ("Change id: I0123", "attribution line"),
                ("Co-" "authored-" "by: A Person", "attribution line"), ("Written for the owner's eyes", None),
                ("Ma" "de with " + C + " Code", "attribution line"), ("Crea" "ted with " + C + " Code", "attribution line"),
                ("Bu" "ilt with Co" "dex", "attribution line"), ("Prepa" "red by " + C, "attribution line"),
                ("Implemen" "ted by Co" "dex", "attribution line"), ("With help from " + C + " Code", "attribution line"),
                ("With the help of " + C, "attribution line"), ("AI-gener" "ated.", "attribution line"),
                ("AI-assi" "sted", "attribution line")]
        self.described(rows)

    def test_every_id_of_the_sessions_tooling(self):
        rows = [("te" "rm_0123abcd", "tooling id"), ("ta" "sk_0123abcd", "tooling id"), ("c" "tx_0123abcd", "tooling id"),
                ("dis" "patch_0123abcd", "tooling id"), ("ms" "g_0123abcd", "tooling id"), ("w" "f_0123abcd", "tooling id"),
                ("conver" "sation://x", "tooling id"), ("terms_and_conditions", None)]
        self.described(rows)

    def test_every_part_of_a_path_of_the_sessions_computer(self):
        rows = [("/Us" "ers/x/y", "local path"), (".cla" "ude/plans/x.md", "local path"), (".gem" "ini/x/y", "local path"),
                ("or" "ca/work" "spaces/x", "local path"), ("or" "ca/proj" "ects/x", "local path"),
                ("cla" "ude-501/x", "local path"), ("/va" "r/folders/ab/x", "local path"),
                ("-us" "ers-x-or" "ca-", "local path"), ("/Us" "ers/Shared/x", None)]
        self.described(rows)

    def test_every_form_of_a_dash_and_a_space_is_brought_to_one_form(self):
        rows = [(T + "\t12", "tracker code"), (T + "\u168012", "tracker code"), (T + "\u221212", "tracker code"),
                (T + "\u201312", "tracker code")]
        self.described(rows)

    def test_the_private_pattern_is_matched_on_the_text_as_written_too_and_in_either_case(self):
        # The copy makes the en dash a hyphen; the text as written keeps it. Letter case never matters.
        # The text as written: a pattern anchored at a line's start meets a line that the copy would not change.
        self.described([("Zorb" "latt at the start", "private pattern"), ("zorb" "latt at the start", "private pattern"),
                        ("Thanks to Zorb latt", None)], pattern="^Zorb" "latt")

    def test_a_tool_s_name_after_generated_with_in_an_added_line(self):
        sha = self.repo.commit("A change", files={"a.md": "co-auth" "ored by " + C + "\ngener" "ated with Gem" "ini\n"
                                                         "written by a person\n"})
        code, out = self.run_check(self.base)
        self.assert_fails((code, out), sha, "attribution line (file 1, line 1)")
        self.assert_fails((code, out), sha, "attribution line (file 1, line 2)")
        self.assertNotIn("(file 1, line 3)", out)


class ClassesTest(unittest.TestCase):
    """The copy for matching, walked over every character of its classes as the language's character tables give
    them: a forbidden text, the private pattern's included, with such a character inside it, or in place of its space,
    its dash or its line break, is refused in a message, in a name and in an added line."""

    TAKEN_OUT = ("Mn", "Me", "Cf", "Cc", "Cs", "Co", "Cn")  # marks, format, control, surrogate, private use, unassigned

    @classmethod
    def setUpClass(cls):
        import unicodedata
        cls.check = load("check.py")
        everything = [chr(point) for point in range(0x110000)]
        cls.breaks = [ch for ch in everything if len(("a" + ch + "b").splitlines()) > 1]
        breaks = set(cls.breaks)
        rest = [ch for ch in everything if ch not in breaks]
        cls.spaces = [ch for ch in rest if ch.isspace()]
        cls.taken_out = [ch for ch in rest if not ch.isspace() and (
            unicodedata.category(ch) in cls.TAKEN_OUT
            or (unicodedata.category(ch) == "Lo" and "FILLER" in unicodedata.name(ch, "").split()))]
        cls.dashes = [ch for ch in rest if unicodedata.category(ch) == "Pd" or (
            unicodedata.category(ch) == "Sm" and "MINUS" in unicodedata.name(ch, "").split()
            and "SET" not in unicodedata.name(ch, "").split())]

    def checker(self, pattern=None):
        import re as regex
        compiled = getattr(self.check, "compile_private", regex.compile)  # as the check compiles the secret
        return self.check.Checker(compiled(pattern) if pattern else None, lambda sha: False)

    def refused_everywhere(self, text, prop):
        message = self.checker(PRIVATE_PATTERN)
        message.prose("x", f"A plain subject\n\n{text}", lambda n: "w")
        line = self.checker(PRIVATE_PATTERN)
        line.added_line("x", 1, 1, text)
        return any(prop in found for found in message.findings) and any(prop in found for found in line.findings)

    def first_survivor(self, characters, make, prop):
        """The first character that lets the text through, for the message of a failed test."""
        for ch in characters:
            if not self.refused_everywhere(make(ch), prop):
                return f"U+{ord(ch):04X}"
        return None

    def test_every_character_taken_out_inside_a_forbidden_text_lets_nothing_through(self):
        # Each character on its own, next to a letter of the forbidden text, so that no character can hide another.
        # The assigned ones (marks, format, control, the fillers) go through every place; the unassigned, private and
        # surrogate points, which have no decomposition, through the copy every place matches on.
        import unicodedata
        assigned = [ch for ch in self.taken_out if unicodedata.category(ch) not in ("Cn", "Co", "Cs")]
        others = [ch for ch in self.taken_out if unicodedata.category(ch) in ("Cn", "Co", "Cs")]
        self.assertGreater(len(assigned), 2000)
        for pattern in (PRIVATE_PATTERN, "ZORB" "LATT|QUIX" "OTL"):  # the secret in small letters and in capitals
            message = self.checker(pattern)
            message.prose("x", "\n".join(f"DO{ch}D-17 Zorb{ch}latt lin{ch}ear.app" for ch in assigned),
                          lambda n: f"line {n}")
            for prop in ("tracker code", "private pattern", "tracker address"):
                caught = {found.rsplit("(", 1)[1] for found in message.findings if f": {prop} (" in found}
                missing = [ch for number, ch in enumerate(assigned, 1) if f"line {number})" not in caught]
                self.assertEqual([f"U+{ord(ch):04X}" for ch in missing[:5]], [], (pattern, prop))
        for ch in assigned:
            line = self.checker(PRIVATE_PATTERN)
            line.added_line("x", 1, 1, f"DO{ch}D-17 Zorb{ch}latt")
            if len(line.findings) != 2 or self.checker(PRIVATE_PATTERN).name_passes(f"Cl{ch}aude", OWN) \
                    or self.checker(PRIVATE_PATTERN).name_passes(f"Zorb{ch}latt", OWN):
                self.fail(f"U+{ord(ch):04X} lets a forbidden text through in a line or a name")
        # Every one of them between two letters, so that each stands next to a letter and none next to another.
        copy = self.check.normalise("O" + "O".join(others) + "O")
        if copy != "o" * (len(others) + 1):
            survivors = [ch for ch in others if self.check.normalise(f"DO{ch}D") != "dod"]
            self.fail(", ".join(f"U+{ord(ch):04X}" for ch in survivors[:5]))

    def test_plus_minus_signs_and_the_gap_fillers_of_other_scripts_stay_as_they_are(self):
        self.assertEqual(self.check.normalise("a\u00b1b\u2213c"), "a\u00b1b\u2213c")
        self.assertEqual(self.check.normalise("a\ua8f9b"), "a\ua8f9b")
        self.assertEqual(self.check.normalise("a\u2216b"), "a\u2216b")  # SET MINUS is no dash
        self.assertEqual(self.check.normalise("a\u02d7b"), "a\u02d7b")  # a modifier letter named MINUS is no sign
        self.assertEqual(self.check.normalise("\uac00\ub098\ub2e4 \u6f22\u5b57"), "\uac00\ub098\ub2e4 \u6f22\u5b57")

    def test_the_classes_are_not_empty(self):
        # Shown so that a change of the language's tables cannot empty a class and leave the walk with nothing.
        self.assertGreater(len(self.taken_out), 900000)
        self.assertGreater(len(self.spaces), 15)
        self.assertGreater(len(self.dashes), 25)
        self.assertGreater(len(self.breaks), 6)

    def test_every_space_and_every_dash_in_place_of_a_forbidden_text_s_own(self):
        for ch in self.spaces:
            with self.subTest(point=f"U+{ord(ch):04X}"):
                self.assertTrue(self.refused_everywhere(f"{T}{ch}17", "tracker code"))
        for ch in self.dashes:
            with self.subTest(point=f"U+{ord(ch):04X}"):
                self.assertTrue(self.refused_everywhere(f"{T}{ch}17", "tracker code"))
                self.assertFalse(self.checker().name_passes(f"GPT{ch}4", OWN))

    def test_every_line_break_ends_a_line_before_a_trailer(self):
        for ch in self.breaks:
            with self.subTest(point=f"U+{ord(ch):04X}"):
                message = self.checker()
                message.prose("x", f"Plain words.{ch}" + AI_COAUTHOR, lambda n: f"line {n}")
                self.assertTrue(any("attribution line" in found for found in message.findings), ch)
                line = self.checker()
                line.added_line("x", 1, 1, f"plain{ch}see {T}-17")
                self.assertTrue(any("tracker code" in found for found in line.findings), ch)

    def test_an_accent_is_folded_away_and_a_word_split_where_cases_meet(self):
        self.assertTrue(self.refused_everywhere("D\u00d3D-17", "tracker code"))
        self.assertFalse(self.checker().name_passes("GPT" "Bot", OWN))
        self.assertFalse(self.checker().name_passes("\uff23\uff4c\uff41\uff55\uff44\uff45\uff22\uff4f\uff54", OWN))
        self.assertTrue(self.checker().name_passes("\uac00\ub098\ub2e4", OWN))  # Korean syllables come back whole


class TextsTest(unittest.TestCase):
    """The README says what the code holds: the closed paths are the desk's, and every list is written out in it."""

    def setUp(self):
        self.readme = (HERE / "README.md").read_text(encoding="utf-8")
        self.check = load("check.py")

    def test_the_closed_paths_in_the_readme_are_the_desk_s(self):
        sys.path.insert(0, str(HERE.parents[1] / "tools" / "desk"))
        import pr_files
        self.assertIn("The paths closed to outside pull requests", self.readme)
        section = self.readme.split("The paths closed to outside pull requests", 1)[1].split("\n\n", 1)[0]
        named = [name for name in re.findall(r"`([^`]+/)`", section.split("Allowed in them", 1)[0])]
        self.assertEqual(sorted(named), sorted(pr_files.PROTECTED_FOLDERS))
        self.assertIn("the rest of `bench/`", section)
        self.assertEqual(pr_files.PAGE_TEXT, "tools/site/strings/")
        self.assertIn("`tools/site/strings/`", section)

    def test_every_word_of_every_list_stands_in_the_readme(self):
        text = self.readme.casefold()
        for word in self.check.ATTRIBUTION_WORDS:
            with self.subTest(word=word):
                self.assertRegex(text, r"(?<![a-z])" + re.escape(word) + r"(?![a-z])")
        for verb in ("generated", "co-authored", "coauthored", "assisted", "authored", "written", "drafted", "produced",
                     "made", "created", "built", "prepared", "implemented"):
            with self.subTest(verb=verb):
                self.assertIn(verb, text)
        for name in ("claude", "anthropic", "openai", "chatgpt", "gpt", "codex", "gemini", "bard", "copilot", "cursor",
                     "devin", "antigravity", "llama", "mistral", "deepseek", "grok", "windsurf", "aider", "cline", "qwen",
                     "tabnine", "codeium", "codewhisperer", "jules", "perplexity", "o1", "o3", "o4-mini"):
            with self.subTest(name=name):
                self.assertRegex(text, r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])")
        for phrase in ("with help of", "with the help of", "with help from", "with the help from", "ai-generated",
                       "ai-assisted"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertEqual(len(self.check.AI_NAMES), 28)
        self.assertEqual(len(self.check.ATTRIBUTION_WORDS), 22)
        self.assertEqual(len(self.check.MADE_VERBS), 12)
        self.assertEqual(len(self.check.LOCAL_PATHS), 11)
        self.assertEqual(len(self.check.TOOLING_IDS), 7)

    def test_the_readme_says_the_merge_commit_is_read_ahead_by_main_s_own_verdict(self):
        text = " ".join(self.readme.split())
        self.assertIn("judged by the same function that judges every commit on main's push", text)
        self.assertIn("the displayed name GitHub writes on a merge commit must be the login of its account, but for "
                      "letter case, or empty", text)
        self.assertIn("90 seconds in all", text)

    def test_no_number_of_main_s_history_stands_in_the_readme(self):
        self.assertNotRegex(self.readme, r"\d+ (?:added )?lines? of \d+ commits")


class ThirdLookTest(RepoTestCase):
    """Inputs that the third look found, each with the outcome the check must give."""

    def merge(self, message, side):
        env = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
               "GIT_COMMITTER_EMAIL": OWN}
        self.repo.git("merge", "-q", "--no-ff", "-m", message, side, env=env)
        return self.repo.head()

    def renamed_against_main(self, new_name):
        """A branch renames a file that main changes after the branch forked; main merges the branch, as GitHub does
        when a pull request is merged without taking main in first (the merge's first parent holds the old name)."""
        start = self.repo.commit("A file", files={"notes.txt": "one\ntwo\nthree\n"})
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.git("mv", "notes.txt", new_name)
        self.repo.commit("Rename the file", files={})
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("Change the file", files={"notes.txt": "one\ntwo\nfour\n"})
        merge = self.merge("Merge the side branch", "side")
        listed = self.repo.git("diff-tree", "-r", "-c", "--no-commit-id", "--name-status", "-z", "-M", merge)
        self.assertEqual(listed.split("\0")[0][:1], "R")  # the combined list's renamed entry, with one path
        return start, merge

    def test_a_merge_whose_combined_list_holds_a_renamed_path_is_read(self):
        start, _ = self.renamed_against_main("kept.txt")
        code, out = self.run_check(start)
        self.assertEqual(code, 0, out)
        self.assertIn("3 commits checked", out)

    def test_the_renamed_path_of_a_merge_s_combined_list_is_held_to_the_rules(self):
        start, merge = self.renamed_against_main(f"{TRACKER_CODE}.txt")
        self.assert_fails(self.run_check(start), merge, "tracker code (path 1)")

    def test_the_private_pattern_holds_whatever_the_case_the_secret_is_written_in(self):
        for text in (PRIVATE_WORD.upper(), "Zorb\u200blatt", "\uff3aorblatt", "Zo\u0301rblatt"):
            with self.subTest(point=[f"U+{ord(ch):04X}" for ch in text if ord(ch) > 127]):
                sha = self.repo.commit(f"Thanks to {text}", files={"a.txt": f"{text}\n"})
                code, out = self.run_check(self.repo.git("rev-parse", "HEAD^"), pattern="Zorb" "latt")
                self.assert_fails((code, out), sha, "private pattern (message line 1)", hidden=(PRIVATE_WORD,))
                self.assertIn(f"commit {sha[:12]}: private pattern (file 1, line 1)", out)

    def test_a_secret_the_copy_can_never_meet_stops_the_run_without_showing_it(self):
        self.repo.commit("A change")
        for secret in ("(?i)jos\u00e9", "jose\u0301", "stra\u00dfe", "zorb\u200blatt", "zorb\tlatt", "zorb\u2013latt",
                       "zorb\u2238latt", "zorb\u3164latt", "zorb\\x41", "zorb\\u00e9", "zorb\\N{LATIN SMALL LETTER A}",
                       "zorb\\101", "zorb\\0", "zorb\\tlatt", "zorb\uff08a\uff09", "\uff3aorblatt", "zorb\u2122+",
                       "zorb\\\u00b9", "zorb  latt", "zorb\u00a0latt", "(?a)zorb" "latt", "(?-i:Z)orb" "latt", "\u037e",
                       "\uf900", "zorb\u2126", "zorb\u212a", "zorb\u03f4", "zorb\u0131"):
            with self.subTest(secret=secret.encode("unicode_escape").decode()):
                code, out = self.run_check(self.base, pattern=secret)
                self.assertEqual(code, 1, out)
                self.assertIn("::error::the private pattern given in the secret is written in a way the copy can never "
                              "meet", out)
                self.assertNotIn(secret, out)

    def test_a_secret_in_capitals_reaches_as_one_in_small_letters(self):
        for number, secret in enumerate(("ZORB" "LATT", "Zorb latt|zorb" "latt", "(?i)zorb\\s?latt",
                                         "[zZ]orb(l)\\1?att", "(?x) z o r b latt")):
            with self.subTest(secret=secret.encode("unicode_escape").decode()):
                sha = self.repo.commit("Thanks to Zorb\u200blatt", files={f"a{number}.txt": "ZORB\u0301LATT\n"})
                code, out = self.run_check(sha + "~1", pattern=secret)
                self.assert_fails((code, out), sha, "private pattern (message line 1)", hidden=(PRIVATE_WORD,))
                self.assertIn(f"commit {sha[:12]}: private pattern (file 1, line 1)", out)

    def test_a_secret_in_a_script_without_cases_or_accents_is_taken(self):
        word = "\ub3c4\ub2f4"  # two Korean syllables, which the copy keeps whole
        sha = self.repo.commit(f"About {word}")
        self.assert_fails(self.run_check(self.base, pattern=word), sha, "private pattern (message line 1)")

    def test_a_path_that_begins_with_the_home_folders_of_macos_is_a_home_folder(self):
        sha = self.repo.commit("A change", files={"Us" "ers/somebody/notes.md": "plain\n"})
        self.assert_fails(self.run_check(self.base), sha, "local path (path 1)")
        sha = self.repo.commit("Us" "ers/somebody/notes.md is where it was")
        self.assert_fails(self.run_check(self.base), sha, "local path (message line 1)")

    def test_a_co_author_line_with_no_space_after_its_colon_in_an_added_line_is_refused(self):
        sha = self.repo.commit("A change", files={"a.txt": "Co-" "Authored-" "By:" + C + " <x@example.org>\n"})
        self.assert_fails(self.run_check(self.base), sha, "attribution line (file 1, line 1)")

    def test_a_key_of_several_words_ending_in_by_is_refused_in_a_middle_paragraph(self):
        sha = self.repo.commit("A change\n\nSigned-" "off-" "by: A Person\nand more words\n\nThe end.")
        self.assert_fails(self.run_check(self.base), sha, "attribution line (message line 3)")

    def test_a_word_in_utf_16_or_utf_32_after_an_odd_number_of_bytes_is_read(self):
        word = "\ub3c4\ub2f4"
        for label, data in (("UTF-16LE from byte 1", b"\x89" + word.encode("utf-16-le")),
                            ("UTF-32BE from byte 3", b"\x89PN" + word.encode("utf-32-be"))):
            with self.subTest(label=label):
                sha = self.repo.commit("A change", files={"z.bin": data})
                code, out = self.run_check(self.repo.git("rev-parse", "HEAD^"), pattern=word)
                self.assert_fails((code, out), sha, f"private pattern (path 1, read as {label}")

    def test_the_last_line_of_a_file_without_a_final_line_break_is_read_as_changed(self):
        self.repo.commit("A file", files={"a.txt": "one\ntwo"})
        start = self.repo.head()
        sha = self.repo.commit("A change", files={"a.txt": "one\nthree"})
        self.assert_passes(self.run_check(start))
        sha = self.repo.commit("A change", files={"a.txt": f"one\n{TRACKER_CODE}"})
        self.assert_fails(self.run_check(start), sha, "tracker code (file 1, line 2)")


class MergeKindTest(RepoTestCase):
    """A commit with more than one parent, as a kind: real small merges for every status git prints in a combined
    list. In each, every listed path is read and held to the rules on the merge commit itself, and nothing stops the
    reading."""

    def setUp(self):
        super().setUp()
        self.n = 0

    def merge(self, first, second, resolve=None, message="Merge"):
        """Merges branch `second` into `first` (first parent), resolving with `resolve` when given."""
        self.repo.git("checkout", "-q", first)
        env = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
               "GIT_COMMITTER_EMAIL": OWN}
        # git needs a name and an address to merge, also without committing; a conflict (1) is resolved below.
        done = subprocess.run(["git", "merge", "-q", "--no-ff", "--no-commit", second], cwd=self.repo.root,
                              capture_output=True, env={**clean_env(), "GIT_CONFIG_GLOBAL": os.devnull,
                                                        "GIT_CONFIG_NOSYSTEM": "1", **env})
        self.assertIn(done.returncode, (0, 1), done.stderr)
        self.assertTrue((self.repo.root / ".git" / "MERGE_HEAD").exists(), "git began no merge")
        if resolve:
            resolve()
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.repo.head()

    def write(self, name, content):
        path = self.repo.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)

    def side(self, name, start, work):
        self.repo.git("checkout", "-q", "-b", name, start)
        work()
        self.repo.git("add", "-A")
        self.repo.commit(f"Work on {name}", files={})

    def statuses(self, merge):
        listed = self.repo.git("diff-tree", "-r", "-c", "--no-commit-id", "--name-status", "-z", "-M", merge)
        fields = [field for field in listed.split("\0") if field]
        return fields[0::2]

    def judged(self, start, merge, expect_status):
        """Every path of the merge's combined list is refused on the merge itself, and nothing stops the reading."""
        statuses = self.statuses(merge)
        self.assertTrue(any(status.startswith(expect_status[:1]) or expect_status in status for status in statuses),
                        statuses)
        code, out = self.run_check(start, merge)
        self.assertEqual(code, 1, out)
        self.assertNotIn("cannot be read", out)
        for number in range(1, len(statuses) + 1):
            self.assertIn(f"commit {merge[:12]}: tracker code (path {number})", out)
        return out

    def base_with(self, files):
        start = self.repo.commit("Files", files=files)
        self.repo.git("branch", "-q", "trunk", start)
        return start

    def test_changed_on_both_sides(self):
        name = f"{TRACKER_CODE}-both.txt"
        start = self.base_with({name: "1\n2\n3\n4\n5\n"})
        self.side("left", start, lambda: self.write(name, "one\n2\n3\n4\n5\n"))
        self.side("right", start, lambda: self.write(name, "1\n2\n3\n4\nfive\n"))
        self.judged(start, self.merge("left", "right"), "MM")

    def renamed_and_changed(self, first_renames):
        old, new = "old.txt", f"{TRACKER_CODE}-new.txt"
        start = self.base_with({old: "1\n2\n3\n4\n5\n6\n7\n8\n"})
        self.side("renames", start, lambda: self.repo.git("mv", old, new))
        self.side("changes", start, lambda: self.write(old, "1\n2\n3\n4\n5\n6\n7\neight\n"))
        first, second = ("renames", "changes") if first_renames else ("changes", "renames")
        return start, self.merge(first, second)

    def test_renamed_on_one_side_and_changed_on_the_other_with_the_change_first(self):
        start, merge = self.renamed_and_changed(first_renames=False)
        self.judged(start, merge, "RM")

    def test_renamed_on_one_side_and_changed_on_the_other_with_the_rename_first(self):
        start, merge = self.renamed_and_changed(first_renames=True)
        self.judged(start, merge, "MR")

    def test_renamed_on_both_sides(self):
        old, new = "old.txt", f"{TRACKER_CODE}-new.txt"
        start = self.base_with({old: "1\n2\n3\n4\n5\n6\n7\n8\n"})

        def left():
            self.repo.git("mv", old, new)
            self.write(new, "one\n2\n3\n4\n5\n6\n7\n8\n")

        def right():
            self.repo.git("mv", old, new)
            self.write(new, "1\n2\n3\n4\n5\n6\n7\neight\n")
        self.side("left", start, left)
        self.side("right", start, right)
        self.judged(start, self.merge("left", "right"), "MM")

    def test_renamed_in_the_merge_itself_beside_files_added_there(self):
        # The merge renames a file both parents hold and adds two files: git lists the rename against both parents
        # and the additions against both, one path each; a later commit takes the renamed file away again.
        start = self.base_with({"orig.txt": "1\n2\n3\n4\n5\n6\n7\n8\n"})
        self.side("left", start, lambda: self.write("a.txt", "a\n"))
        self.side("right", start, lambda: self.write("b.txt", "b\n"))

        def evil():
            self.repo.git("mv", "orig.txt", f"A-{PRIVATE_WORD}-{TRACKER_CODE}.txt")
            self.write(f"R-{TRACKER_CODE}.md", "plain\n")  # a path that begins as a status of a rename would
            self.write(f"z-{TRACKER_CODE}.txt", "plain\n")
        merge = self.merge("left", "right", resolve=evil)
        self.assertEqual(self.statuses(merge)[0], "RR")
        self.repo.git("rm", "-q", f"A-{PRIVATE_WORD}-{TRACKER_CODE}.txt")
        self.repo.commit("Take it away", files={})
        code, out = self.run_check(start, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertNotIn("cannot be read", out)
        self.assertIn(f"commit {merge[:12]}: private pattern (path 1)", out)
        for number in (1, 2, 3):
            self.assertIn(f"commit {merge[:12]}: tracker code (path {number})", out)
        self.assertNotIn(PRIVATE_WORD, out)

    def test_added_on_both_sides(self):
        name = f"{TRACKER_CODE}-added.txt"
        start = self.base_with({"keep.txt": "k\n"})
        self.side("left", start, lambda: self.write(name, "left\n"))
        self.side("right", start, lambda: self.write(name, "right\n"))
        self.judged(start, self.merge("left", "right", resolve=lambda: self.write(name, "left\nright\n")), "MM")

    def test_added_in_the_merge_itself(self):
        start = self.base_with({"keep.txt": "k\n"})
        self.side("left", start, lambda: self.write("a.txt", "a\n"))
        self.side("right", start, lambda: self.write("b.txt", "b\n"))
        merge = self.merge("left", "right", resolve=lambda: self.write(f"{TRACKER_CODE}-new.txt", "new\n"))
        self.assertEqual(self.statuses(merge), ["AA"])
        self.judged(start, merge, "AA")

    def test_a_change_of_type_in_the_merge(self):
        name = f"{TRACKER_CODE}-link"
        start = self.base_with({name: "target\n"})
        self.side("left", start, lambda: self.write("a.txt", "a\n"))
        self.side("right", start, lambda: self.write("b.txt", "b\n"))

        def to_link():
            (self.repo.root / name).unlink()
            os.symlink("somewhere-else", self.repo.root / name)
        self.judged(start, self.merge("left", "right", resolve=to_link), "TT")

    def test_a_file_that_is_not_text_changed_in_the_merge(self):
        name = f"{TRACKER_CODE}.bin"
        start = self.base_with({name: b"\x89PNG\x00\x01"})
        self.side("left", start, lambda: self.write("a.txt", "a\n"))
        self.side("right", start, lambda: self.write("b.txt", "b\n"))
        word = "\ub3c4\ub2f4"
        merge = self.merge("left", "right", resolve=lambda: self.write(name, b"\x89PNG\x00" + word.encode("utf-16-le")))
        out = self.judged(start, merge, "MM")
        code, out = self.run_check(start, merge, pattern=word)
        self.assertIn(f"commit {merge[:12]}: private pattern (path 1, read as UTF-16LE from byte", out)

    def test_added_on_one_side_and_changed_in_the_merge(self):
        name = f"{TRACKER_CODE}-later.txt"
        start = self.base_with({"keep.txt": "k\n"})
        self.side("adds", start, lambda: self.write(name, "1\n2\n"))
        self.side("other", start, lambda: self.write("b.txt", "b\n"))
        merge = self.merge("other", "adds", resolve=lambda: self.write(name, "1\n2\nthree\n"))
        self.assertEqual(self.statuses(merge), ["AM"])
        self.judged(start, merge, "AM")

    def test_deleted_in_the_merge_where_the_parents_differed_and_a_path_after_it(self):
        start = self.base_with({"a-gone.txt": "1\n2\n3\n4\n5\n"})
        self.side("left", start, lambda: self.write("a-gone.txt", "one\n2\n3\n4\n5\n"))
        self.side("right", start, lambda: self.write("a-gone.txt", "1\n2\n3\n4\nfive\n"))

        def resolve():
            self.repo.git("rm", "-q", "-f", "a-gone.txt")
            self.write(f"b-{TRACKER_CODE}.txt", "new\n")
        merge = self.merge("left", "right", resolve=resolve)
        self.assertEqual(self.statuses(merge), ["DD", "AA"])
        code, out = self.run_check(start, merge)
        self.assertNotIn("cannot be read", out)
        self.assert_fails((code, out), merge, "tracker code (path 2)")
        self.assertNotIn(f"commit {merge[:12]}: tracker code (path 1)", out)

    def test_a_merge_of_three_parents(self):
        start = self.base_with({"keep.txt": "k\n"})
        for name in ("one", "two", "three"):
            self.side(name, start, lambda name=name: self.write(f"{name}.txt", f"{name}\n"))
        self.repo.git("checkout", "-q", "one")
        env = {"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": OWN_LOGIN,
               "GIT_COMMITTER_EMAIL": OWN}
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "two", "three", env=env)
        self.write(f"{TRACKER_CODE}-octopus.txt", f"{TRACKER_CODE}\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Merge three", env=env)
        merge = self.repo.head()
        self.assertEqual(len(self.repo.git("rev-list", "--parents", "-n", "1", merge).split()), 4)
        out = self.judged(start, merge, "AAA")
        self.assertIn(f"commit {merge[:12]}: tracker code (file 1, line 1)", out)

    def test_a_line_and_a_path_a_merge_adds_stay_refused_when_a_later_commit_takes_the_file_away(self):
        start = self.base_with({"keep.txt": "k\n"})
        self.side("left", start, lambda: self.write("a.txt", "a\n"))
        self.side("right", start, lambda: self.write("b.txt", "b\n"))
        name = f"{TRACKER_CODE}-brief.txt"
        merge = self.merge("left", "right", resolve=lambda: self.write(name, f"{PRIVATE_WORD}\n"))
        self.repo.git("rm", "-q", name)
        self.repo.commit("Take it away", files={})
        code, out = self.run_check(start, pattern=PRIVATE_PATTERN)
        self.assert_fails((code, out), merge, "tracker code (path 1)", hidden=(PRIVATE_WORD,))
        self.assertIn(f"commit {merge[:12]}: private pattern (file 1, line 1)", out)

    def test_lines_a_merge_adds_are_numbered_by_the_files_of_its_own_difference(self):
        # One side changes the first line of a file, the other its last line; the merge takes both, which git leaves
        # out of the merge's own difference, and adds a line of its own to another file.
        start = self.base_with({"a.txt": "L1\nL2\nL3\nL4\n", "z.txt": "Z1\n"})
        self.side("left", start, lambda: self.write("a.txt", "L1-from-side\nL2\nL3\nL4\n"))
        self.side("right", start, lambda: self.write("a.txt", "L1\nL2\nL3\nL4-from-main\n"))

        def resolve():
            self.write("a.txt", "L1-from-side\nL2\nL3\nL4-from-main\n")
            self.write("z.txt", f"{TRACKER_CODE}\n")
        merge = self.merge("right", "left", resolve=resolve)
        self.assert_fails(self.run_check(start, merge), merge, "tracker code (file 1, line 1)")


class SameVerdictTest(RepoTestCase):
    """The pull request's run builds the merge GitHub will make and judges it with the function main's push uses: the
    same merge gets one verdict both ways."""

    def findings(self, out):
        return sorted(line.split(": ", 1)[1] for line in out.splitlines()
                      if line.startswith(("commit ", "the range: ", "merge commit: ")))

    def both_ways(self, displayed, owner, branch, title, pattern):
        # Main's history holds a merge GitHub made, which names the account that merges.
        self.repo.git("checkout", "-q", "-b", "earlier")
        self.repo.commit("Earlier work")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #1 from SomeOrg/earlier\n\nEarlier", "earlier",
                      env={"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": "GitHub",
                           "GIT_COMMITTER_EMAIL": GITHUB})
        tip = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "side")
        head = self.repo.commit("Side work")
        event = {"pull_request": {"number": 7, "title": title, "body": "",
                                  "head": {"ref": branch, "repo": {"owner": {"login": owner}}}},
                 "repository": {"owner": {"login": owner}}}
        fake = self.fake({f"/users/{OWN_LOGIN}": {"login": OWN_LOGIN, "id": 1234567, "name": displayed}})
        ahead = self.run_check(tip, head, event=event, pattern=pattern, extra=["--build-merge"], env_extra=fake.env,
                               verified=[])
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", f"Merge pull request #7 from {owner}/{branch}\n\n{title}", "side",
                      env={"GIT_AUTHOR_NAME": displayed or OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN,
                           "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": GITHUB})
        merge = self.repo.head()
        on_main = self.run_check(tip, merge, pattern=pattern, verified=[merge], extra=["--require-commits"])
        return ahead, on_main

    def test_one_verdict_on_the_same_merge(self):
        cases = [  # (displayed name, owner, branch, title, pattern, refused)
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "som" "eone-made-up", False),  # the account
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "SOMEONE-mad" "e-up", False),  # its name
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "some" "org", True),  # the repository's owner
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "fix-no" "tes", True),  # the branch
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "the no" "tes", True),  # the title
            ("SOMEONE-made-up", "SomeOrg", "fix-notes", "Fix the notes", "zorb" "latt", False),  # nothing
            (None, "SomeOrg", "fix-notes", "Fix the notes", None, False),
            ("Some Real Person", "SomeOrg", "fix-notes", "Fix the notes", None, True),
            (C + " Code", "SomeOrg", "fix-notes", "Fix the notes", None, True),
            ("SOMEONE-made-up", "SomeOrg", T.lower() + "-17-notes", "Fix the notes", None, True),
            ("SOMEONE-made-up", "SomeOrg", "Us" "ers/someone/notes", "Fix the notes", None, True),
        ]
        for displayed, owner, branch, title, pattern, refused in cases:
            with self.subTest(displayed=displayed, branch=branch, pattern=pattern):
                self.tearDown_repo()
                (code_ahead, out_ahead), (code_main, out_main) = self.both_ways(displayed, owner, branch, title, pattern)
                self.assertEqual(self.findings(out_ahead), self.findings(out_main), out_ahead + out_main)
                self.assertEqual(code_main, 1 if refused else 0, out_main)
                self.assertEqual(code_ahead, code_main, out_ahead)

    def test_main_s_push_is_never_told_a_signature_it_does_not_have(self):
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side work")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #7 from SomeOrg/side\n\nSide", "side",
                      env={"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": "GitHub",
                           "GIT_COMMITTER_EMAIL": GITHUB})
        merge = self.repo.head()
        self.assert_fails(self.run_check(self.base, merge, verified=[], extra=["--require-commits"]), merge,
                          "committer address")

    def test_a_pull_request_that_does_not_merge_cleanly_with_main_s_tip_fails_with_a_fixed_message(self):
        self.repo.commit("A file", files={"a.txt": "one\n"})
        tip_before = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "side")
        head = self.repo.commit("Side", files={"a.txt": "two\n"})
        self.repo.git("checkout", "-q", "main")
        tip = self.repo.commit("Main", files={"a.txt": "three\n"})
        code, out = self.run_check(tip, head, event=OWN_EVENT, extra=["--build-merge"], compare=tip_before)
        self.assertEqual(code, 1, out)
        self.assertIn("::error::the pull request does not merge cleanly with main's tip", out)

    def tearDown_repo(self):
        self.repo = Repo(Path(tempfile.mkdtemp(dir=self.dir)) / "repo")
        self.base = self.repo.commit("The base commit")


class SurvivorsTest(RepoTestCase):
    """Inputs for the breaks the machine left standing at fc4337b, each with the outcome the check must give."""

    def text_only(self, event, pattern=None):
        path = self.dir / "text.json"
        path.write_text(json.dumps(event))
        env = clean_env({"COMMIT_CHECK_PRIVATE_PATTERN": pattern} if pattern else None)
        done = subprocess.run([sys.executable, "-I", str(CHECK), "--settings", str(self.settings()), "--context", "own",
                               "--text-only", "--event", str(path)], capture_output=True, text=True, env=env,
                              timeout=120, cwd=self.dir)
        return done.returncode, done.stdout + done.stderr

    def test_a_case_change_inside_a_word_splits_only_names_and_branches(self):
        # For the rules of every place and for the private pattern alike.
        word = "Zorb" "Latt"
        self.repo.commit("Fix the fix" + T + "-17 case and " + word, files={"a.txt": "fix" + T + "-17 " + word + "\n"})
        self.assert_passes(self.run_check(self.base, pattern="zorb latt"))
        for branch, pattern, prop in (("fix" + T + "-17", None, "tracker code"),
                                      ("Zorb" "Latt", "zorb latt", "private pattern")):
            with self.subTest(branch=branch):
                code, out = self.text_only({"pull_request": {"number": 1, "title": "Plain", "body": "",
                                                             "head": {"ref": branch}}}, pattern=pattern)
                self.assertEqual(code, 1, out)
                self.assertIn(f"pull request: {prop} (pull request branch name)", out)

    def test_a_merge_not_made_by_github_keeps_a_plain_name(self):
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.commit("Side work")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge the side branch", "side",
                      env={"GIT_AUTHOR_NAME": "A Person", "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": "A Person",
                           "GIT_COMMITTER_EMAIL": OWN})
        self.assert_passes(self.run_check(self.base))

    def test_github_s_api_without_a_named_repository_is_no_answer_and_nothing_is_asked(self):
        check = load("check.py")
        asked = []
        saved = os.environ.pop("GITHUB_REPOSITORY", None), check.urllib.request.urlopen
        check.urllib.request.urlopen = lambda request, timeout: asked.append(request) or (_ for _ in ()).throw(OSError())
        try:
            outcome = raised(lambda: check.api_get("commits/x"))
        finally:
            check.urllib.request.urlopen = saved[1]
            if saved[0] is not None:
                os.environ["GITHUB_REPOSITORY"] = saved[0]
        self.assertIsInstance(outcome, check.Problem)
        self.assertEqual(str(outcome), "GitHub's API did not answer")
        self.assertEqual(asked, [])

    def test_an_id_that_is_no_number_cannot_be_read(self):
        start = self.github_merge()
        self.repo.commit("A change")
        fake = self.fake({f"/users/{OWN_LOGIN}": {"login": OWN_LOGIN, "id": True, "name": None}})
        code, out = self.run_check(start, event=OWN_EVENT, extra=["--build-merge"], env_extra=fake.env)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: author name (the displayed name of the account that merges cannot be read)", out)
        self.assertNotIn("author address", out)

    def test_the_owner_of_the_base_stands_in_the_merge_message_when_the_head_names_none(self):
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "", "head": {"ref": "fix"}},
                 "repository": {"owner": {"login": PRIVATE_WORD + "-Org"}}}
        code, out = self.text_only(event, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: private pattern (message line 1)", out)
        self.assertNotIn(PRIVATE_WORD, out)

    def test_the_merge_message_holds_the_pull_request_s_number(self):
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "", "head": {"ref": "fix"}}}
        code, out = self.text_only(event, pattern="request #7 from")
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: private pattern (message line 1)", out)
        event["pull_request"]["number"] = "7"  # not a number: GitHub's own number is unknown
        self.assertEqual(self.text_only(event, pattern="request #7 from")[0], 0)

    def test_the_text_check_holds_the_merge_message_to_the_rules(self):
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                  "head": {"ref": "fix", "repo": {"owner": {"login": T.lower() + "-12"}}}}}
        code, out = self.text_only(event)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: tracker code (message line 1)", out)

    def test_with_nobody_known_who_merges_the_merge_is_still_built_and_judged(self):
        self.repo.commit("A change")
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                  "head": {"ref": "fix", "repo": {"owner": {"login": T.lower() + "-12"}}}}}
        fake = self.fake({})
        code, out = self.run_check(self.base, event=event, extra=["--build-merge"], env_extra=fake.env)
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"commit [0-9a-f]{12}: tracker code \(message line 1\)")
        self.assertEqual(fake.asked, [])

    def test_a_displayed_name_git_cannot_write_is_refused(self):
        start = self.github_merge()
        self.repo.commit("A change")
        fake = self.fake({f"/users/{OWN_LOGIN}": {"login": OWN_LOGIN, "id": 1234567, "name": "<>"}})
        code, out = self.run_check(start, event=OWN_EVENT, extra=["--build-merge"], env_extra=fake.env)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: author name (the displayed name of the account that merges cannot be written)", out)

    def github_merge(self):
        self.repo.git("checkout", "-q", "-b", "earlier")
        self.repo.commit("Earlier work")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request #1 from SomeOrg/earlier\n\nEarlier", "earlier",
                      env={"GIT_AUTHOR_NAME": OWN_LOGIN, "GIT_AUTHOR_EMAIL": OWN, "GIT_COMMITTER_NAME": "GitHub",
                           "GIT_COMMITTER_EMAIL": GITHUB})
        return self.repo.head()

    def test_building_the_merge_needs_the_event(self):
        self.repo.commit("A change")
        code, out = self.run_check(self.base, extra=["--build-merge"], verified=[])
        self.assertEqual(code, 1, out)
        self.assertIn("::error::--build-merge needs --event", out)

    def test_only_the_merge_the_run_built_is_told_a_signature(self):
        sha = self.repo.commit("Made on GitHub", author=(OWN_LOGIN, OWN), committer=("GitHub", GITHUB))
        code, out = self.run_check(self.base, event=OWN_EVENT, extra=["--build-merge"], verified=[])
        self.assert_fails((code, out), sha, "committer address")
        self.assertEqual(out.count("committer address"), 1)


class BudgetTest(unittest.TestCase):
    """Every try at GitHub's API in one run shares one budget of 90 seconds on a clock that only runs forward, each try
    the lesser of 10 seconds and what is left; when it runs out the check stops with its own message."""

    def test_a_github_that_does_not_answer_ends_the_run_inside_its_budget_with_a_fixed_message(self):
        check = load("check.py")
        self.assertTrue(hasattr(check, "API_BUDGET"), "one budget for every try at GitHub's API")

        class Clock:  # the stand-in clock: a try takes all the time it is given, a wait takes its seconds
            now = 1000.0

            @staticmethod
            def monotonic():
                return Clock.now

            @staticmethod
            def sleep(seconds):
                Clock.now += seconds

        timeouts = []

        def silent(request, timeout):
            timeouts.append(timeout)
            Clock.now += timeout
            raise TimeoutError()

        saved = (check.time, check.urllib.request.urlopen, {key: os.environ.get(key) for key in ("GITHUB_REPOSITORY",)})
        check.time, check.urllib.request.urlopen = Clock, silent
        os.environ["GITHUB_REPOSITORY"] = "made-up/repo"
        outcomes = []
        try:
            for path in ("commits/a", "commits/b", "commits/c", "users/x"):
                try:
                    outcomes.append(check.api_get(path))
                except check.Problem as problem:
                    outcomes.append(str(problem))
        finally:
            check.time, check.urllib.request.urlopen = saved[0], saved[1]
            for key, value in saved[2].items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(outcomes, ["GitHub's API did not answer"] * 4)
        self.assertLessEqual(Clock.now - 1000.0, 90.0)
        self.assertTrue(all(timeout <= 10 for timeout in timeouts), timeouts)
        self.assertEqual(timeouts[:5], [10, 10, 10, 10, 10])  # the first thing asked: five tries, waits 2, 5, 10, 20
        self.assertEqual(sum(timeouts), 90.0 - (2 + 5 + 10 + 20))  # then the budget runs out inside the next try

    def test_the_budget_and_the_try(self):
        check = load("check.py")
        self.assertEqual((getattr(check, "API_BUDGET", None), getattr(check, "API_TRY", None)), (90.0, 10.0))



class EdgeTest(RepoTestCase):
    """What the check refuses when its input is not as git, GitHub or a workflow gives it."""

    def call(self, *args, env=None):
        done = subprocess.run([sys.executable, "-I", str(CHECK), *args], capture_output=True, text=True,
                              env=clean_env(env), timeout=120, cwd=self.dir)
        return done.returncode, done.stdout + done.stderr

    def basic(self, *more):
        return ["--repo", str(self.repo.root), "--settings", str(self.settings()), "--context", "own",
                "--from", self.base, "--head", "HEAD", "--compare-settings", self.base, *more]

    def crafted(self, header_lines):
        """A commit object written by hand, which git itself would not make."""
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        text = "\n".join([f"tree {tree}", f"parent {self.base}", *header_lines, "", "A message", ""])
        path = self.dir / "object.txt"
        path.write_text(text)
        return self.repo.git("hash-object", "-t", "commit", "-w", "--literally", str(path))

    def test_a_commit_without_an_author_or_with_two_or_with_one_that_cannot_be_read_is_refused(self):
        stamp = "1700000000 +0000"
        cases = [([f"committer {OWN_LOGIN} <{OWN}> {stamp}"], "a commit has no author or no committer"),
                 ([f"author {OWN_LOGIN} {OWN} {stamp}", f"committer {OWN_LOGIN} <{OWN}> {stamp}"],
                  "the author line of a commit cannot be read"),
                 ([f"author {OWN_LOGIN} <{OWN}> {stamp}", f"author {OWN_LOGIN} <{OWN}> {stamp}",
                   f"committer {OWN_LOGIN} <{OWN}> {stamp}"], "the author line of a commit cannot be read")]
        for lines, message in cases:
            with self.subTest(message=message):
                sha = self.crafted(lines)
                code, out = self.call("--repo", str(self.repo.root), "--settings", str(self.settings()), "--context",
                                      "own", "--from", self.base, "--head", sha, "--compare-settings", self.base)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def test_a_difference_that_does_not_keep_its_own_lengths_is_refused(self):
        check = load("check.py")
        for patch in ("diff --git a/x b/x\n@@ -1 -1 +1 @@\n+a\n", "diff --git a/x b/x\n@@ -0,0 +1,2 @@\n+a\n",
                      "diff --git a/x b/x\n@@ -0,0 +1,2 @@\n+a",
                      "diff --git a/x b/x\n@@ -0,0 +1,1 @@\nXa\n", "diff --cc x\n@@@ -1,1 -1,1 +1,1 @@@\n*+a\n"):
            with self.subTest(patch=patch):
                self.assertIsInstance(raised(lambda: check.Checker(None, lambda sha: False).patch("x", patch)),
                                      check.Problem)

    def test_a_list_of_paths_that_does_not_keep_its_shape_is_refused(self):
        check = load("check.py")
        for fields in (["R100", "a"], ["M"], ["A"]):
            with self.subTest(fields=fields):
                self.assertIsInstance(raised(lambda: check.check_paths(check.Checker(None, lambda sha: False), "x",
                                                                         fields)), check.Problem)

    def test_a_raw_list_that_does_not_keep_its_shape_is_refused(self):
        check = load("check.py")
        for fields in (["M", "x"], [":100644 100644 aaa M"], [":100644 100644 " + "a" * 40 + " " + "b" * 40 + " M"],
                       ["100644 100644 " + "a" * 40 + " " + "b" * 40 + " M", "x"], [":100644 100644 aaa M", "x"]):
            with self.subTest(fields=fields):
                self.assertIsInstance(raised(lambda: check.new_blobs(fields)), check.Problem)

    def test_contents_git_cannot_give_are_refused(self):
        check = load("check.py")
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        for repo, shas in ((str(self.dir / "nowhere"), ["a" * 40]), (str(self.repo.root), ["0" * 40]),
                           (str(self.repo.root), [tree]), (str(self.repo.root), ["x blob 0 extra"])):
            with self.subTest(shas=shas):
                self.assertIsInstance(raised(lambda: check.blob_contents(repo, shas)), check.Problem)

    def test_a_deleted_file_in_another_encoding_asks_for_no_content(self):
        self.repo.commit("Add", files={"z.bin": "D\x00O\x00D\x00".encode() + b"\xff"})
        start = self.repo.head()
        self.repo.commit("Remove", files={}, remove=["z.bin"])
        self.assert_passes(self.run_check(start))

    def test_a_renamed_file_does_not_bring_its_old_lines_in_again(self):
        self.repo.commit("Old", files={"notes.md": f"{TRACKER_CODE}\n" + "".join(f"line {n}\n" for n in range(20))})
        exempt = self.repo.head()
        self.repo.git("mv", "notes.md", "kept.md")
        self.repo.commit("Rename", files={})
        path = self.dir / "settings5.json"
        path.write_text(json.dumps({"exempt_through": exempt, "private_pattern_expected": False, "fork_way_open": False,
                                    "outside_runs_held_since": None}))
        code, out = self.call("--repo", str(self.repo.root), "--settings", str(path), "--context", "own", "--from", exempt,
                              "--head", "HEAD", "--compare-settings", exempt, "--verified-shas", os.devnull)
        self.assertEqual(code, 0, out)

    def test_the_job_s_outputs_that_cannot_be_written_are_refused(self):
        fake = self.fake({ENVIRONMENT_PATH: REVIEWED})
        settings = self.dir / "open.json"
        settings.write_text(json.dumps({"exempt_through": self.base, "private_pattern_expected": False,
                                        "fork_way_open": True, "outside_runs_held_since": "2026-11-27"}))
        code, out = self.call("--fork-way-gate", "--context", "fork-approved", "--settings", str(settings),
                              env={**fake.env, "GITHUB_OUTPUT": str(self.dir)})
        self.assertEqual(code, 1, out)
        self.assertIn("the job's outputs cannot be written", out)

    def test_this_repository_s_own_owner_is_held_in_the_merge_message_as_main_holds_it(self):
        # Main's push holds the whole merge message, the owner's name in it too; the pull request's run reads it ahead
        # the same way, so that the pull request fails first.
        self.repo.commit("A change")
        owner = T.lower() + "-12org"
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                  "head": {"ref": "fix", "repo": {"owner": {"login": owner}}}},
                 "repository": {"owner": {"login": owner}}}
        path = self.dir / "event-owner.json"
        path.write_text(json.dumps(event))
        code, out = self.call(*self.basic("--event", str(path), "--verified-shas", os.devnull))
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: tracker code (message line 1)", out)

    def test_a_private_pattern_that_matches_this_repository_s_owner_fails_the_pull_request_first(self):
        self.repo.commit("A change")
        owner = PRIVATE_WORD + "-Org"
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                  "head": {"ref": "fix", "repo": {"owner": {"login": owner}}}},
                 "repository": {"owner": {"login": owner}}}
        code, out = self.run_check(self.base, event=event, pattern=PRIVATE_PATTERN)
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: private pattern (message line 1)", out)
        self.assertNotIn(PRIVATE_WORD, out)
        # And main's push of the merge GitHub would make refuses the same line.
        merge = self.repo.commit(f"Merge pull request #7 from {owner}/fix\n\nFix")
        self.assert_fails(self.run_check(self.base, pattern=PRIVATE_PATTERN), merge, "private pattern (message line 1)",
                          hidden=(PRIVATE_WORD,))

    def test_a_branch_whose_name_turns_into_a_path_in_the_merge_message_is_refused(self):
        self.repo.commit("A change")
        event = {"pull_request": {"number": 7, "title": "Fix", "body": "",
                                  "head": {"ref": "Us" "ers/someone/notes", "repo": {"owner": {"login": "SomeOrg"}}}},
                 "repository": {"owner": {"login": "SomeOrg"}}}
        path = self.dir / "event-own.json"
        path.write_text(json.dumps(event))
        code, out = self.call(*self.basic("--event", str(path), "--verified-shas", os.devnull))
        self.assertEqual(code, 1, out)
        self.assertIn("merge commit: local path (message line 1)", out)

    def test_git_s_variables_of_the_calling_process_do_not_steer_the_check(self):
        self.repo.commit("A change")
        code, out = self.call(*self.basic("--verified-shas", os.devnull),
                              env={"GIT_DIR": str(self.dir / "elsewhere"), "GIT_WORK_TREE": str(self.dir),
                                   "GIT_OBJECT_DIRECTORY": str(self.dir / "nowhere")})
        self.assertEqual(code, 0, out)
        self.assertIn("1 commits checked", out)

    def test_settings_of_every_wrong_kind_are_refused_by_name(self):
        good = {"exempt_through": self.base, "private_pattern_expected": False, "fork_way_open": False,
                "outside_runs_held_since": None}
        path = self.dir / "listed.json"
        path.write_text(json.dumps(sorted(good)))
        code, out = self.call("--repo", str(self.repo.root), "--settings", str(path), "--context", "own",
                              "--from", self.base, "--head", "HEAD", "--compare-settings", self.base)
        self.assertEqual(code, 1, out)
        self.assertIn("::error::settings.json does not hold", out)
        for change in ({"exempt_through": 123}, {"fork_way_open": "no"}, {"outside_runs_held_since": 5},
                       {"outside_runs_held_since": "yesterday"}, {"private_pattern_expected": None}):
            with self.subTest(change=change):
                path = self.dir / "wrong.json"
                path.write_text(json.dumps({**good, **change}))
                code, out = self.call("--repo", str(self.repo.root), "--settings", str(path), "--context", "own",
                                      "--from", self.base, "--head", "HEAD", "--compare-settings", self.base)
                self.assertEqual(code, 1, out)
                self.assertIn("::error::settings.json does not hold", out)

    def test_settings_on_the_range_s_start_that_cannot_be_read_are_refused(self):
        for text in ("not json", "[]"):
            with self.subTest(text=text):
                path = self.repo.root / ".github" / "commit-check" / "settings.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                self.repo.git("add", "-f", "--", str(path))
                start = self.repo.commit("Odd settings", files={})
                self.repo.commit("A change")
                code, out = self.call("--repo", str(self.repo.root), "--settings", str(self.settings()), "--context",
                                      "own", "--from", start, "--head", "HEAD", "--compare-settings", start,
                                      "--verified-shas", os.devnull)
                self.assertEqual(code, 1, out)
                self.assertIn("settings: the settings on the range's start cannot be read", out)

    def test_settings_that_stay_as_they_were_pass_whichever_they_are(self):
        for expected in (False, True):
            with self.subTest(expected=expected):
                path = self.repo.root / ".github" / "commit-check" / "settings.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                value = {"exempt_through": self.base, "private_pattern_expected": expected, "fork_way_open": False,
                         "outside_runs_held_since": None}
                path.write_text(json.dumps(value))
                self.repo.git("add", "-f", "--", str(path))
                start = self.repo.commit("Settings", files={})
                self.repo.commit("A change")
                local = self.dir / "same.json"
                local.write_text(json.dumps(value))
                code, out = self.call("--repo", str(self.repo.root), "--settings", str(local), "--context", "own",
                                      "--from", start, "--head", "HEAD", "--compare-settings", start,
                                      "--verified-shas", os.devnull,
                                      env={"COMMIT_CHECK_PRIVATE_PATTERN": PRIVATE_PATTERN})
                self.assertEqual(code, 0, out)

    def test_an_event_that_cannot_be_read_is_refused_by_name(self):
        self.repo.commit("A change")
        cases = [(None, "the event cannot be read"), ("not json", "the event cannot be read"),
                 (json.dumps({"issue": {}}), "the event names no pull request"),
                 (json.dumps({"pull_request": {"title": "x", "head": {"ref": 5}}}), "the event's pull request cannot be read")]
        for text, message in cases:
            with self.subTest(message=message):
                path = self.dir / "event-case.json"
                if path.exists():
                    path.unlink()
                if text is not None:
                    path.write_text(text)
                code, out = self.call(*self.basic("--event", str(path), "--verified-shas", os.devnull))
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def test_a_pull_request_without_a_description_or_without_a_title_passes(self):
        self.repo.commit("A change")
        for pull in ({"title": "Fix the notes", "body": None}, {"title": None, "body": "Plain words."}):
            with self.subTest(pull=pull):
                path = self.dir / "event-none.json"
                path.write_text(json.dumps({"pull_request": {"number": 7, **pull, "head": {"ref": "notes"}}}))
                code, out = self.call(*self.basic("--event", str(path), "--verified-shas", os.devnull))
                self.assertEqual(code, 0, out)

    def test_the_count_of_problems_is_printed(self):
        self.repo.commit(f"One ({TRACKER_CODE})")
        self.repo.commit("Two", author=("someone", PERSONAL), committer=("someone", PERSONAL))
        code, out = self.call(*self.basic("--verified-shas", os.devnull))
        self.assertEqual(code, 1, out)
        self.assertIn("\n3 problems\n", out)

    def test_a_wrong_call_prints_a_fixed_message(self):
        code, out = self.call("--settings", str(self.settings()), "--context", "fork")
        self.assertEqual(code, 2, out)
        self.assertIn("::error::the check was not called as its workflows call it", out)

    def test_something_the_check_does_not_know_stops_it_with_a_fixed_message(self):
        check = load("check.py")

        def odd(args):
            raise RuntimeError("made up " + MARKER)

        import contextlib
        import io
        printed = io.StringIO()
        real = check.run
        check.run = odd
        try:
            with contextlib.redirect_stdout(printed):
                code = check.main(["--settings", "x", "--context", "own"])
        finally:
            check.run = real
        self.assertEqual(code, 1)
        self.assertIn("::error::the check stopped on something it does not know", printed.getvalue())
        self.assertNotIn(MARKER, printed.getvalue())

    def test_a_list_of_signed_commits_that_cannot_be_read_is_refused(self):
        code, out = self.call(*self.basic("--verified-shas", str(self.dir)))
        self.assertEqual(code, 1, out)
        self.assertIn("the list of signed commits cannot be read", out)

    def test_the_text_check_needs_an_event(self):
        code, out = self.call("--settings", str(self.settings()), "--context", "own", "--text-only")
        self.assertEqual(code, 1, out)
        self.assertIn("--text-only needs --event", out)

    def test_a_check_of_commits_needs_its_start_and_its_head(self):
        for drop in ("--from", "--head"):
            with self.subTest(drop=drop):
                args = self.basic()
                index = args.index(drop)
                del args[index:index + 2]
                code, out = self.call(*args)
                self.assertEqual(code, 1, out)
                self.assertIn("the check needs --from, --head and --compare-settings", out)

    def test_the_way_for_forks_is_asked_about_only_on_main_s_push(self):
        self.repo.commit("A change")
        fake = self.fake({})
        code, out = self.run_check(self.base, open_way=True, held=None, env_extra=fake.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.asked, [])

    def test_a_push_of_a_branch_with_no_new_commit_passes(self):
        code, out = self.run_check(self.base, self.base)
        self.assertEqual(code, 0, out)
        self.assertIn("0 commits checked", out)


class ApiTest(unittest.TestCase):
    """How the check asks GitHub: with the token when there is one, again after a failed answer, not again after an
    answer that says no."""

    def ask(self, answers, token="made-up"):
        check = load("check.py")
        fake = FakeGitHub({})
        self.addCleanup(fake.close)
        sequence = list(answers)
        headers = []

        class Handler(fake.server.RequestHandlerClass):
            def do_GET(self):
                headers.append(self.headers.get("Authorization"))
                answer = sequence.pop(0) if len(sequence) > 1 else sequence[0]
                if isinstance(answer, tuple):  # an error with headers
                    self.send_response(answer[0])
                    for key, value in answer[1].items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if isinstance(answer, int):
                    self.send_error(answer)
                    return
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        fake.server.RequestHandlerClass = Handler
        saved = {key: os.environ.get(key) for key in fake.env}
        os.environ.update({**fake.env, "GITHUB_TOKEN": token})
        sleep = check.time.sleep
        self.waits = []
        check.time.sleep = self.waits.append
        self.check = check
        try:
            try:
                return check.api_get("x"), headers
            except check.Problem as problem:
                return str(problem), headers
        finally:
            check.time.sleep = sleep
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_a_failed_answer_is_asked_five_times_in_all_with_waits_of_two_five_ten_and_twenty_seconds(self):
        answer, asked = self.ask([502])
        self.assertEqual(answer, "GitHub's API did not answer")
        self.assertEqual(len(asked), 5)
        self.assertEqual(self.waits, [2, 5, 10, 20])
        answer, asked = self.ask([500, 503, {"a": 1}])
        self.assertEqual(answer, {"a": 1})
        self.assertEqual(len(asked), 3)
        self.assertEqual(self.waits, [2, 5])
        answer, asked = self.ask([{"a": 1}])
        self.assertEqual(self.waits, [])

    def test_the_wait_between_tries_is_put_back_after_the_test(self):
        import time as clock
        before = clock.sleep
        self.ask([502])
        self.assertIs(clock.sleep, before)

    def test_the_token_is_sent_when_there_is_one_and_not_otherwise(self):
        self.assertEqual(self.ask([{"a": 1}])[1], ["Bearer made-up"])
        self.assertEqual(self.ask([{"a": 1}], token="")[1], [None])

    def test_too_many_requests_is_no_answer_and_is_asked_again(self):
        answer, asked = self.ask([429, {"a": 1}])
        self.assertEqual((answer, len(asked)), ({"a": 1}, 2))
        answer, asked = self.ask([429])
        self.assertEqual((answer, len(asked)), ("GitHub's API did not answer", 5))

    def test_a_rate_limit_or_a_refused_token_is_no_answer(self):
        for answer in ((403, {"X-RateLimit-Remaining": "0"}), (403, {"Retry-After": "60"}), 401):
            with self.subTest(answer=answer):
                said, asked = self.ask([answer])
                self.assertEqual((said, len(asked)), ("GitHub's API did not answer", 5))

    def test_an_answer_that_says_no_is_not_asked_again(self):
        for code in (403, 404, 410, 422):
            with self.subTest(code=code):
                answer, asked = self.ask([code])
                self.assertIsNone(answer)
                self.assertEqual(len(asked), 1)

    def test_a_failed_answer_is_asked_again(self):
        for code in (500, 502, 429):
            with self.subTest(code=code):
                answer, asked = self.ask([code, {"a": 1}])
                self.assertEqual(answer, {"a": 1})
                self.assertEqual(len(asked), 2)

    def test_an_environment_is_believed_to_wait_only_for_a_named_reviewer(self):
        check = load("check.py")
        cases = [({"protection_rules": ["x"]}, False),
                 ({"protection_rules": [{"type": "required_reviewers", "reviewers": "someone"}]}, False),
                 ({"protection_rules": [{"type": "branch_policy", "reviewers": [{"type": "User"}]}]}, False),
                 ({"protection_rules": "x"}, False), (REVIEWED, True)]
        for answer, expected in cases:
            with self.subTest(answer=answer):
                real = check.api_get
                check.api_get = lambda path: answer
                try:
                    result = raised(lambda: self.assertEqual(check.environment_waits_for_a_named_reviewer(), expected))
                finally:
                    check.api_get = real
                self.assertIsNone(result)


class ForkPathsEdgeTest(ForkPathsCase):
    """The check of a fork's paths refuses what it cannot read completely, and says so."""

    def test_a_wrong_call_is_refused(self):
        for args in (["--number", "x", "--base-sha", "a" * 40], ["--number", "7", "--base-sha", "z"], ["--number", "7"]):
            with self.subTest(args=args):
                done = subprocess.run([sys.executable, "-I", str(FORK_PATHS), *args], capture_output=True, text=True,
                                      env=clean_env(), timeout=120)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("::error::the check of a fork's paths was not called as its workflow calls it", done.stdout)

    def test_a_listing_or_a_page_that_cannot_be_read_is_refused_by_name(self):
        for answers in ({"/repos/made-up/repo/pulls/7": 404}, {"/repos/made-up/repo/pulls/7/files?per_page=100&page=1": 404},
                        {"/repos/made-up/repo/contents/bench/tests?ref=main": 404},
                        {"/repos/made-up/repo/pulls/7/files?per_page=100&page=1": 500},
                        {"/repos/made-up/repo/contents/bench/tasks?ref=main": 404}):
            with self.subTest(answers=answers):
                code, out = self.run_paths([self.entry("README.md")], extra_answers=answers, quick=True)
                self.assertEqual(code, 1, out)
                self.assertTrue("could not be read completely" in out or SILENT in out, out)

    def test_a_pull_request_that_touches_no_closed_path_is_told_so(self):
        code, out = self.run_paths([self.entry("README.md")], extra_answers={
            "/repos/made-up/repo/contents/bench/tests?ref=main": [{"name": "test_judge.py", "type": "file"}, "junk"]})
        self.assertEqual(code, 0, out)
        self.assertIn("touches no path that the working rules close", out)

    def test_every_page_of_the_list_of_paths_is_read(self):
        first = [self.entry(f"docs/n{index:03}.md") for index in range(100)]
        pull = {"number": 7, "changed_files": 101, "draft": False, "base": {"ref": "main"}}
        code, out = self.run_paths(first, pull=pull, extra_answers={
            "/repos/made-up/repo/pulls/7/files?per_page=100&page=2": [self.entry(".github/x.yml")]})
        self.assertEqual(code, 1, out)
        self.assertIn(".github/", out)

    def test_a_new_task_folder_is_known_by_the_folders_of_the_listing_only(self):
        added = [self.entry("bench/tasks/brand-new-task/task.toml", "added")]
        listing = [{"name": "brand-new-task", "type": "file"}, "junk"]
        code, out = self.run_paths(added, tasks=listing)
        self.assertEqual(code, 0, out)
        full = [{"name": "brand-new-task", "type": "file"}] + [{"name": f"t{index}", "type": "dir"} for index in range(999)]
        code, out = self.run_paths(added, tasks=full)
        self.assertEqual(code, 1, out)

    def test_something_it_does_not_know_stops_it_with_a_fixed_message(self):
        paths = load("fork_paths.py")
        real = paths.judge

        def odd(number, base):
            raise RuntimeError("made up " + MARKER)

        import contextlib
        import io
        printed = io.StringIO()
        paths.judge = odd
        try:
            with contextlib.redirect_stdout(printed):
                code = paths.main(["--number", "7", "--base-sha", "a" * 40])
        finally:
            paths.judge = real
        self.assertEqual(code, 1)
        self.assertIn("::error::the check of a fork's paths stopped on something it does not know", printed.getvalue())
        self.assertNotIn(MARKER, printed.getvalue())


class RunTestsTest(unittest.TestCase):
    """The runner of these tests fails when a test fails and when it cannot find or run every test it found."""

    def run_in(self, files):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        for name, text in files.items():
            (folder / name).write_text(text)
        done = subprocess.run([sys.executable, "-I", str(HERE / "run_tests.py"), str(folder)], capture_output=True,
                              text=True, env=clean_env(), timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_passing_tests_pass_and_are_counted(self):
        code, out = self.run_in({"test_a.py": "import unittest\nclass A(unittest.TestCase):\n"
                                              "    def test_one(self): pass\n    def test_two(self): pass\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("2 tests of the commit check passed", out)

    def test_a_failing_test_fails_the_run(self):
        code, out = self.run_in({"test_a.py": "import unittest\nclass A(unittest.TestCase):\n"
                                              "    def test_one(self): self.fail('made up')\n"})
        self.assertEqual(code, 1, out)

    def test_no_test_at_all_fails_the_run(self):
        code, out = self.run_in({"notes.txt": "nothing"})
        self.assertEqual(code, 1, out)
        self.assertIn("could not be found", out)

    def test_a_test_file_that_does_not_load_fails_the_run(self):
        code, out = self.run_in({"test_a.py": "import nothing_of_that_name\n"})
        self.assertEqual(code, 1, out)


if __name__ == "__main__":
    unittest.main()
