# The commit check

Every commit that reaches the repository is checked by a job of the automatic check, not by hand.

## What a commit must keep

For every commit after the exempt commit named in `settings.json`:

- **Addresses.** The author's and the committer's address is a GitHub no-reply address. GitHub's own address
  (`noreply@github.com`) stands only as the committer of a commit that GitHub signed, and GitHub's API must say
  so; the check asks three times, and whatever goes wrong counts as not signed.
- **Names.** A name names no AI and does not match the private pattern. The login inside a name's own no-reply
  address is spared the private pattern, because GitHub shows it in the address anyway; a login that names an AI
  is not spared. On a commit GitHub signed, the committer is named GitHub, and the author's name that GitHub puts
  there is held to the same rules.
- **Attribution**, in the message and in a pull request's title and description, is refused by its form, whatever
  tool it names:
  - in the last paragraph, where trailers stand, a key with a hyphen in it is refused, except a co-author line for
    a person with a no-reply address whose name passes the rules for names;
  - on every line, a key of words that ends in "by" (such as a "signed off by" or "reviewed by" line), a change
    id, a one-word key that is a word of attribution (author, committer, agent, model, tool, assistant, bot, AI and
    their like) or whose value holds an address, and a line that begins by saying how the text was made
    ("generated with", "authored by", and the like) or carries the robot sign.
  A heading of two words ("Case runner: ...") is no trailer and passes.
- **Every place** (the message, the title, the description, the branch's name, every line a commit adds and every
  path it adds or renames) carries no work item number, with or without its dash; no address of the tracker; no
  id of the sessions' tooling; no path of the computer the project's sessions run on, known by its folder parts
  whatever stands in front (a home folder of macOS or Windows, the sessions' tool folders, working folders and
  temporary folders, and folders named after a home folder); and nothing that matches the private pattern.
  Every text is first brought to one form: the forms of a character, letter case, dashes, spaces and characters
  without width. What is not caught, on purpose: a temporary folder (`/tmp/...`: 91 added lines of 32 commits of
  main, the isolation tests) and a home folder of Linux (`/home/<name>/...`: 17 added lines of 9 commits of main, a
  home inside the judge's container); and a bare id of the UUID form (30 added lines of 9 commits, made-up ids in
  tests). Tests that need an offending text as a sample put it together from parts.
- **Lists.** Where a property cannot be closed by its form, a list of tools' names stays a list: a branch named
  after a tool (`<tool>/...`), "generated with" or "co-authored by" followed by a tool's name inside an added line,
  and a person's name that names a tool.

Every line a commit adds is read as text, whatever git or an attributes file thinks of the file, and the
difference is read by its structure: a file's header only outside a hunk, a hunk by the lengths its own header
gives. Lines that a merge adds beyond all of its parents count as the merge's own. The difference and the paths
of the whole range are checked as well, so that a merge cannot bring back a line or a path of the exempt history.

What the check prints is made by the check alone: a commit's id, a property's name, and positions by number (a
message's line, the number of a file in the commit's difference and its line, the number of a path in git's
list). It never prints a text, a path, a title or anything else it read, also not when it cannot run.

## Where it runs

The owner's gate refuses a pull request whose head carries any run that was cancelled or failed, not only the
latest one. So no run of the check is cancelled (neither of its workflows has a concurrency group), no clock
decides, and nothing waits and nothing is read again: a run fails at once on whatever breaks a rule, and only
then, or because the way for forks is shut.

- **Pushes to a branch and the project's own pull requests** are judged in the workflow `commits` by the branch's
  own copy of the check, with the project's private pattern. A push from one of the project's branches runs that
  branch's workflows with the repository's secrets anyway, so this opens nothing new, and a broken copy of the
  check's script on main cannot block the pull request that mends it. A branch cannot loosen the settings it is
  judged by: it fails when it stops expecting the private pattern or moves the exempt commit, compared with main;
  the comparison is a required argument, given once. A push to a branch checks the branch's name as well. Tags
  are outside the check.
- **Main** is checked on every push from the push's "before" commit, so that the range is never empty. A merge
  commit is made at the moment of merging and is not part of the pull request, so on main the check detects and
  does not prevent: a red main stops the next merge at the owner's gate.
- **Pull requests from forks** are judged in the workflow `pull-requests` by main's copy, under
  `pull_request_target`. Nothing of the pull request runs there: its commits are fetched into a ref and read as
  data, and its paths are read from GitHub's answer as data. Two jobs judge a fork's pull request:
  - `pull-request-paths` fails at once, with no secret and no approval, when the pull request touches a path that
    the working rules close to outside pull requests. The closed paths are those of the desk's tool on main
    (`tools/desk/pr_files.py`), with its exception for the page's text and its allowance for new tests and new
    benchmark tasks.
  - `pull-request-commits` runs in the environment `private-check` and names only that environment's own secret,
    `FORK_CHECK_PRIVATE_PATTERN`, so that the project's secret can never reach it. It waits for the project's
    approval, which the coordinating session gives after both reviews, for every waiting run of that head at once
    (rejecting a waiting run would leave a failed run on the head). Every new push or edit starts a run that waits
    again, so the private part never turns green by being skipped.
  - A pull request from the project's own branches runs no code there: one job prints that the workflow `commits`
    judges it.

## The way for forks is not open yet

Until `fork_way_open` in `settings.json` is `true` and `outside_runs_held_since` holds a date, a fork's
`pull-request-commits` says that the way is not open, judges nothing and ends failed: ending it in success would be
a green that checked nothing. A head that stays refused while the way is shut is the way working; once it opens,
the pull request takes main in and has a new head. The job `pull-request-paths` works from the day of the merge.

The way is opened by a small pull request of its own, after the owner has:

1. made the environment `private-check`, with the project's account as its required reviewer and "Prevent
   self-review" off;
2. given that environment its own secret `FORK_CHECK_PRIVATE_PATTERN`, and given the repository no secret of that
   name;
3. turned on GitHub's setting that holds the workflow runs of every outside contributor until the project approves
   them (today it holds first-time contributors only).

That pull request sets `fork_way_open` to `true` and writes the date of the third step into
`outside_runs_held_since`. From then on, every push to main asks GitHub's API what a job's token can read: that the
environment exists and its reviewer rule names a reviewer, and that `FORK_CHECK_PRIVATE_PATTERN` is empty in a job
that is in no environment, which it is only when the repository holds no secret of that name. Main turns red when
one of them fails. The third step cannot be read by a job's token: GitHub answers 403, with or without the
permission actions (measured on the side branch of this check), because it needs the repository's administration
permission. For that step, the date in `settings.json` is the owner's word, and it is the only part a job cannot
confirm. In a fork's run the check asks again that the environment names a reviewer and judges nothing if not.

## The private pattern

One regular expression holds what may not appear in public text and is not written in any file: names, and a
few numbers the project keeps to itself. It is matched on the text as written and on the text brought to one form.
The owner sets the repository's secret `COMMIT_CHECK_PRIVATE_PATTERN`; then a small pull request turns
`private_pattern_expected` in `settings.json` to `true`. While it is `false`, a missing secret passes with a notice;
once it is `true`, a missing secret fails. GitHub writes the account's displayed name as the author of every merge
commit it makes, and that name is held to the rules for names: before the pattern is expected, the displayed name
must not match it.

## When a text breaks a rule

The run fails at once. The way back is a new head: mend the title, the description or the branch first; then, on
a branch of the project, push an empty commit whose message says why it is there (never an amended commit, never a
forced push); on a fork, any new commit. The same holds after GitHub's API failed on a head without a fault.

So that it is rare, run the check on the text before it is published:

    python3 -I .github/commit-check/check.py --text-only --settings .github/commit-check/settings.json \
        --context own --event text.json

where `text.json` holds `{"pull_request": {"title": "...", "body": "...", "head": {"ref": "<branch>"}}}`. It
needs no repository and applies the same rules; with the private pattern in `COMMIT_CHECK_PRIVATE_PATTERN` it
applies that too.

## When main turns red

Main's history is never rewritten, and the offending commit never comes into a range again: a branch is checked
from main onwards, a pull request from its base, main from the push's "before". The way back:

1. The coordinating session opens a pull request that says what happened and how the next merges avoid it.
2. Its own checks pass, because they look only at the commits after the red one.
3. The owner merges it by hand, because the owner's gate asks for a green main.
4. The push run of that merge looks only at the commits after the red one, and main is green again.

## When main's copy is broken

A broken copy of the check's script on main can block only pull requests from forks: the project's own pull
requests are judged by their branch's own copy, also the one that mends it. A broken `pull-requests.yml` on main is
different: GitHub runs main's copy of that file for every pull request, the project's own too, so a failure of its
job for the project's own branches would stand on their heads as well, the mend's included. Then the owner merges
the mend by hand. Nothing else goes around the gate.

## Every run on one head

| Event on one head | Own pull request | Pull request from a fork |
| --- | --- | --- |
| push to the branch | `commits`: the branch's commits and name; fails only on what breaks a rule | (the fork's own workflows, not ours) |
| opened, synchronize, reopened | `commits`: commits and text; `pull-requests`: the job for own branches prints and passes | `pull-request-paths`: fails at once on a closed path; `pull-request-commits`: waits for approval, then judges at once (while the way is shut: says so and fails) |
| edited | `commits`: commits and the new text | `pull-request-paths` again; another `pull-request-commits` that waits |
| `ci`, `site` | unchanged by this check | the fork's copies |

No run of the check is cancelled; none waits on a clock.

## Tests

`python3 -I -m unittest discover -s .github/commit-check` runs the tests of the check, of the check of a fork's
paths and of both workflows. They need nothing but Python and git. `break_check.py` breaks the check by machine and
runs the tests against every break; it runs in the automatic check on a side branch, not on the project's computer.
