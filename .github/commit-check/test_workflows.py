"""Tests that hold the two workflows of the commit check to the lines they must keep.

The gate on pull requests from forks ("pull-requests") runs main's copy under pull_request_target: it
holds the repository's token and could read secrets also for a pull request from a fork. The workflow
"commits" runs a branch's own copy and gets the secret only for a push or for a pull request from the
project's own branches. The rules below read the files as text; each test of a broken copy shows that
breaking one line makes a rule fail.

Run: python -m unittest discover -s .github/commit-check
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"
GATE = WORKFLOWS / "pull-requests.yml"
COMMITS = WORKFLOWS / "commits.yml"
OTHERS = [WORKFLOWS / "ci.yml", WORKFLOWS / "site.yml", WORKFLOWS / "pages.yml"]

OWN_REPO = "github.event.pull_request.head.repo.id == github.repository_id"
FORK = "github.event.pull_request.head.repo.id != github.repository_id"
LABELLED = "github.event.action == 'labeled' && github.event.label.name == 'private-check'"
SECRET = "secrets.COMMIT_CHECK_PRIVATE_PATTERN"
FORBIDDEN_IN_RUN = re.compile(
    r"\bgit\s+(?:checkout|switch|worktree|reset|merge|pull|restore|am|apply|cherry-pick|rebase|submodule|stash)\b"
    r"|\b(?:pip|pip3|npm|npx|yarn|make|cargo|go|uv|poetry|bash|sh|source|eval)\b|(?:^|\s)\./")


def uncommented(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def run_blocks(text):
    """The shell text of every run key, one-line and block form."""
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        found = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[index])
        if not found:
            index += 1
            continue
        indent = len(found.group(1))
        rest = found.group(2)
        body = []
        if rest in ("|", ">", "|-", ">-"):
            index += 1
            while index < len(lines) and (not lines[index].strip()
                                          or len(lines[index]) - len(lines[index].lstrip()) > indent):
                body.append(lines[index])
                index += 1
        else:
            body.append(rest)
            index += 1
        blocks.append("\n".join(body))
    return blocks


def top_level_block(text, key):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() == f"{key}:" or line.startswith(f"{key}: "):
            body = [line]
            for following in lines[index + 1:]:
                if following and not following.startswith(" "):
                    break
                body.append(following)
            return [entry for entry in body if entry.strip()]
    return None


def steps(text):
    """The text of each step of the file's jobs."""
    found = []
    current = None
    for line in text.splitlines():
        if re.match(r"^      - ", line):
            current = [line]
            found.append(current)
        elif current is not None and (line.startswith("        ") or not line.strip()):
            current.append(line)
        else:
            current = None
    return ["\n".join(step) for step in found]


def step_if(step):
    found = re.search(r"^        if: (.*)$", step, re.M)
    return found.group(1).strip() if found else None


def job_names(text):
    return set(re.findall(r"^  ([\w-]+):\s*$", text.split("\njobs:", 1)[-1], re.M))


def common_problems(code, found):
    blocks = run_blocks(code)
    if not blocks or any("${{" in block for block in blocks):
        found.add("expression in a shell line")
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", code, re.M)
    if not uses or any(not re.fullmatch(r"actions/checkout@[0-9a-f]{40}", ref) for ref in uses):
        found.add("unpinned action")
    permissions = top_level_block(code, "permissions")
    if permissions is None or [line.strip() for line in permissions] != ["permissions:", "contents: read"] \
            or code.count("permissions:") != 1 or re.search(r"\bwrite(-all)?\b", code):
        found.add("permissions")
    if "secrets: inherit" in code or len(re.findall(r"secrets\.", code)) != code.count(SECRET):
        found.add("secret")
    return blocks


def gate_problems(text, others=()):
    """The lines of the gate on pull requests from forks."""
    found = set()
    code = uncommented(text)
    on = top_level_block(code, "on")
    kinds = ("opened", "synchronize", "reopened", "edited", "labeled", "unlabeled")
    if on is None or len(on) != 3 or on[1].strip() != "pull_request_target:" or not on[2].strip().startswith("types:") \
            or not all(re.search(r"\b" + kind + r"\b", on[2]) for kind in kinds):
        found.add("trigger")
    blocks = common_problems(code, found)
    # Nothing of the pull request runs: main's checkout, no other ref checked out, nothing installed, nothing run
    # but main's own check, the head fetched into a ref only.
    if re.search(r"^\s*(?:ref|repository|path):", code, re.M) or any(FORBIDDEN_IN_RUN.search(block) for block in blocks):
        found.add("runs something of the pull request")
    for block in blocks:
        for line in block.splitlines():
            if re.search(r"\bpython", line) and not re.match(
                    r"^\s*python3 -I (?:-m unittest discover -s )?\.github/commit-check(?:/check\.py)?\b", line):
                found.add("runs something of the pull request")
            if "git fetch" in line and not re.search(r":refs/remotes/pull/head\"?\s*$", line):
                found.add("runs something of the pull request")
    # The project's own branches: one step that only prints; every other step runs for a fork only.
    job_steps = steps(code)
    own = [step for step in job_steps if step_if(step) == OWN_REPO]
    if len(own) != 1 or not re.search(r"^        run: echo \"[^\"$`\\]*\"$", own[0], re.M) or "uses:" in own[0] \
            or "env:" in own[0] or any(step_if(step) != FORK for step in job_steps if step not in own):
        found.add("own branches")
    # The secret only in the run the project's label starts.
    secret_lines = [line for line in code.splitlines() if SECRET in line]
    if len(secret_lines) != 1 or not re.fullmatch(
            r"\s*COMMIT_CHECK_PRIVATE_PATTERN: \$\{\{ \(" + re.escape(LABELLED) + r"\) && " + re.escape(SECRET) + r" \|\| '' \}\}",
            secret_lines[0]):
        found.add("secret")
    if not re.search(r"^\s*CONTEXT: \$\{\{ \(" + re.escape(LABELLED) + r"\) && 'fork-labeled' \|\| 'fork' \}\}$", code, re.M):
        found.add("secret")
    concurrency = "\n".join(top_level_block(code, "concurrency") or [])
    if "github.event.pull_request.number" not in concurrency or "cancel-in-progress: true" not in concurrency:
        found.add("concurrency")
    names = job_names(code)
    if not names or any(names & job_names(other) for other in others):
        found.add("job name")
    return found


def commits_problems(text, others=()):
    """The lines of the workflow that judges pushes and the project's own pull requests by their own copy."""
    found = set()
    code = uncommented(text)
    on = top_level_block(code, "on")
    if on is None or [line.strip() for line in on[1:]] != ["push:", "pull_request:", "types: [opened, synchronize, reopened, edited]"]:
        found.add("trigger")
    common_problems(code, found)
    # The secret goes only to the step of a push and to the step of a pull request from the project's own branches.
    for step in steps(code):
        if SECRET in step and step_if(step) not in ("github.event_name == 'push'",
                                                     f"github.event_name == 'pull_request' && {OWN_REPO}"):
            found.add("secret")
        if SECRET in step and not re.search(r"^          COMMIT_CHECK_PRIVATE_PATTERN: \$\{\{ " + re.escape(SECRET) + r" \}\}$",
                                            step, re.M):
            found.add("secret")
    if not any(SECRET in step for step in steps(code)):
        found.add("secret")
    names = job_names(code)
    if not names or any(names & job_names(other) for other in others):
        found.add("job name")
    return found


class WorkflowTest(unittest.TestCase):

    def setUp(self):
        self.gate = GATE.read_text()
        self.commits = COMMITS.read_text()
        self.others = [path.read_text() for path in OTHERS]

    def test_the_gate_keeps_every_line(self):
        self.assertEqual(gate_problems(self.gate, [self.commits, *self.others]), set())

    def test_the_workflow_commits_keeps_every_line(self):
        self.assertEqual(commits_problems(self.commits, [self.gate, *self.others]), set())

    def broken(self, old, new, rule, count=1):
        self.assertIn(old, self.gate)
        variant = self.gate.replace(old, new, count)
        self.assertIn(rule, gate_problems(variant, [self.commits, *self.others]), f"breaking {old!r} went unnoticed")

    def broken_commits(self, old, new, rule, count=1):
        self.assertIn(old, self.commits)
        variant = self.commits.replace(old, new, count)
        self.assertIn(rule, commits_problems(variant, [self.gate, *self.others]), f"breaking {old!r} went unnoticed")

    # The gate.

    def test_a_second_trigger_is_noticed(self):
        self.broken("on:\n  pull_request_target:", "on:\n  pull_request:\n  pull_request_target:", "trigger")

    def test_a_missing_unlabeled_trigger_is_noticed(self):
        self.broken(", unlabeled]", "]", "trigger")

    def test_a_write_permission_is_noticed(self):
        self.broken("contents: read", "contents: write", "permissions")

    def test_permissions_given_to_a_job_are_noticed(self):
        self.broken("    runs-on: ubuntu-24.04", "    permissions:\n      pull-requests: read\n    runs-on: ubuntu-24.04",
                    "permissions")

    def test_missing_permissions_are_noticed(self):
        self.broken("permissions:\n  contents: read\n", "", "permissions")

    def test_an_expression_in_a_shell_line_is_noticed(self):
        self.broken('--head "$HEAD_SHA"', '--head "${{ github.event.pull_request.head.sha }}"',
                    "expression in a shell line")

    def test_an_action_pinned_by_a_tag_is_noticed(self):
        self.broken("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "actions/checkout@v7", "unpinned action")

    def test_checking_out_the_pull_request_s_head_is_noticed(self):
        self.broken("          persist-credentials: false\n",
                    "          persist-credentials: false\n          ref: ${{ github.event.pull_request.head.sha }}\n",
                    "runs something of the pull request")

    def test_switching_to_the_fetched_head_is_noticed(self):
        self.broken("          if [ \"$(git rev-parse", "          git checkout --quiet refs/remotes/pull/head\n"
                    "          if [ \"$(git rev-parse", "runs something of the pull request")

    def test_fetching_the_head_into_a_branch_is_noticed(self):
        self.broken(":refs/remotes/pull/head\"", ":refs/heads/main\"", "runs something of the pull request")

    def test_an_install_is_noticed(self):
        self.broken("        run: python3 -I -m unittest", "        run: pip install -r requirements.txt && python3 -I -m unittest",
                    "runs something of the pull request")

    def test_setting_up_python_is_noticed(self):
        self.broken("      - name: Fetch the pull request's commits as data",
                    "      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0\n"
                    "        if: " + FORK + "\n      - name: Fetch the pull request's commits as data", "unpinned action")

    def test_running_a_script_of_the_pull_request_is_noticed(self):
        self.broken("        run: python3 -I -m unittest discover -s .github/commit-check",
                    "        run: python3 -I -m unittest discover -s tests", "runs something of the pull request")

    def test_python_without_isolation_is_noticed(self):
        self.broken("python3 -I .github/commit-check/check.py", "python3 .github/commit-check/check.py",
                    "runs something of the pull request")

    def test_the_secret_for_every_pull_request_is_noticed(self):
        self.broken(") && secrets.COMMIT_CHECK_PRIVATE_PATTERN || '' }}", " || true) && secrets.COMMIT_CHECK_PRIVATE_PATTERN || '' }}",
                    "secret")

    def test_the_secret_by_another_name_is_noticed(self):
        self.broken("          GITHUB_TOKEN: ${{ github.token }}\n",
                    "          GITHUB_TOKEN: ${{ github.token }}\n          OTHER: ${{ secrets.OTHER }}\n", "secret")

    def test_a_label_of_another_name_is_noticed(self):
        self.broken("github.event.label.name == 'private-check'", "github.event.label.name != 'private-check'",
                    "secret", count=2)

    def test_a_fork_judged_as_labelled_without_the_label_is_noticed(self):
        self.broken(") && 'fork-labeled' ||", " || true) && 'fork-labeled' ||", "secret")

    def test_a_check_step_that_runs_also_for_the_project_s_own_branches_is_noticed(self):
        self.broken("      - name: Check the commits and the text\n        if: " + FORK + "\n",
                    "      - name: Check the commits and the text\n", "own branches")

    def test_a_check_step_that_runs_only_for_the_project_s_own_branches_is_noticed(self):
        self.broken("      - name: Check the commits and the text\n        if: " + FORK + "\n",
                    "      - name: Check the commits and the text\n        if: " + OWN_REPO + "\n", "own branches")

    def test_the_own_branches_step_running_more_than_a_message_is_noticed(self):
        self.broken('        run: echo "This pull request', '        run: echo "$(cat x)" "This pull request', "own branches")

    def test_a_run_that_is_not_cancelled_by_a_later_event_is_noticed(self):
        self.broken("cancel-in-progress: true", "cancel-in-progress: false", "concurrency")

    def test_a_job_named_like_one_of_another_workflow_is_noticed(self):
        self.broken("  pull-request-commits:\n", "  commits:\n", "job name")

    # The workflow commits.

    def test_the_secret_for_a_fork_s_pull_request_is_noticed(self):
        self.broken_commits("        if: github.event_name == 'pull_request' && " + OWN_REPO + "\n",
                            "        if: github.event_name == 'pull_request'\n", "secret")

    def test_the_secret_in_the_step_of_the_tests_is_noticed(self):
        self.broken_commits("        run: python3 -I -m unittest discover -s .github/commit-check\n",
                            "        env:\n          COMMIT_CHECK_PRIVATE_PATTERN: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}\n"
                            "        run: python3 -I -m unittest discover -s .github/commit-check\n", "secret")

    def test_pull_request_target_in_the_workflow_commits_is_noticed(self):
        self.broken_commits("  pull_request:\n", "  pull_request_target:\n", "trigger")

    def test_an_expression_in_a_shell_line_of_the_workflow_commits_is_noticed(self):
        self.broken_commits('--from "$BASE_SHA"', '--from "${{ github.event.pull_request.base.sha }}"',
                            "expression in a shell line")

    def test_a_write_permission_in_the_workflow_commits_is_noticed(self):
        self.broken_commits("contents: read", "contents: write", "permissions")

    def test_a_job_of_the_workflow_commits_named_like_one_of_ci_is_noticed(self):
        self.broken_commits("  commits:\n    runs-on", "  bench:\n    runs-on", "job name")


if __name__ == "__main__":
    unittest.main()
