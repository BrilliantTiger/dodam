"""Tests that hold the two workflows of the commit check to the text they must have.

The gate on pull requests from forks ("pull-requests") runs main's copy under pull_request_target: it holds the
repository's token and could read secrets also for a pull request from a fork. The workflow "commits" runs a
branch's own copy and gets the project's secret only for a push or for a pull request from the project's own
branches. A rule that looks for forbidden shapes misses the shapes it does not know, so these tests hold the whole
text of both files, comments included: every shell block, every action line and every expression. A change of
either workflow is then always a change of this file, in sight of a review. Any further workflow file fails too.

Run: python3 -I .github/commit-check/run_tests.py
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
# - the job that judges a fork's commits runs in the environment private-check, names only that environment's own
#   secret, and starts only after a first job without environment or secret said that settings.json opens the way for
#   forks and that the environment names a reviewer; until then that first job fails with a fixed message.
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

  # The first job of a fork's run names no environment and no secret: it reads main's settings and, when they say the
  # way for forks is open, asks GitHub whether the environment names a reviewer. While the way is shut, nobody is named,
  # or it cannot tell, it fails with a fixed message, and the job that names the environment and the secret never
  # starts, so GitHub makes no environment and hands no secret to a runner.
  pull-request-way:
    if: github.event.pull_request.head.repo.id != github.repository_id
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    outputs:
      open: ${{ steps.way.outputs.open }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Ask whether the way for forks is open and a reviewer is named
        id: way
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -I .github/commit-check/check.py --fork-way-gate --settings .github/commit-check/settings.json --context fork-approved

  # A pull request from a fork: every commit after the exempt commit, its title, description and branch name, with the
  # environment's own private pattern. It waits for the project's approval, given after both reviews for every waiting
  # run of the head at once; before its secret is named, a step asks once more for the named reviewer.
  pull-request-commits:
    needs: pull-request-way
    if: github.event.pull_request.head.repo.id != github.repository_id && needs.pull-request-way.outputs.open == 'true'
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    environment: private-check
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
          fetch-depth: 0
      - name: Ask once more for the named reviewer, before any step that names the secret
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -I .github/commit-check/check.py --fork-way-gate --settings .github/commit-check/settings.json --context fork-approved
      - name: Fetch the pull request's commits as data
        env:
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          case "$HEAD_SHA" in *[!0-9a-f]*|'') echo "::error::no head commit"; exit 1;; esac
          git fetch --no-tags --quiet origin "+$HEAD_SHA:refs/remotes/pull/head"
      - name: The check's own tests
        run: python3 -I .github/commit-check/run_tests.py
      - name: Check the commits and the text
        env:
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          FORK_CHECK_PRIVATE_PATTERN: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          main=$(git rev-parse --verify origin/main)
          python3 -I .github/commit-check/check.py --repo . --from "$main" --head "$HEAD_SHA" \
            --event "$GITHUB_EVENT_PATH" --build-merge --settings .github/commit-check/settings.json \
            --context fork-approved --compare-settings "$main"
'''

COMMITS = r'''# Every commit that a push to a branch brings, and every commit and the text of a pull request from the project's
# own branches: the properties at the top of .github/commit-check/check.py. It runs the branch's own copy of the
# check, with the private pattern: a push from one of the project's branches runs that branch's workflows with the
# repository's secrets anyway, so judging the branch by its own copy opens nothing new, and a broken copy of the
# check's script on main cannot block the pull request that mends it. A branch or a pull request that does not change
# the check or its two workflows is judged by the base's copy instead, so that a rule main gained since the branch
# was made holds for it before its merge; one that changes them must have main's newest commit among its ancestors. A pull request from a fork is judged by main's
# copy in the workflow "pull-requests"; here it only runs the check's own tests. Tags are outside the check.
#
# The owner's gate refuses a head on which any run ended otherwise than in success, so no run here is cancelled
# (there is no concurrency group), nothing waits and nothing is read again: a run fails only on a commit, a text or
# a path that breaks a rule. The way back after a text is mended is a new head: on a branch of the project an empty
# commit whose message says why. The runs of a branch also read ahead what a merge will carry (the displayed name of
# the account that merges) and, once the way for forks is open, what main's push asks; the fork's secret is passed only
# as whether it is set, never its value. On main the range starts at the push's "before", so it is never empty; there the
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
        run: python3 -I .github/commit-check/run_tests.py
      - name: Check the commits of a push to a branch other than main, and the branch's name
        if: github.event_name == 'push' && github.ref != 'refs/heads/main'
        env:
          BRANCH: ${{ github.ref_name }}
          AFTER: ${{ github.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN != '' }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          check=.github/commit-check
          if git diff --quiet origin/main..."$AFTER" -- .github/commit-check .github/workflows/commits.yml \
              .github/workflows/pull-requests.yml; then
            mkdir -p "$RUNNER_TEMP/base"
            git archive origin/main .github/commit-check | tar -x -C "$RUNNER_TEMP/base"
            check="$RUNNER_TEMP/base/.github/commit-check"
          elif ! git merge-base --is-ancestor origin/main "$AFTER"; then
            echo "::error::This branch changes the commit check: take main in first, so that main's newest commit is among its ancestors."
            exit 1
          fi
          python3 -I "$check/check.py" --repo . --from origin/main --head "$AFTER" --branch "$BRANCH" \
            --check-fork-way --settings "$check/settings.json" --context own --compare-settings origin/main
      - name: Check the commits of a push to main, and what the way for forks needs when it is open
        if: github.event_name == 'push' && github.ref == 'refs/heads/main'
        env:
          BEFORE: ${{ github.event.before }}
          AFTER: ${{ github.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN != '' }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          python3 -I .github/commit-check/check.py --repo . --from "$BEFORE" --head "$AFTER" --require-commits \
            --check-fork-way --settings .github/commit-check/settings.json --context own --compare-settings "$BEFORE"
      - name: Check the commits and the text of a pull request from the project's own branches
        if: github.event_name == 'pull_request' && github.event.pull_request.head.repo.id == github.repository_id
        env:
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}
          FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN != '' }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          main=$(git rev-parse --verify origin/main)
          check=.github/commit-check
          if git diff --quiet "$main"..."$HEAD_SHA" -- .github/commit-check .github/workflows/commits.yml \
              .github/workflows/pull-requests.yml; then
            mkdir -p "$RUNNER_TEMP/base"
            git archive "$main" .github/commit-check | tar -x -C "$RUNNER_TEMP/base"
            check="$RUNNER_TEMP/base/.github/commit-check"
          elif ! git merge-base --is-ancestor "$main" "$HEAD_SHA"; then
            echo "::error::This pull request changes the commit check: take main in first, so that main's newest commit is among its ancestors."
            exit 1
          fi
          python3 -I "$check/check.py" --repo . --from "$main" --head "$HEAD_SHA" --event "$GITHUB_EVENT_PATH" \
            --build-merge --check-fork-way --settings "$check/settings.json" --context own --compare-settings "$main"
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
        # The fork's secret reaches the workflow commits only as whether it is set, in the three steps that ask what
        # the way for forks needs, never as its value.
        self.assertEqual(COMMITS.count("secrets.FORK_CHECK_PRIVATE_PATTERN"), 3)
        self.assertEqual(COMMITS.count("FORK_SECRET_AT_REPOSITORY_LEVEL: ${{ secrets.FORK_CHECK_PRIVATE_PATTERN != '' }}"), 3)
        self.assertEqual(COMMITS.count("--check-fork-way"), 3)

    def test_every_run_of_the_check_names_its_context_and_its_comparison_once(self):
        # A call of the fork's first job names its context and no range; every other call names both once.
        for name, text in (("pull-requests.yml", GATE), ("commits.yml", COMMITS)):
            calls = text.split("check.py ")[1:]
            self.assertTrue(calls, name)
            for call in calls:
                command = call.split("\n      - ", 1)[0].split("\n\n", 1)[0]
                with self.subTest(name=name):
                    self.assertEqual(len(re.findall(r"--context (?:own|fork-approved)\b", command)), 1)
                    if command.startswith("--fork-way-gate"):
                        self.assertNotIn("--compare-settings", command)
                        self.assertNotIn("--from", command)
                    else:
                        self.assertEqual(command.count("--compare-settings"), 1)

    def test_a_fork_s_job_that_names_the_environment_waits_for_the_first_job_and_its_open(self):
        judging = GATE.split("  pull-request-commits:\n", 1)[1]
        self.assertIn("    needs: pull-request-way\n", judging)
        self.assertIn("needs.pull-request-way.outputs.open == 'true'", judging)
        first = GATE.split("  pull-request-way:\n", 1)[1].split("\n  pull-request-", 1)[0]
        self.assertNotIn("environment:", first)
        self.assertNotIn("secrets.", first)
        steps = judging.split("      - ")
        gate = next(index for index, step in enumerate(steps) if "--fork-way-gate" in step)
        secret = next(index for index, step in enumerate(steps) if "secrets." in step)
        self.assertLess(gate, secret)


if __name__ == "__main__":
    unittest.main()
