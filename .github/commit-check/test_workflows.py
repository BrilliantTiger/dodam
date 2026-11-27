"""Tests that hold the two workflows of the commit check to the text they must have.

The gate on pull requests from forks ("pull-requests") runs main's copy under pull_request_target: it holds the
repository's token and could read secrets also for a pull request from a fork. The workflow "commits" runs a
branch's own copy and gets the project's secret only for a push or for a pull request from the project's own
branches. A rule that looks for forbidden shapes misses the shapes it does not know, so these tests hold the whole
text of both files, comments included: every shell block, every action line and every expression. A change of
either workflow is then always a change of this file, in sight of a review. Any further workflow file fails too.

Run: python3 -I -m unittest discover -s .github/commit-check
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"

# The workflows that exist; the three beside the two of the check are not the check's, and their names only are held.
WORKFLOW_FILES = {"ci.yml", "commits.yml", "pages.yml", "pull-requests.yml", "site.yml"}

GATE = r'''# The gate on every pull request from a fork. It runs main's copy of this file and of its scripts
# (pull_request_target), so a pull request from outside cannot change what checks it. A pull request from the
# project's own branches runs no code here: the workflow "commits" judges it with its branch's own copy. Because this
# workflow holds the repository's token and could read secrets for a pull request from a fork:
# - nothing from the pull request is executed or imported: the checkout is main's, the pull request's commits are
#   fetched into a ref and read as data, its paths are read from GitHub's answer as data, nothing is installed, and
#   git is told to use no diff program and no text conversion;
# - the only permission is contents, to read; it also reads whether the environment of forks waits for a reviewer;
# - no value of the event goes into a shell line through the expression syntax, only through env;
# - every action is pinned by its commit;
# - the job that judges a fork's commits runs in the environment private-check and names only that environment's
#   own secret. Until settings.json opens the way for forks, after the owner has set the environment with a named
#   reviewer, its secret and GitHub's setting that holds every run of an outside contributor, that job says the way is
#   not open, judges nothing and fails.
# A pull request from a fork that touches a path closed to outside pull requests fails the job pull-request-paths at
# once, with no secret and no approval; the closed paths are those of the desk's tool on main.
# No run here is cancelled and nothing waits but the environment's approval. The tests in
# .github/commit-check/test_workflows.py hold this file's whole text.
name: pull-requests

on:
  pull_request_target:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read

jobs:
  # A pull request from the project's own branches: judged by the job commits of the workflow commits.
  pull-request-own:
    if: github.event.pull_request.head.repo.id == github.repository_id
    runs-on: ubuntu-24.04
    timeout-minutes: 2
    steps:
      - name: A pull request from the project's own branches is judged by its branch's own copy
        run: echo "This pull request comes from a branch of the project; the job commits of the workflow commits judges it with that branch's own copy of the check."

  # A pull request from a fork that touches a closed path fails here at once.
  pull-request-paths:
    if: github.event.pull_request.head.repo.id != github.repository_id
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Fail when the pull request touches a path closed to outside pull requests
        env:
          NUMBER: ${{ github.event.pull_request.number }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -I .github/commit-check/fork_paths.py --number "$NUMBER" --base-sha "$BASE_SHA"

  # A pull request from a fork: every commit after the exempt commit, its title, description and branch name, with the
  # fork's own private pattern. It waits for the project's approval, given after both reviews for every waiting run of
  # the head at once; while the way for forks is shut it says so and fails.
  pull-request-commits:
    if: github.event.pull_request.head.repo.id != github.repository_id
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    environment: private-check
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
          fetch-depth: 0
      - name: Fetch the pull request's commits as data
        env:
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          case "$HEAD_SHA" in *[!0-9a-f]*|'') echo "::error::no head commit"; exit 1;; esac
          git fetch --no-tags --quiet origin "+$HEAD_SHA:refs/remotes/pull/head"
      - name: The check's own tests
        run: python3 -I -m unittest discover -s .github/commit-check
      - name: Check the commits and the text
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          FORK_CHECK_PRIVATE_PATTERN: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          python3 -I .github/commit-check/check.py --repo . --from "$BASE_SHA" --head "$HEAD_SHA" \
            --event "$GITHUB_EVENT_PATH" --settings .github/commit-check/settings.json --context fork-approved \
            --compare-settings "$BASE_SHA"
'''

COMMITS = r'''# Every commit that a push to a branch brings, and every commit and the text of a pull request from the project's
# own branches: the properties at the top of .github/commit-check/check.py. It runs the branch's own copy of the
# check, with the private pattern: a push from one of the project's branches runs that branch's workflows with the
# repository's secrets anyway, so judging the branch by its own copy opens nothing new, and a broken copy of the
# check's script on main cannot block the pull request that mends it. A pull request from a fork is judged by main's
# copy in the workflow "pull-requests"; here it only runs the check's own tests. Tags are outside the check.
#
# The owner's gate refuses a head on which any run ended otherwise than in success, so no run here is cancelled
# (there is no concurrency group), nothing waits and nothing is read again: a run fails only on a commit, a text or
# a path that breaks a rule. The way back after a text is mended is a new head: on a branch of the project an empty
# commit whose message says why. On main the range starts at the push's "before", so it is never empty; there the
# check detects and does not prevent, and a red main stops the next merge at the owner's gate.
#
# Reading whether the environment of forks waits for a reviewer needs no permission beyond contents in this public
# repository (measured on the side branch: the token of contents only reads an environment). It needs no package and ends in well under a minute.
name: commits

on:
  push:
    branches: ["**"]
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read

jobs:
  commits:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
          fetch-depth: 0
      - name: The check's own tests
        run: python3 -I -m unittest discover -s .github/commit-check
      - name: Check the commits of a push to a branch other than main, and the branch's name
        if: github.event_name == 'push' && github.ref != 'refs/heads/main'
        env:
          BRANCH: ${{ github.ref_name }}
          AFTER: ${{ github.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          python3 -I .github/commit-check/check.py --repo . --from origin/main --head "$AFTER" --branch "$BRANCH" \
            --settings .github/commit-check/settings.json --context own --compare-settings origin/main
      - name: Check the commits of a push to main, and what the way for forks needs when it is open
        if: github.event_name == 'push' && github.ref == 'refs/heads/main'
        env:
          BEFORE: ${{ github.event.before }}
          AFTER: ${{ github.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          python3 -I .github/commit-check/check.py --repo . --from "$BEFORE" --head "$AFTER" --require-commits \
            --check-fork-way --settings .github/commit-check/settings.json --context own --compare-settings "$BEFORE"
      - name: Check the commits and the text of a pull request from the project's own branches
        if: github.event_name == 'pull_request' && github.event.pull_request.head.repo.id == github.repository_id
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          python3 -I .github/commit-check/check.py --repo . --from "$BASE_SHA" --head "$HEAD_SHA" \
            --event "$GITHUB_EVENT_PATH" --settings .github/commit-check/settings.json --context own \
            --compare-settings "$BASE_SHA"
'''


class WorkflowTest(unittest.TestCase):

    def test_no_further_workflow_file_exists(self):
        self.assertEqual({path.name for path in WORKFLOWS.iterdir()}, WORKFLOW_FILES)

    def test_the_gate_on_pull_requests_from_forks_has_exactly_its_written_text(self):
        self.assertEqual((WORKFLOWS / "pull-requests.yml").read_text(encoding="utf-8"), GATE)

    def test_the_workflow_commits_has_exactly_its_written_text(self):
        self.assertEqual((WORKFLOWS / "commits.yml").read_text(encoding="utf-8"), COMMITS)

    def test_the_written_texts_use_no_form_that_hides_a_key_or_a_block(self):
        # What the written texts may hold: plain keys, block scalars only as "|", no mapping in one line, no anchors.
        for name, text in (("pull-requests.yml", GATE), ("commits.yml", COMMITS)):
            for number, line in enumerate(text.split("\n"), 1):
                content = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
                with self.subTest(name=name, line=number):
                    self.assertNotRegex(content, r"^\s*(?:- )?[\"']")
                    self.assertNotRegex(content, r"^\s*(?:- )?\{")
                    self.assertNotRegex(content, r"(?:^|\s)[&*!][\w-]")
                    self.assertNotRegex(content, r"<<\s*:")
                    header = re.match(r"^\s*(?:- )?[\w-]+:\s*([|>].*)$", content)
                    if header:
                        self.assertEqual(header.group(1), "|")

    def test_the_fork_s_secret_and_the_project_s_secret_stand_where_they_belong(self):
        # Shown on the written texts, so that a review of a change to them sees these lines change.
        self.assertEqual(GATE.count("secrets."), 1)
        self.assertIn("    environment: private-check\n", GATE.split("  pull-request-commits:\n", 1)[1])
        self.assertIn("FORK_CHECK_PRIVATE_PATTERN: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN }}",
                      GATE.split("  pull-request-commits:\n", 1)[1])
        self.assertNotIn("COMMIT_CHECK_PRIVATE_PATTERN", GATE)
        self.assertEqual(COMMITS.count("secrets.FORK_CHECK_PRIVATE_PATTERN"), 1)
        main_step = COMMITS.split("Check the commits of a push to main", 1)[1].split("      - name:", 1)[0]
        self.assertIn("FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN }}", main_step)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", main_step)

    def test_every_run_of_the_check_names_its_context_and_its_comparison_once(self):
        for name, text in (("pull-requests.yml", GATE), ("commits.yml", COMMITS)):
            calls = text.split("check.py ")[1:]
            for call in calls:
                command = call.split("\n      - ", 1)[0].split("\n\n", 1)[0]
                with self.subTest(name=name):
                    self.assertEqual(len(re.findall(r"--context (?:own|fork-approved)\b", command)), 1)
                    self.assertEqual(command.count("--compare-settings"), 1)


if __name__ == "__main__":
    unittest.main()
