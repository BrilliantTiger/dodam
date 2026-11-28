# The commit check

Every commit that reaches the repository is checked by a job of the automatic check, not by hand.

## The copy every rule is matched on

Every text (a message, a title, a description, a branch's name, a person's name, a line a commit adds, a path) is
first split into lines at every line or paragraph break that Python's own line splitting knows. Each line is then
copied for matching, by classes of characters from the language's own character tables, in this order:

1. taken apart by compatibility (the decomposed form, so that a letter and a mark set on it stay two);
2. every space becomes one space, and a run of spaces one space;
3. every character of the categories for marks set on or around another character, format, control, surrogate,
   private use and unassigned (Mn, Me, Cf, Cc, Cs, Co, Cn) is taken out, and so is every letter whose name holds the
   word FILLER;
4. every dash of the category Pd, and every mathematical sign whose name holds the word MINUS but not the word SET,
   becomes one dash;
5. put together again, so that Korean syllables come back whole;
6. for a person's name and a branch's name, a word is split where a small letter meets a capital, where capitals
   meet a capital followed by a small letter, and at an underscore;
7. letter case is folded.

An accent is folded away in the copy: a word that equals a forbidden one once its accents are gone is refused. A
character outside these classes that looks like a dash or a space (such as a bullet) is not folded.

## The rules, and what each one is

A rule is either a form, written out below in words and refusing exactly that, or a list, written out below. No
rule is yet closed over every spelling of what it means: making the forms closed, and one matcher for every place
and every position in a line, is the next version of this check.

Every place (the message, the pull request's title and description, the branch's name, every line a commit adds,
and the path of every file a commit adds, renames, copies or changes):

- **A work item number** (a form): the tracker's key, then a dash, an underscore, a space or nothing, then digits;
  not after a letter or a digit.
- **The tracker's address** (a form): its host name.
- **An id of the sessions' tooling** (a list): a terminal, a task, a context, a dispatch, a message or a workflow id
  of the coordinating program (the word, an underscore, eight or more hexadecimal digits), and a conversation
  address of a tool.
- **A path of the computer the project's sessions run on**:
  - a path inside a home folder of macOS or Windows (a form): the folder "Users" at the start of the text or after a
    separator, a separator, a name and a separator after it, the shared folder of macOS set aside (the home folder
    alone, with nothing after its name, passes);
  - the folders that tools keep (a list): an entry directly inside the folder of Claude Code, Codex, Gemini (the
    Antigravity tool keeps its sessions there) or the program the sessions run in; the settings file Claude Code
    keeps beside its folder; the sessions' working folders and project folders of that program; the temporary
    folder Claude Code makes, named after it and a number, wherever it stands; the temporary folders of macOS;
  - a folder named after a home folder (a form): "Users", a name and the program's name, joined by dashes.
- **The private pattern** (a form, kept in a secret): one regular expression of what may not appear in public text
  and is written in no file (names, and a few numbers the project keeps to itself). It is matched on the text as
  written and on its copy (a branch's name also on its copy split into words), in either letter case. It is written
  in the form the copy has: without accents, without compatibility forms such as full-width letters, and without a
  letter whose folded form is not its small form (such as the German sharp s); a secret written otherwise stops every
  run with a fixed message, because it could never match the copy.

What is not checked, on purpose: a temporary folder such as `/tmp`, which the isolation tests use (Claude Code's
temporary folder inside it is refused, as above), and a Linux home
folder such as `/home/agent`, which is the home inside the judge's container; and a bare id of the UUID form, which
the tests use as made-up values. A web route made of the word "users", a name and a separator is refused as a home
folder. A home folder with nothing after its name is not caught today.

Messages, titles and descriptions only (attribution):

- **A trailer** (a form): in the last paragraph, a key with a hyphen in it is refused, except a co-author line for a
  person with a no-reply address whose name passes the rules for names; on every line, a key of words that ends in
  "by" and a change id; a one-word key that is a word of attribution (a list: author, authors, committer, agent,
  model, models, tool, tools, assistant, assistants, bot, bots, AI, LLM, generator, generated, assisted, credit,
  credits, via, coauthor, coauthors) or whose value holds an address.
- **A line that says how the text was made** (a list), at the head of a line, after any marks: one of the verbs
  generated, co-authored, coauthored, assisted, authored, written, drafted, produced, made, created, built,
  prepared, implemented, followed by with, by, using, via or through; or one of the phrases "with help from",
  "with help of", "with the help from", "with the help of", "AI-generated", "AI-assisted"; or the robot sign
  anywhere. A heading of two words ("Case
  runner: ...") is no trailer and passes.

Added lines only: "generated" or "co-authored" followed by with, by, using or via, then a colon or none, spaces or
none, and a tool's name (a list).

Names (the author's and the committer's, a co-author's, and the displayed name GitHub writes on a merge commit):
a name names no tool (a list: the names of the tools below, and the word "bot") and does not match the private
pattern. A name that is its whole own no-reply login but for letter case is spared the private pattern, because
GitHub shows that login in the address anyway. A person whose name is a tool's name is refused. And on a commit
of more than one parent that GitHub signed, the displayed name GitHub writes on a merge commit must be the login of
its account, but for letter case, or empty (GitHub then writes the login): the check cannot tell any other name
from one kept private.

Before a merge, every pull request's run writes the merge commit GitHub will make: for each account that may merge,
its displayed name (its login when it has none) and its no-reply address as the author, GitHub as the signed
committer, and the message "Merge pull request #N from <owner>/<branch>" with the pull request's title. That commit
is judged by the same function that judges every commit on main's push, so a merge the pull request's run passes is a
merge main's push passes.

A branch's name: a branch named after a tool, that is, a tool's name and a slash at its head (a list), is refused.

The names of tools (a list): Claude, Anthropic, OpenAI, ChatGPT, GPT with or without a number, Codex, Gemini, Bard,
Copilot, Cursor, Devin, Antigravity, Llama, Mistral, DeepSeek, Grok, Windsurf, Aider, Cline, Qwen, Tabnine, Codeium,
CodeWhisperer, Jules, Perplexity, o1, o3, o4 and o4-mini.

Addresses: the author's and the committer's address is a GitHub no-reply address. GitHub's own address stands
only as the committer of a commit GitHub signed, as GitHub's API says.

## How the commits are read

Every line a commit adds is read as text, whatever git or an attributes file thinks of the file, by the structure
of the difference: a file's header only outside a hunk, a hunk by the lengths its own header gives. A new content
that is not text in UTF-8, or that holds a zero byte, is read once more in UTF-16 and UTF-32, in both byte orders,
from every byte a character of that width can start at, passing over bytes that are not valid there (so a mark at
the start or a header of a binary file hides nothing after it), and once more as UTF-8 with its zero bytes taken
out. Reading ends there: content that is compressed or encoded (an archive, an image's pixels, base64) is not read.

A commit with more than one parent is read by its own shape: the lines it adds beyond all of its parents count as
its own, and its combined list of paths gives one path for every entry, a renamed or copied one too (git gives two
paths only for a commit with one parent). A file the merge deletes has no line of the result. The difference and
the paths of the whole range are checked as well, so that a merge cannot bring back a line or a path of the exempt
history.

What the check prints is made by the check alone: a commit's id, a property's name and positions by number (a
message's line, the number of a file in the commit's difference and its line, the number of a path in git's list
and the encoding it was read in). It never prints a text, a path, a title or anything else it read.

## Where it runs

The owner's gate refuses a pull request whose head carries any run that was cancelled or failed. So no run of the
check is cancelled, and nothing is read again. The only clock is the budget of the tries at GitHub's API, and the
only waits are between those tries. A run fails at once on whatever breaks a rule, and otherwise only when the way
for forks is shut, when GitHub's API did not answer, or when the pull request does not merge cleanly with main's
tip.

- **Pushes to a branch and the project's own pull requests** are judged in the workflow `commits`. A branch or a pull
  request that does not change the check or its two workflows is judged by main's copy of the check, as main is
  when the run starts, so that a rule main gained since the branch was made holds before its merge. One that changes them is judged by its own
  copy, so that a broken copy on main never blocks the pull request that mends it, and must have main's newest commit
  among its ancestors. A branch cannot loosen the settings it is judged by. Tags are outside the check.
- **Main** is checked on every push from the push's "before" commit.
- **The merge**: a pull request's run (the project's own and a fork's) has git build the merge GitHub will make, of
  main's tip as it is when the run starts with the pull request's head, and a merge commit of the shape GitHub
  writes: both parents, the whole merge message ("Merge pull request #N from <owner>/<branch>", an empty line, the
  title), GitHub as the committer, and as the author each account that may merge (the account that made main's last
  merge, and on the project's own branches the account that pushed), with its displayed name, or its login when it
  has none, and its no-reply address. That commit, and the range from main's tip to it, are judged by the same
  function that judges every commit on main's push; only it is told GitHub's signature, which it cannot have, and
  main's push is never told one. Nothing of the pull request runs: git only merges trees. When the two do not merge
  cleanly the run fails with a fixed message.
- **Pull requests from forks** are judged in the workflow `pull-requests` by main's copy. Nothing of the pull request
  runs there: its commits are fetched into a ref and read as data, and its paths are read from GitHub's answer as
  data.
  - `pull-request-paths` fails at once, with no secret and no approval, when the pull request touches a path closed
    to outside pull requests (below).
  - `pull-request-way` names no environment and no secret. It reads main's settings and, when they say the way for
    forks is open, asks GitHub whether the environment `private-check` names a reviewer. While the way is shut,
    nobody is named, or it cannot tell, it fails with a fixed message.
  - `pull-request-commits` needs `pull-request-way` and runs only when it said "open". It runs in the environment
    `private-check`, waits for the project's approval, asks once more for the named reviewer before any step that
    names a secret, and names only the environment's own secret `FORK_CHECK_PRIVATE_PATTERN`.
  - A pull request from the project's own branches runs no code there: one job prints that the workflow `commits`
    judges it.

The paths closed to outside pull requests (the same list as the desk's): `bench/dodambench/`, `bench/images/`,
`.github/`, `bench/tests/`, `bench/tasks/`, `bench/experiments/`, `bench/harbor/`, `bench/records/`, `tools/desk/`,
`tools/gemini/`, `tools/site/` and the rest of `bench/`. Allowed in them: one language's text of the introduction
page, a `.json` file directly in `tools/site/strings/`; a new test, a `test_<name>.py` directly in `bench/tests/`;
and a new benchmark task, a new folder in `bench/tasks/`.

## The way for forks is not open yet

Until `fork_way_open` in `settings.json` is `true` and `outside_runs_held_since` holds a date, a fork's
`pull-request-way` says the way is not open and fails, and `pull-request-commits` never starts: no environment is
made and no secret reaches a runner. The way is opened by a small pull request of its own, after the owner has made
the environment `private-check` with the project's account as its required reviewer and "Prevent self-review" off,
given that environment its own secret `FORK_CHECK_PRIVATE_PATTERN` (and the repository none of that name), and
turned on GitHub's setting that holds the workflow runs of every outside contributor until the project approves
them. That pull request sets `fork_way_open` and writes the date of the last step.

Every run of the project's own branches and pull requests and of main's push asks whether a secret of the fork's
name stands at the repository's level, as the result of a comparison, never its value, and fails if it does. Once
the way is open, they also ask what a job's token can read: that the environment names a reviewer, and that the
date is there. A job's token cannot read GitHub's setting for outside contributors (GitHub answers 403): for that
step the date is the owner's word.

## The private pattern

The owner sets the repository's secret `COMMIT_CHECK_PRIVATE_PATTERN`. It is applied from the moment it has a
value; `private_pattern_expected` in `settings.json`, turned to `true` by a small pull request afterwards, only makes
a missing secret fail.

How it is written: as a regular expression in plain letters, in either case; it is matched ignoring letter case,
on the text as written and on its copy. The run stops with a fixed message, showing nothing of the secret, when the
pattern holds a character that is not its own copy but for letter case (an accent or another mark; a format,
control, surrogate, private-use or unassigned character; one of the four letters named FILLER; a space other than
the plain one; a dash or minus sign other than the plain hyphen; a character with a compatibility form, such as a
full-width letter or a ligature), a letter that folding turns into something other than its small letter (such as
the German sharp s), two spaces in a row, an escape that names a character by its number or its name or names a
control character, or an inline flag that narrows how it matches.

What it must not match, beyond what the rule for names spares: the account's displayed name, which GitHub writes
as the author of every merge commit, and this repository's owner's name, which GitHub writes in the first line of
every merge message. Every pull request's run judges the merge it will make and fails first if either matches.

## When main turns red

Nothing merges while main's newest commit is red. For every way this check can end main's push run failed, exactly
one of three holds:

- (i) the pull request's own run fails for the same reason before the merge;
- (ii) the check names the failure by its fixed message as GitHub's API not answering, or git not fetching, after
  all its tries: the coordinating session runs the failed job again once, under the project's rule (on main's push
  run only, once for that commit in all, after the failed job, the test and the message are written down and the
  first attempt's log is kept). A second failure is reverted. This holds also for a merge that changes the check: a
  change that broke a way only main walks fails the second time too and is reverted, so no broken check stays on
  main. Any other failure the merged change could have made is not run again;
- (iii) only the owner's hand brings main back: the coordinating session opens a pull request that says what
  happened, its checks pass because they look only at the commits after the red one, and the owner merges it by
  hand. What the red commit wrote stays in the history.

| What fails on main | Class | Who acts, and how |
| --- | --- | --- |
| a commit, a line, a path, a title or a branch name that breaks a rule | (i) | the pull request's run refuses it first |
| the merge commit: its paths, its lines, its author's name, its message (the repository's owner and a fork's owner in its first line alike) | (i) | the pull request's run builds that merge and judges it with main's function |
| a rule main gained since the pull request's last green run | (i) | a branch that does not change the check is judged by main's copy |
| the settings loosened, or the way opened without what it needs | (i) | the branch's own runs ask the same |
| the check's own tests | (i) | the same tests run first in every run of the pull request |
| GitHub's API did not answer (each thing asked up to five times, waits of 2, 5, 10 and 20 seconds, each try at most 10 seconds, and 90 seconds in all for every try of the run, so the job ends well inside its limit with the check's own message) | (ii) | the coordinating session, once |
| git could not fetch or read the history | (ii) | the coordinating session, once |
| main moved on between the pull request's last run and its merge, and the merge then differs from the one the run built | (iii) | the owner's hand; the working rules merge only on a run newer than main's last change of the check |
| the project's secret missing while `settings.json` expects it, a secret written in a form the copy can never meet, the secrets or the displayed name changed after the last green run, or a setting of the way changed after it opened | (iii) | a change the owner made; the owner puts it back, then the coordinating session runs the job once |
| a merge commit with another address than the merging account's no-reply one, or a merge message written by hand | (iii) | the owner's hand |
| a squash or rebase merge (the repository is asked to allow merge commits only): such a commit has another author, message and shape than the merge the run built | (iii) | the owner's hand |
| a pull request merged after main's check changed since its green run, without taking main in | (iii) | the owner's hand; the working rules say it is not done |

Running again changes nothing for a fault in the merge commit's own text, name or address, and a reverting commit
takes nothing out of the history.

## When main's copy is broken

A broken copy of the check's script on main cannot block the pull request that mends it, which is judged by its own
copy. A broken `pull-requests.yml` on main is different: GitHub runs main's copy of that file for every pull request,
the project's own too, so a failure of its job for the project's own branches would stand on their heads as well,
the mend's included. Then the owner merges the mend by hand.

## When a text breaks a rule

The run fails at once. The way back is a new head: mend the title, the description or the branch first; then, on a
branch of the project, push an empty commit whose message says why it is there (never an amended commit, never a
forced push); on a fork, any new commit. To check a text before it is published:

    python3 -I .github/commit-check/check.py --text-only --settings .github/commit-check/settings.json \
        --context own --event text.json

where `text.json` holds `{"pull_request": {"number": N, "title": "...", "body": "...", "head": {"ref": "<branch>",
"repo": {"owner": {"login": "<owner>"}}}}}`; the merge message is then checked as GitHub will write it.

## Tests

`python3 -I .github/commit-check/run_tests.py` runs the tests of the check, of the check of a fork's paths and of
both workflows, in four processes, and fails when one fails or when not every test it found ran. A script beside
them breaks the check by machine and runs the tests against every break; it runs on a side branch.
