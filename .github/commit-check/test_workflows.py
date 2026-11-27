"""Tests that hold the gate's workflow to the lines it must keep.

The workflow of the gate on pull requests runs main's copy under pull_request_target: it holds the
repository's token and could read secrets also for a pull request from a fork. The rules below read the
file as text; each test of a broken copy shows that breaking one line makes a rule fail.

Run: python -m unittest discover -s .github/commit-check
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"
GATE = WORKFLOWS / "pull-requests.yml"
CI = WORKFLOWS / "ci.yml"

OWN_REPO = "github.event.pull_request.head.repo.id == github.repository_id"
LABELLED = "github.event.action == 'labeled' && github.event.label.name == 'private-check'"
FORBIDDEN_IN_RUN = re.compile(
    r"\bgit\s+(?:checkout|switch|worktree|reset|merge|pull|restore|am|apply|cherry-pick|rebase|submodule|stash)\b"
    r"|\b(?:pip|pip3|npm|npx|yarn|make|cargo|go|uv|poetry|bash|sh|source|eval)\b|(?:^|\s)\./|\bactions/setup-")


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
                if following and not following.startswith((" ", "#")):
                    break
                body.append(following)
            return [entry for entry in body if entry.strip() and not entry.strip().startswith("#")]
    return None


def problems(text, ci_text=""):
    found = set()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    # Only pull_request_target starts it.
    on = top_level_block(code, "on")
    if on is None or [line.strip() for line in on[1:2]] != ["pull_request_target:"] or len(on) != 3 \
            or not on[2].strip().startswith("types:"):
        found.add("trigger")
    # Read-only permissions, written out once at the top and nowhere else.
    permissions = top_level_block(code, "permissions")
    if permissions is None or [line.strip() for line in permissions] != ["permissions:", "contents: read"] \
            or code.count("permissions:") != 1 or re.search(r"\bwrite(-all)?\b", code):
        found.add("permissions")
    # No expression inside a shell line.
    blocks = run_blocks(code)
    if not blocks or any("${{" in block for block in blocks):
        found.add("expression in a shell line")
    # Every action pinned by its commit.
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", code, re.M)
    if not uses or any(not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref) for ref in uses):
        found.add("unpinned action")
    # Nothing of the pull request runs: main's checkout, no other ref checked out, nothing installed,
    # nothing run but main's own check.
    if re.search(r"^\s*(?:ref|repository|path):", code, re.M) or any(FORBIDDEN_IN_RUN.search(block) for block in blocks) \
            or any(not ref.startswith("actions/checkout@") for ref in uses):
        found.add("runs something of the pull request")
    for block in blocks:
        for line in block.splitlines():
            if re.search(r"\bpython", line) and not re.match(r"^\s*python3 -I (?:-m unittest discover -s )?\.github/commit-check(?:/check\.py)?\b", line):
                found.add("runs something of the pull request")
            fetches = re.findall(r"git fetch\b.*", line)
            if any(not re.search(r":refs/remotes/pull/head\"?\s*$", fetch) for fetch in fetches):
                found.add("runs something of the pull request")
    # The secret only on the project's own branches, or in the run the project's label starts.
    secret_lines = [line for line in code.splitlines() if "secrets." in line or "secrets:" in line]
    if not secret_lines or any(not re.match(r"^\s*COMMIT_CHECK_PRIVATE_PATTERN: \$\{\{ \(" + re.escape(OWN_REPO)
                                            + r" \|\| \(" + re.escape(LABELLED) + r"\)\) && secrets\.COMMIT_CHECK_PRIVATE_PATTERN \|\| '' \}\}$",
                                            line) for line in secret_lines):
        found.add("secret for a fork")
    if not re.search(r"^\s*CONTEXT: \$\{\{ " + re.escape(OWN_REPO) + r" && 'own' \|\| \(" + re.escape(LABELLED)
                     + r"\) && 'fork-labeled' \|\| 'fork' \}\}$", code, re.M):
        found.add("secret for a fork")
    # A later event cancels a run still going, per pull request; and the label's removal starts a run.
    concurrency = top_level_block(code, "concurrency")
    if concurrency is None or "github.event.pull_request.number" not in "\n".join(concurrency) \
            or "cancel-in-progress: true" not in "\n".join(concurrency):
        found.add("concurrency")
    if on is None or not all(kind in on[2] for kind in ("synchronize", "edited", "labeled", "unlabeled", "opened", "reopened")):
        found.add("trigger")
    # The job's name differs from every job of ci.yml, so that two checks of one name never meet on a head.
    names = set(re.findall(r"^  ([\w-]+):\s*$", code.split("\njobs:", 1)[-1], re.M))
    ci_names = set(re.findall(r"^  ([\w-]+):\s*$", ci_text.split("\njobs:", 1)[-1], re.M))
    if not names or names & ci_names:
        found.add("job name")
    return found


class GateWorkflowTest(unittest.TestCase):

    def setUp(self):
        self.text = GATE.read_text()
        self.ci = CI.read_text()

    def test_the_gate_s_workflow_keeps_every_line(self):
        self.assertEqual(problems(self.text, self.ci), set())

    def broken(self, old, new, rule, count=1):
        self.assertIn(old, self.text)
        variant = self.text.replace(old, new, count)
        self.assertIn(rule, problems(variant, self.ci), f"breaking {old!r} went unnoticed")

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
                    "      - name: Fetch the pull request's commits as data", "runs something of the pull request")

    def test_running_a_script_of_the_pull_request_is_noticed(self):
        self.broken("        run: python3 -I -m unittest discover -s .github/commit-check",
                    "        run: python3 -I -m unittest discover -s tests", "runs something of the pull request")

    def test_python_without_isolation_is_noticed(self):
        self.broken("python3 -I .github/commit-check/check.py", "python3 .github/commit-check/check.py",
                    "runs something of the pull request")

    def test_the_secret_for_every_pull_request_is_noticed(self):
        self.broken("&& secrets.COMMIT_CHECK_PRIVATE_PATTERN || '' }}", "|| true && secrets.COMMIT_CHECK_PRIVATE_PATTERN || '' }}",
                    "secret for a fork")

    def test_the_secret_by_another_name_is_noticed(self):
        self.broken("          GITHUB_TOKEN: ${{ github.token }}\n",
                    "          GITHUB_TOKEN: ${{ github.token }}\n          OTHER: ${{ secrets.COMMIT_CHECK_PRIVATE_PATTERN }}\n",
                    "secret for a fork")

    def test_a_label_of_another_name_is_noticed(self):
        self.broken("github.event.label.name == 'private-check'", "github.event.label.name != 'private-check'",
                    "secret for a fork", count=2)

    def test_a_fork_judged_as_own_is_noticed(self):
        self.broken("&& 'own' ||", "|| true && 'own' ||", "secret for a fork")

    def test_a_run_that_is_not_cancelled_by_a_later_event_is_noticed(self):
        self.broken("cancel-in-progress: true", "cancel-in-progress: false", "concurrency")

    def test_a_job_named_like_one_of_ci_is_noticed(self):
        self.broken("  pull-request-commits:\n", "  commits:\n", "job name")


if __name__ == "__main__":
    unittest.main()
