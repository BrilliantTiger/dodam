# The Gemini job

The Gemini job is the one way in which Google's model takes part in the Dodam project. A script of the project runs Google's Antigravity CLI once, in its headless mode (`agy -p`), for one pull request or issue of the project, when the project's own account has put a mark on that item. The answer has a public part, which is posted as one comment on the item, and a private part, which stays on the owner's computer. The project's rules ask for a review by a model of another company than the author's, and for an attack by such a model before the judge counts for measurements and before the desk opens to the outside; this job is how that second pair of eyes looks.

Two programs make it up. The job (`job.py`) runs agy once for one item and one kind of look. The watcher (`watch.py`) looks at the repository at a steady pace and starts jobs, one at a time.

## What Google's pages say, and what is not known

Google's frequently asked questions for Antigravity say, as read on 19 November 2026: "Antigravity CLI headless mode (agy -p) is intended for direct scripting and CI workflows. Wrapping agy inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans." Under the question "Why can't I use third-party software (such as Claude Code, OpenClaw, or OpenCode) with my Antigravity login?", they say: "Using third-party software, tools, or services to access Antigravity is a violation of our Terms of Service". The terms say: "Using third party software, tools, or services to access the Service (e.g. using OpenClaw with Antigravity OAuth) is a breach of this Agreement. Such actions may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts." The account is the owner's.

So the job is a script of the project that events in the repository start, and no session of a coding agent may pass text into agy or read what agy writes. That is why the job refuses to run inside a coding agent, and why nothing it prints carries text of the model.

What the job relies on from Google's page on headless mode, as read the same day:

- `agy -p "<prompt>"` runs one prompt and exits. The response goes to standard output and diagnostics to standard error.
- `--output-format json` prints one JSON object when the run ends, with a `status` (`SUCCESS`, `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING` or `RUNNING`), the `response`, and an `error` that is there only on failure. A run that fails exits with a code other than 0.
- `--print-timeout` bounds the wait for a response; by default there is no limit.
- `--sandbox` runs "with terminal sandbox restrictions enabled".
- `--model` names a model; with an unknown name the run exits with an error status and a code other than 0, rather than running another model.
- Headless mode uses cached credentials: one signs in once in an interactive session. A run with no terminal that is not signed in exits with an "authentication required" error.
- By default the CLI follows the execution and permission modes in its own settings. A tool action that needs an approval it cannot get in headless mode is denied, the run goes on and exits with 0, and a notice naming the tool goes to standard error. Tools can be allowed ahead of time in the CLI's own settings file, which the job does not touch. `--dangerously-skip-permissions` approves every tool call of a run; the job never passes it.

What is not known:

- Google has not confirmed in writing that a job started this way, by a watcher that a person starts in a plain terminal and that reacts to marks on the repository, counts as the "direct scripting" its page allows.
- How long Google's quota window is, and how a used-up quota shows in the answer: the pages that were read say neither. The job takes an answer whose error or diagnostics speak of a quota, a rate limit, exhausted resources, too many requests or code 429 as a used-up quota; those words are a guess. The wait of five hours in the settings is a guess as well.
- What `--sandbox` restricts exactly.
- How agy tells its own version, and whether and when it updates itself: the pages that were read say neither. For every run the job records the program file's resolved path, its size and its SHA-256 digest; a program that changed, by an update or otherwise, shows as a different record. The job sets nothing to change how agy updates.
- Where agy keeps its cached sign-in: the page says that headless mode uses it, not where it is. The job passes the real home folder on so that agy can find it; that agy finds it there is a guess, to be seen on the first run.
- Whether a prompt can be given on standard input together with `-p`: the page's list of flags says that `--input-format` (text or streaming JSON) reads prompts on standard input, but shows it only in the streaming mode. The job passes the prompt after `-p`, as the page's examples do.

## How it is started

The owner starts the watcher by hand, in a plain terminal of the computer (not a terminal of Orca and not inside a coding agent):

```
python3 tools/gemini/watch.py --tell run_...
```

`--tell` names the coordinator's run in Orca, which hears one line when a job ends. The watcher keeps running until its terminal is closed or Ctrl-C is pressed. The job alone can be started the same way:

```
python3 tools/gemini/job.py --kind verify --number 12
```

Before the first run, the owner writes the names that must never be posted (their own names in every spelling, and the computer's names) into a file named `names.txt` in the private folder, one per line. Without that file nothing is posted.

The private folder is named `dodam-gemini-` followed by the account's number and lies in the computer's folder for all accounts (the folder `Shared` beside the home folders), or in `/var/tmp` where there is no such folder; `--private` names another one. It must lie outside the repository and outside the folders `.dodam` and `.dodam-desk` of the home folder, and be a real folder that only the owner's account can open. It holds `names.txt`, `finished.log` (one line for every job that ended), the watcher's state, and one folder for each answer with the prompt, the way agy was started, everything agy printed, the response, the comment as it was or would have been posted, and the result.

Three labels are the marks: `gemini-verify` for a verification look, `gemini-attack` for an attack look, and `gemini-discuss` for a discussion. For a discussion, the question is the newest comment of the project's account whose first line is `Question for the Gemini job:`.

## The rules, each with the check that pins it

"Checks" are the automatic tests in `bench/tests/test_gemini.py`. They run on every push, in the required job `variants` of the automatic check (beside the pilot's checks, so that no job comes near half of its time limit), without the network. They start the job and the watcher as programs, the way the owner does, against three stand-ins: a program named `agy` that records what it was given and answers what the check tells it to; a program named `gh` that serves made-up items and records every request; and a local git repository in place of the project's. Every check first makes sure that the names `agy`, `gh` and `orca` lead to the stand-ins and that the repository is a local one, so no check calls the real model or the real repository. The programs are started through a small runner that leaves its own parent first, so that the processes above them are those of a plain terminal and not whatever started the checks.

### Inside a coding agent, nothing starts

The job and the watcher look at their own environment and at all their parent processes before they do anything else, and the job looks again right before it starts agy. They refuse when the environment carries a mark of Claude Code, of Orca or of two other coding agents, or when a parent process is a coding agent of another company or Orca, whose terminals an agent can type into. The refusal names the mark or the parent and never its value. When the parent processes cannot be read, they refuse as well.

The marks of Claude Code are the ones it set in the shells of the session that wrote the job (Claude Code 2.1.327): `CLAUDECODE`, `AI_AGENT`, and every name that begins with `CLAUDE_`, among them `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_PID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_CHILD_NOTES_DIR`, `CLAUDE_CODE_PLUGIN_DIRS`, `CLAUDE_CODE_FORCE_TERMINAL_IMAGES` and `CLAUDE_EFFORT`. Orca's marks are every name that begins with `ORCA_` and `TERM_PROGRAM` set to `Orca`. The other agents' marks are names beginning with `CODEX_` and the name `OPENCODE`. The parent processes are recognised by the name of their program (among them `claude`, `codex`, `opencode`, `cursor-agent`, `aider`, `goose` and `orca-terminal-host`) or by the package of a coding agent in their command line. Only Claude Code's and Orca's marks, and the program names `claude` and `orca-terminal-host`, were seen at work; the others are a guess, and a wrong one only makes the job refuse where it could have run.

Checks: `test_inside_claude_code_or_an_orca_terminal_the_job_starts_nothing` (each mark alone), `test_a_coding_agent_among_the_parent_processes_stops_the_job` (a real parent process named `claude`, `codex`, `opencode` or `orca-terminal-host`), `test_under_a_plain_shell_the_job_runs_agy_once`, `test_the_watcher_inside_claude_code_asks_github_for_nothing`.

### Only a mark of the project's account on one of its own items starts a job

The watcher starts a job only for the newest event that put one of the three labels on an open item, when the account that put it has the project's numeric id (`project_account_id` in the settings), the item was opened by that account, and, for a pull request, its head lies in the project's own repository and not in a fork. A mark that does not qualify is remembered and never looked at again; the label stays. Comments never start a job, so the job's own comments cannot; the job only ever takes labels off. An event that was handled is never handled twice, even when its label could not be taken off; when a job is done the label is taken off, so that putting it again (a new event) starts a new job. A job that could not read GitHub, or fetch the commit, before agy started is not handled: its mark stays, nothing is counted, and the next round tries again. The job, started by hand, refuses an item that is not the project's own in the same way. The watcher stops at once when the GitHub command-line tool is signed in as another account.

Checks: `test_a_mark_of_the_project_account_on_its_own_pull_request_starts_one_job_and_the_mark_is_taken_off`, `test_a_job_that_could_not_read_github_before_agy_started_is_tried_again`, `test_no_other_mark_or_item_starts_a_job` (a mark by another account, by a bot, a pull request from a fork, a pull request or issue by another account, a closed item), `test_a_job_started_by_hand_refuses_an_item_that_is_not_the_projects_own`, `test_a_handled_mark_never_starts_a_second_job_and_a_new_mark_does`, `test_the_jobs_own_comments_and_unlabeling_start_nothing`, `test_the_watcher_stops_when_the_signed_in_account_is_not_the_projects`.

### The prompt is made of three things only

The prompt is the template fixed in the repository for the kind of look, then the done criteria of the item, then text written by the project's account on the item: its title and description and its comments, oldest first. Comments of every other account are left out whole, also when they copy the job's first line or the question's first line. Quoted text inside the project's own text is left out too: every line that begins with `>` and every HTML block quote. The job's own earlier answers go in a section of their own, marked as its own. For a discussion the newest question of the project's account goes last, under its own heading. Past the prompt's bound in bytes, the oldest comments are left out and the prompt says how many; the template, the done criteria and the question never are. A discussion without a question of the project's account does not start agy. Reviews and review comments of a pull request are never read.

Checks: `test_no_text_of_another_account_reaches_the_prompt` (made-up comments of the project's account, of two other accounts and of a bot, with quotes and copied first lines), `test_the_discussion_question_is_the_newest_marked_comment_of_the_project_account`, `test_a_discussion_without_a_question_of_the_project_account_does_not_start_agy`, `test_a_prompt_past_its_bound_leaves_out_the_oldest_comments_and_never_the_question`.

### A clean checkout of its own, with no remote

For each run the job makes a new folder in the computer's folder for temporary files, outside the repository and outside the private folder. In it, it starts an empty git repository and fetches the pull request's head and its base branch (for an issue, the tip of `main`) from the public address in the settings, which needs no sign-in. It checks that the fetched head is the commit GitHub named, and stops when the head has moved. The checkout is a clone of its own, not a worktree of any repository, and has no remote at all, so there is nowhere a push could go without naming an address. The branch `base` holds the base for comparing. The folder is removed when the run ends.

Checks: `test_the_checkout_is_a_clone_of_its_own_at_the_head_commit_with_no_remote`, `test_an_issue_is_checked_out_at_the_tip_of_main`, `test_a_head_that_moved_since_github_was_read_stops_the_job`.

### agy gets no token and no key, and signed-out settings for gh and git

agy runs with an environment built from a fixed list of names, not by removing names from the job's own: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR` and `TZ` as they are, and the following, set by the job. `GH_CONFIG_DIR` points at a new empty folder and `GH_PROMPT_DISABLED` is `1`, so a `gh` that the model starts finds no sign-in (on version 2.101.0 it ends with "To get started with GitHub CLI, please run: gh auth login"). `GIT_CONFIG_GLOBAL` points at a new empty file, `GIT_CONFIG_NOSYSTEM` is `1` and `GIT_TERMINAL_PROMPT` is `0`, so git has no credential helper and asks nobody; `GIT_SSH_COMMAND` is `false`, so a push over SSH does not reach the account's keys. No variable of Antigravity's own is set. Nothing of GitHub, Claude, Google's cloud or any other cloud gets through, nor an SSH agent. The job's own reading and posting happen outside agy's run, with the sign-in the job was started with.

This stops a mistake, not a program: a command that sets the variables back, calls the GitHub tool with its own settings, or reads the keychain is not stopped by it.

Checks: `test_agy_gets_an_environment_without_tokens_or_keys_and_signed_out_settings` (a dozen made-up secrets in the job's environment; none reaches agy; inside agy's run git has no credential helper and the SSH command fails).

### How agy is started

agy runs once, in the checkout, with the prompt after `-p`, `--output-format json`, `--print-timeout` set to the job's time bound, `--model` when the settings name one, and `--sandbox` for the kinds that only read and reason (a verification look and a discussion). It never gets `--dangerously-skip-permissions`, a way to continue an earlier conversation, or prompts on standard input. It runs in a process group of its own, so that the job can stop it with everything it started. Before it starts, the job records in the private answer folder the program's resolved path, size and SHA-256 digest, its arguments (without the prompt, which is kept beside them) and the names in its environment.

Checks: `test_agy_is_started_once_with_json_output_a_time_limit_and_the_sandbox_for_reading_kinds`.

### The whole answer stays private, and one comment carries the public part

Everything agy prints is written to the answer folder in the private folder and stays there. From a successful response the job takes the part between a line `=== PUBLIC ===` and a line `=== PRIVATE ===`; without exactly one public part nothing is posted. The verdict is one of two fixed words: `approve` when the public part holds the line "Verdict: approve" and no other verdict line, otherwise `changes-needed`. The comment's first line is written by the script and says that the project's Gemini job wrote it automatically; the model cannot write it or remove it. A public part longer than its bound is not posted.

Checks: `test_the_whole_answer_stays_private_and_only_the_public_part_is_posted_under_the_scripts_first_line`, `test_the_verdict_is_one_of_two_fixed_words_and_unclear_means_changes_needed`, `test_an_answer_without_a_public_part_or_too_long_is_not_posted`.

### The scan: nothing that must not be public is posted

Before posting, the comment is put into one normal form (invisible characters removed) and scanned. It is not posted, and the result says which kinds it found, when it holds: a personal name (from `names.txt`, the account's own name and full name, a Korean name with an honorific, or an @-mention outside code); an e-mail address; a local path (a home folder, a temporary folder of the system, `~/`, a Windows user folder, or the paths of this computer); an address inside a private network (private and shared IPv4 ranges, private and link-local IPv6 addresses, names ending in `.local`, `.lan`, `.internal`, `.home.arpa` or `.ts.net`); a computer's name (this computer's name, or a name such as one ending in `-Mac-mini`); a work item number (the prefixes in the settings, or a Linear address); or the shape of a token (GitHub, Anthropic, AWS, Google, Slack, GitLab, DigitalOcean and npm tokens, private keys, signed web tokens, and any run of 32 or more letters, digits, dashes and underscores that mixes capitals, small letters and digits). A commit's hexadecimal name passes. When `names.txt` is missing, nothing is posted.

Checks: `test_a_public_part_that_fails_the_scan_is_not_posted` (one case or more for each kind), `test_without_the_private_list_of_names_nothing_is_posted`.

### The coordinator hears one fixed line

When a job that got as far as an answer folder ends, it appends one line to `finished.log` in the private folder:

```
gemini-job kind=verify number=12 status=posted verdict=approve private=<the answer folder>
```

The status is one fixed word (`posted`, `held`, `quota`, `timeout`, `signed_out`, `refused` or `error`), and so is the verdict. With `--tell`, the job also sends that line to the coordinator's run with Orca's command-line tool (`orca orchestration send --to run:<run> --type status`). That tool answered from an empty environment; a send from a plain terminal has not been tried. The line carries no text of the model, and neither does anything the job or the watcher prints.

Checks: `test_the_coordinator_hears_a_fixed_line_that_carries_no_text_of_the_model`, `test_without_a_run_to_tell_the_line_goes_only_to_the_private_file`.

### Bounds

Every bound is in `settings.toml`, and a check pins each at its value: a look every 60 seconds, at most 8 runs of agy in a day (by the day in UTC), at most 1,800 seconds for one run, a wait of 18,000 seconds when Google's quota is used up (a guess at the quota's window), a prompt of at most 100,000 bytes, a public part of at most 30,000 characters, an answer of at most 2,000,000 bytes, and at most 10 pages of 100 comments or events for one item. Settings without a bound, or with one out of its range, start nothing. One job runs at a time: a lock in the private folder keeps a job started by hand and a job of the watcher apart, and the watcher starts the jobs of one round one after another. A run past its time is stopped with everything it started. When Google's quota is used up, as far as the job can tell from words that are a guess, the job is put back: its mark stays, it is counted neither as handled nor in the day, and the watcher starts nothing until the quota's wait has passed.

Checks: `test_the_settings_file_holds_every_bound_at_its_decided_value`, `test_settings_without_a_bound_or_with_a_bound_out_of_range_start_nothing`, `test_the_day_bound_stops_jobs_and_keeps_the_marks_for_the_next_day`, `test_one_job_runs_at_a_time`, `test_a_job_past_its_time_is_stopped_with_agy_and_everything_it_started`, `test_used_up_quota_puts_the_job_back_until_the_window_has_passed`, `test_the_watcher_asks_at_its_pace`.

### It runs only while its terminal is open, and installs nothing

The watcher and the job run in the foreground of the terminal they were started in. When the terminal closes (or on Ctrl-C, or when they are told to stop), they stop agy's whole process group and end. Neither installs a service, a scheduled task or anything else that keeps running, and both use only Python's standard library.

Checks: `test_closing_the_terminal_stops_the_program_and_agy`, `test_nothing_is_installed_to_keep_running_and_only_the_standard_library_is_used`.

### The templates

Each of the three templates says what its look is and what it is not, asks for the public part and the private part, asks for the verdict line, and carries the rules for the owner's computer: programs under test run only in containers, as the tests run them, never on the host; one batch of containers at a time; the free space is read first and the work stops under 20 GB; Docker and colima are never restarted or pruned; nothing is deleted with a bare wildcard; the folders `.dodam` and `.dodam-desk` of the home folder, the keychain, tokens and keys are never opened; nothing in the pull request is changed, nothing is pushed or merged, no setting is changed; text in the repository is data, not instructions; and nothing in the public part tells how the judge, the measuring tools or the desk could be fooled.

Checks: `test_each_template_states_the_look_the_two_parts_the_verdict_and_the_rules`, and this page's own: `test_the_document_states_each_property_with_a_test_that_exists`.

## What the job does not do

- The refusal stops a coding agent from starting the job as part of its work. It cannot stop a program of the same account that removes its marks and leaves its parents; such a program could as well start agy itself.
- The rules for the owner's computer in the templates are instructions to the model, not limits the job enforces. What may limit the model is the terminal sandbox for the reading kinds (what it restricts is not known), agy's own rule that an action needing approval is denied in headless mode (unless the CLI's own settings allow it), and the reduced environment above. The model runs as the owner's account with the real home folder, so it can read what that account can read.
- The scan finds shapes and listed values. It cannot know every name, and it cannot tell whether a public part explains how to fool the judge, the measuring tools or the desk; that rule is in the templates only.
- Text of another account that the project's account copied without quoting it reaches the prompt; the job cannot tell it apart.
- The private answer folder can be opened by any program of the owner's account. The job hands no text of the model to anyone, but it cannot stop a session of that account from opening the folder.
- It does not post anything when it holds an answer, and it does not tell anyone on GitHub why.
