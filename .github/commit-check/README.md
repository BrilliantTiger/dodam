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

- **Pushes and the project's own pull requests** are judged in the workflow `commits` by the branch's own
  copy of the check, with the private pattern. A push from one of the project's branches runs that
  branch's workflows with the repository's secrets anyway, so this opens nothing new, and a broken copy on
  main cannot block the pull request that mends it. A branch cannot loosen the settings it is judged by:
  it fails when it stops expecting the private pattern or moves the exempt commit, compared with main.
- **Pull requests from forks** are judged in the workflow `pull-requests` by main's copy, under
  `pull_request_target`. Nothing of the pull request runs there: its commits are fetched into a ref and
  read as data. A fork's pull request is held to the private pattern only in the run that the label
  `private-check` starts, which only the project's account can put on; every later push, edit or removal
  of the label turns that check red again until the label is put on anew, so nobody outside can find
  the pattern by trying.
- **Main** is checked on every push from the push's "before" commit, so that the range is never empty.

## The private pattern

One regular expression in the repository's secret `COMMIT_CHECK_PRIVATE_PATTERN` holds what may not appear
in public text and is not written in any file: names, and a few numbers the project keeps to itself.
While `private_pattern_expected` in `settings.json` is `false`, a missing secret passes with a notice.
Once it is `true`, a missing secret fails on the project's own branches, and a fork's pull request
waits for the label.

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
