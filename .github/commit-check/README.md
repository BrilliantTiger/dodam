# The commit check

Every commit that reaches the repository is checked by a job of the automatic check, not by hand.

## What a commit must keep

For every commit after the exempt commit named in `settings.json`:

- The author's and the committer's address is a GitHub no-reply address. GitHub's own address
  (`noreply@github.com`) stands only as the committer of a commit that GitHub signed, and GitHub's API
  must confirm the signature; when the API cannot be asked, after three tries, the commit fails.
- Each name is the login inside its own no-reply address; or, on a commit GitHub signed, the committer is
  named GitHub and the author's name is the one GitHub put there; or it is a name that names no AI and
  does not match the private pattern.
- The message has no co-author line that names an AI, no co-author with an address other than a
  no-reply one, and no "generated with" line that names an AI.
- The message, the lines the commit adds and the paths of the files it adds or renames carry no work item
  number, no address of the tracker, no path of the computer the project's sessions run on and no id of
  those sessions' tooling, and do not match the private pattern. Tests that need such a text as a sample
  put it together from parts.
- A pull request's title, description and branch name are held to the same, because GitHub writes the
  branch name and the title into the merge commit it makes on main.

Lines that a merge adds beyond all of its parents count as the merge's own. The difference of the whole
range is checked as well, so that a merge cannot bring back a line from the exempt history.

What the check prints names the commit and the property, never the offending text. For the private
pattern it does not name the file either.

## Where it runs

The owner's gate refuses a pull request whose head carries any run that was cancelled or failed, not only the
latest one. So no run of the check is cancelled (neither workflow has a concurrency group), and a run fails only
on what cannot be mended on its head: a commit, or the branch's name. A title or a description that breaks a rule
can be mended without a new commit, so it never makes a run fail while it can still be mended.

- **Pushes and the project's own pull requests** are judged in the workflow `commits` by the branch's own
  copy of the check, with the private pattern. A push from one of the project's branches runs that
  branch's workflows with the repository's secrets anyway, so this opens nothing new, and a broken copy on
  main cannot block the pull request that mends it. A branch cannot loosen the settings it is judged by:
  it fails when it stops expecting the private pattern or moves the exempt commit, compared with main.
  When a pull request's title or description breaks a rule, its run waits up to 15 minutes, reading them
  again every 15 seconds; once they are mended, every waiting run on that head ends in success. A run that
  waits is not finished, and the gate refuses that as well.
- **Pull requests from forks** are judged in the workflow `pull-requests` by main's copy, under
  `pull_request_target`, in one job that runs in the environment `private-check`. Nothing of the pull
  request runs there: its commits are fetched into a ref and read as data. The job waits for the project's
  approval, which the coordinating session gives after both reviews, for every waiting run of that head at
  once; a waiting job uses no runner. Every new push or edit starts a run that waits again, so the private
  part never turns green by being skipped. Once approved, the job reads the title and the description as they
  are then, uses the private pattern, and gives its judgement at once: it does not wait for a mend, so a fork
  cannot hold runners or try its text against the pattern again and again. The private pattern is used only
  when GitHub's API says that the environment waits for a reviewer.
- **Main** is checked on every push from the push's "before" commit, so that the range is never empty.

## Setting up the private pattern

One regular expression holds what may not appear in public text and is not written in any file: names, and
a few numbers the project keeps to itself. The owner sets it once:

1. The repository's secret `COMMIT_CHECK_PRIVATE_PATTERN`, for the project's own branches.
2. The environment `private-check`, with the project's account as its required reviewer, "Prevent self-review"
   off, and the same secret `COMMIT_CHECK_PRIVATE_PATTERN` as a secret of the environment.
3. Then a small pull request turns `private_pattern_expected` in `settings.json` to `true`.

While `private_pattern_expected` is `false`, a missing secret passes with a notice. Once it is `true`, a missing
secret fails, and so does a fork's run in an environment that does not wait for a reviewer.

## When main turns red

A merge commit is made at the moment of merging and is not part of the pull request, so on main the check
detects and cannot prevent. A red push run on main stops every later merge at the owner's gate, which is
the intended effect. Main's history is never rewritten, and the offending commit never comes into a range
again: a branch is checked from main onwards, a pull request from its base, main from the push's
"before". The way back:

1. The coordinating session opens a pull request that says what happened and how the next merges avoid it.
2. Its own checks pass, because they look only at the commits after the red one.
3. The owner merges it by hand, because the owner's gate asks for a green main.
4. The push run of that merge looks only at the commits after the red one, and main is green again.

## When main's copy is broken

A broken copy on main can block only pull requests from forks, which main's copy judges. The pull request
that mends it comes from one of the project's branches and is judged by its own, mended copy. Nothing
goes around the gate.

## Tests

`python3 -I -m unittest discover -s .github/commit-check` runs the tests of the check and of both
workflows. They need nothing but Python and git.
