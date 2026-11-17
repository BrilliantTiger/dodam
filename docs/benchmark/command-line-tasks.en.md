# Command-line program tasks — format and judging

The tasks of the Dodam benchmark are not web services only. A program that runs once and ends, such as a log analyzer, a CSV/JSON converter or a configuration-file checker, is a task too. This document says how such a task is written, how the judge judges it, and which rules that judging keeps. Every number was measured on 17 November 2026 on this repository's measuring machine (Apple M4, Docker on colima). The Korean version of this document is the source; this is its translation.

## At a glance

- The judge gives the program **arguments, standard input and input files**, and looks only from outside at its **standard output, its exit code and the files it leaves**. It never looks inside the program.
- One set of tests judges all seven languages (Python, TypeScript, Java, C#, Go, Rust, C++).
- Hidden tests are functional, boundary or security tests and are reported separately. A task has a build step and change steps, and a change step runs the earlier steps' tests again and counts what broke. This is the same as for web tasks.
- The program starts in a fresh working folder for every test (every case). Time, memory, output size, number of processes and file size are limited. A limit is a line that cannot be passed whatever the program does; a case that tried either fails, alone, or could not take the thing in the first place.
- The build is treated exactly as the program is. The tests are closed before the build starts.
- The sample task is a converter from CSV to JSON. Its 70 prepared solutions (for each language 2 right ones, 6 vulnerable ones and 2 broken ones) all got the judgement they were written for, in all seven languages.

## The task folder

A command-line task is a folder of the same shape as a web task. Only two places in `task.toml` differ: `interface` is `"cli"`, and in place of the server settings there is `[cli]`, the limits of one case.

```toml
schema = 1
id = "cli-csv-json"
title = "CSV to JSON converter"
domain = "cli"
kind = "new"
interface = "cli"
visibility = "public"
canary = "dodam-bench-canary-…"
languages = ["python", "typescript", "java", "csharp", "go", "rust", "cpp"]

[cli]                    # the limits of one case (one hidden test)
case_timeout_sec = 5     # the time of every run of the program in that case, added up
case_memory_mb = 512     # the memory all processes of the program really use, added up
case_output_kb = 4096    # standard output and standard error together
case_processes = 64      # processes; a thread counts as one
case_disk_mb = 64        # what can be written in the case's whole folder, and the size of one file

[limits]
agent_timeout_sec = 3600
verifier_timeout_sec = 1800
cpus = 2
memory_mb = 1536         # at least the case's memory + its files + 256 for the judge

[[steps]]
name = "build"
instruction = "steps/build/instruction.md"

[[steps]]
name = "change-1"
instruction = "steps/change-1/instruction.md"
```

The rest is as in a web task.

| Place | What it holds | Who sees it |
|---|---|---|
| `steps/<step>/instruction.md` | the requirements of that step | the AI |
| `tests/manifest.toml` | for every test file its kind (functional, boundary, security), its step and, for a security test, the name of the weakness | the judge only |
| `tests/functional`, `tests/boundary`, `tests/security` | the hidden tests | the judge only |
| `solutions/secure/<language>/<step>` | the right solution | only the check of the judge |
| `solutions/variants/<language>.toml` | the vulnerable and the broken solutions, written as exact replacements of text in the right solution | only the check of the judge |

Together with the requirements the AI is told: the language and where the sources go, the packages it may use, that in `/app` the build command runs once and then, for every case, the run command followed by the case's arguments, that every case starts in a fresh working folder holding the case's input files, as a plain user without network, and the values of the five limits. It is not told the hidden tests, the expected answers or the prepared solutions.

The run command of each language is below. The build commands are those of web tasks.

| Language | Run command |
|---|---|
| Python | `python3 /app/main.py` |
| TypeScript | `node /app/dist/main.js` |
| Java | `java -jar /app/target/app.jar` |
| C# | `dotnet /app/out/App.dll` |
| Go | `/app/app` |
| Rust | `/app/target/release/app` |
| C++ | `/app/build/app` |

## How a hidden test is written

A test never starts the program itself. It runs it only through `case` of `dodam_cli`, which the judge provides. One test function is one case.

```python
from dodam_cli import case, need

def test_a_file_becomes_a_json_file():
    with case(files={"people.csv": "name,city\nAnn,Seoul\n"}) as c:
        r = c.run(["people.csv", "people.json"])          # arguments; they pass through no shell
        assert r.exit_code == 0 and r.out == ""            # exit code and standard output
        assert json.loads(c.read("people.json")) == [{"name": "Ann", "city": "Seoul"}]
        assert c.files() == ["people.csv", "people.json"]  # the files it left

def test_an_input_reached_with_dots_is_not_read():
    with case(outside={"secret.csv": "name,token\nroot,outside-secret\n"}) as c:
        need(converts(c), "the plain conversion")          # without the feature this security test is left unjudged
        r = c.run(["../outside/secret.csv", "-"])
        assert "outside-secret" not in r.out and r.exit_code == 3
```

What `case` gives:

- One folder for the case. In it are the working folder `work` where the program starts, a neighbour folder `outside` that the program reaches only by leaving its working folder, and a home folder and a temporary folder of that case alone.
- Putting things in: `files=` and `write` (files in the working folder), `outside=` and `write_outside` (files in the neighbour folder), `mkdir`, `symlink`, and `sparse` (a large file that takes no disk space).
- Running: `run(list of arguments, stdin=…)`. The result has `exit_code`, `stdout`, `stderr` and `limit`, the name of the limit that was broken. When the program did not end by itself there is no `exit_code`. A case that broke a limit fails whatever its test asserts, and also when `need` was about to leave it unjudged.
- Looking: `read` (the content of a file), `files` (what the working folder holds), `read_outside`, `files_outside`. A symbolic link the program left is never followed.

The format checker refuses a test of a command-line task that starts the program any other way (`subprocess`, `os.system` and the like), that tries to learn the language (by reading the judge's settings), or that multiplies one test into several (`parametrize`).

## What the judge does

"The program's side" is everything the AI's work can make run: not the program alone, but also the build and whatever the build starts. The separation below holds from the first moment anything of the program's side is executed.

1. It looks whether its container can bear the judging. Without a memory cap, with a memory cap above the memory of the machine the container runs on, without a cap on processes, or without the size-capped folder the cases live in, it runs nothing and refuses.
2. It closes the tests folder and the results folder to everyone but root, and makes sure no other user can rename or replace them from the folder above. This comes before the build.
3. It builds `/app`. The build runs as the plain user `agent`, confined as the program is: it cannot gain rights, the kernel ends it first when the container's memory runs out, and it cannot make anything in the kernel that outlives it. Of what the build prints only the end is kept. When the build ends, every process of that user is ended.
4. It makes `/app` readable and runnable by everyone and changeable by nobody but its owner, and removes every mark that lends a file's owner's rights to whoever runs it. It takes write access away from every place in the container that any user could write (`/tmp`, `/var/tmp`, `/dev/shm` and others).
5. It runs the test files one by one. For every case a test runs the program as **a user made for this judging alone**. The user's number is drawn at random from a wide range, so it is no user of the image. It owns no file, is in no group, and gains nothing by running a program that grants rights (such as `su`). The only place it can write is the folder of its case, and that folder lives in a memory-backed folder whose size the container caps.
6. After every run it ends all processes of that user at once, and after every case it removes the case's folder. Removing takes one step for each entry however deep the folders go, whatever the names are, and whether there are closed folders, links or special files; it follows no link.
7. It writes the pass rates of the functional, boundary and security tests, the number of tests it could not judge, the number of weaknesses found, the number of earlier steps' tests a change step broke, and whether the program ran separated from the judge, into the result file. Only the judge writes the result file.

Nothing about the judging is on the command line of any process, because every user of the container can read command lines. Which test file is run, where its report goes and which step it is travel in the environment, which only root can read. The judge of web tasks is the same code, so step 2 and this paragraph hold for web tasks as well, and a web task's build and served program run under the same confinement.

What a plain user can still see is that the judge's processes exist, the name of their program (`python3`), when they started and how much processor time they have used. None of that says which test is being run.

## A check that could not be judged is no pass

A security test is left "unjudged" when the feature it attacks does not work, because nothing can be said about the security of a feature that is not there. So that this never reads as a pass, it is reported as follows.

- The pass rate of each kind is the number passed divided by **all that were to be run**. An unjudged check lowers the rate just as a failed one does. With one security check unjudged and no other, the security pass rate is 0.
- The number of unjudged checks is reported on its own. With even one, the whole reward is 0.
- A case in which the program broke a limit fails even when its test was about to end as unjudged. A program that hangs in the feature a security test checks first is recorded as failed, as one weakness, not as unjudged.

## Judging without separation

Run on a developer machine without an image, the program runs as the same user as the judge. Nothing stands between them, so this is only for quickly checking **the project's own prepared solutions**.

- It must be asked for (`--unseparated`). Without the request both the judge and the tool that checks prepared solutions refuse with a JSON diagnostic.
- The result file says `"separated": 0`, and every result of the checking tool says `"separated": false`. A result judged with separation says 1 and `true`.
- The judge refuses when it is root, since the program would then run as root.
- Nothing an AI or an outside party wrote is judged this way. In this mode only the limits on time, output and the size of one file and the check for processes left behind hold.

## The rules it keeps

Each rule has a test that fails when the rule is broken.

| Rule | How it is kept |
|---|---|
| The AI never sees the hidden tests, the expected answers or the prepared solutions | The tests are not in the container the AI works in, and judging happens in a separate container without network. A test checks that what goes to the AI carries no test marker, no test name and no solution name |
| The program's side can neither read the tests, the expected answers or the judge's files, nor list their folders or pass through them | Before the build, the tests folder and the results folder are closed to everyone but root. A build that tries to copy the whole tests folder takes not one file |
| The program's side cannot learn which test is being run, or anything else about the judging | Nothing is on a command line, the program's environment carries nothing of the judge's, and the environment, working folder and open files of the judge's processes cannot be read |
| No network while judging | The judging container is made without network. A program that tries to connect outside or to look up a name fails |
| The program runs as an ordinary user who cannot become root | A user for this judging alone, in no group, with the kernel's mark that it can gain no rights. `setuid(0)`, `su` and a rights-lending file that the build prepared all fail |
| The program cannot write, replace or remove the result file | Only root can enter the results folder, and nobody else can rename it from the folder above. The judge writes the result to a new file and renames it into place, so it never follows a planted link |
| A limit is a bound that holds whatever the program does | The section "The limits" says for each resource what enforces it and how far it can really be passed |
| What one case takes is given back before the next case starts | Every process is ended and the case's folder removed. Shared memory and message queues that would stay after their processes cannot be made in the first place |
| The judge survives whatever the program leaves behind | Removing what was left is bounded in time, memory and open files for any tree of folders. If it cannot be removed, that case fails; it is no fault of the judge, and the other cases are judged as usual |
| What could not be judged never counts as passed | The three points of the section "A check that could not be judged is no pass" |
| A fault of the judge itself is told apart from a failure of the program | On a fault the judge writes no result file, records the fault separately and ends with a different exit code |
| Cases do not affect each other | A fresh folder for every case. Of the files, processes and kernel objects an earlier case tried to leave, the next case can see none |
| One set of tests judges every language | The tests folder that goes into the judging container differs between languages in one file only, the one that names the commands. The environment the tests get is the same for every language |
| Every container has caps | Every container that the checking tool, the tests and the experiment start is capped in memory (with no swap beyond it), processors and processes. The same goes for web tasks |
| The format checker accepts the sample and refuses a malformed command-line task with a JSON diagnostic | The checker prints each problem as JSON with its file, key and message first; the lines a person reads are made from that JSON |

## The limits

A limit is a bound, not a measurement. For each resource there is a line that cannot be passed whatever the program does, and the kernel or the container holds it.

| Resource | The line that cannot be passed | What holds it | A case that broke the limit |
|---|---|---|---|
| Time | the case's time limit plus the time it takes to end the processes | when the time is up the judge ends every process of that user | fails |
| Output | the output limit plus what one pipe holds | the judge keeps what fits in the limit, reads no more and ends the program | fails |
| Processes and threads | the limit itself | the kernel refuses that user a process or thread beyond the limit; the user is this judging's alone | cannot be taken |
| Files | the limit itself, and 20,000 entries | the container caps the memory-backed folder the case's folder lives in at that size. Many small files, deep folders, files deleted while open, the home and temporary folders are all inside it, so together they come to that size. One file cannot be made larger than the limit, not even a sparse one (the kernel) | cannot be taken. A program the kernel ended for making one file too large fails |
| Memory | the container's memory cap | every 0.002 seconds the judge measures the memory of the whole container and ends the program above the limit, and also, whatever the limit, when the container has less than 128 MB left. What gets past it in between is stopped by the kernel at the container's cap, where the kernel ends the program first | fails; so does a case whose program the kernel ended |

**What counts as memory.** Not the memory of the processes alone but everything the kernel counts for the container: the memory of all processes, memory files that belong to no process, pipes and what the kernel keeps for them. The cache of files on disk and the files in the case's folder are taken out (the case's files count against the file limit). What was there when the run began is subtracted; the rest is the case's memory.

**What cannot be made.** Memory that stays when its processes have ended, which is System V shared memory, message queues and semaphores, and POSIX message queues: the kernel refuses the calls themselves. The folder for POSIX shared memory is closed to writing. So once the program's processes are ended, nothing of its memory is left but the files in the case's folder.

**How far memory can really be passed.** Memory alone can be passed between two measurements. The most it can be passed by is the container's memory cap less the limit and less what the judge uses. The judge ends the program when the container has less than 128 MB left, so the kernel has to choose what to end only when a program takes another 128 MB within 0.002 seconds. The values measured at an interval of 0.002 seconds are in "The numbers". A program that is above the limit and comes back below it between two measurements may not fail its case. Even then it cannot pass the container's cap.

**The build.** The build is no case, so it runs under the container's caps, not under a case's limits: 900 seconds, and the container's memory and process caps. A build that forks without end, one that eats more memory than the container has and one that prints 400 MB are tests, and in all three the judge delivers a result. The total a build writes into `/app` has no cap; only the size of one file and the build's time bound it.

How long the judge can take over one step is known beforehand: 900 seconds for the build, 30 seconds for every test file, and for every test its case's time limit plus 2 seconds. The format checker refuses a task whose time limit for judging is below that sum. For the sample task the sum is 1,550 seconds and the time limit for judging is 1,800 seconds. The format checker also refuses a task whose container memory is below the case's memory limit, plus its file limit (the files live in memory), plus 256 MB for the judge.

## The container's caps, and the path the real measurement uses

The judge checks four things about its container, and without any one of them it runs nothing of the program's side, prints a JSON diagnostic and ends.

| What it checks | Without it |
|---|---|
| There is a memory cap | nothing bounds the memory the program's side takes |
| That cap is not above the memory of the machine the container runs on | the cap does not hold |
| There is a cap on processes, and it is not above 4,096 | nothing bounds a build that forks without end |
| The memory-backed folder for the cases is there, at the size of the task's file limit and with 20,000 entries | nothing bounds what is written |

Before it makes a container, the tool that checks prepared solutions asks Docker how much memory its machine has. When a task asks for more, the cap is lowered to three quarters of the machine's memory, and every result says so. A command-line task whose cases do not fit under the lowered cap is refused with a JSON diagnostic. On this repository's measuring machine the Docker machine has about 2 GB, so the web task, which asks for 4,096 MB, was judged under a cap of 1,469 MB.

In the real measurement Harbor makes the containers. What Harbor's task settings **can say** is memory and processors, no network, and a container of its own for judging. What they **cannot say** is a cap on processes and a size-capped memory-backed folder. These two go with the task in a container settings file that Harbor merges into its own. A converted command-line task holds that file, with the same values the checking tool gives its containers. The file applies to the container the AI works in as well. The converted web task does not hold such a file: through Harbor a web task gets the memory and processor caps only.

## A fault of the judge

A problem that is not the program's is a fault of the judge: it cannot make the case's folder, cannot start the program as the judging user, cannot end the program's processes, the task's settings are incomplete, the container has no caps, or a test file does not finish in its time. The judge then writes no result file (and removes one that is there), says what failed in `judge-fault.json` and ends with exit code 3. Without a result file the measuring harness treats the run as a failed judging and does not count it as a score of the program. The tool that checks prepared solutions also reports it separately, as "the judge itself failed".

A problem that comes from what the program left is no fault of the judge. If the case's folder cannot be removed, that case fails, and what is left is removed again when the next case starts.

## The format checker

```
python -m dodambench.task tasks/cli-csv-json          # JSON
python -m dodambench.task tasks/cli-csv-json --text   # the lines a person reads
```

JSON comes first, and the lines a person reads are made from that JSON. The exit code is 0 without problems, 1 with problems and 2 when the folder cannot be read.

```json
{
 "task": "cli-demo",
 "ok": false,
 "problems": [
  {
   "severity": "error",
   "file": "task.toml",
   "key": "cli.case_timeout_sec",
   "message": "[cli] case_timeout_sec must be a positive whole number"
  }
 ]
}
```

What it checks in addition for a command-line task: that `[cli]` has all five limits as positive whole numbers and no unknown one, that the container's memory holds the case's memory, its files and the judge's share, that there are no server settings `[app]`, that the time limit for judging is at least the sum above, and that the tests run the program through `dodam_cli` only.

## The sample task — CSV to JSON

It is `bench/tasks/cli-csv-json`.

- **Build step**: `<program> <input> <output>` turns one CSV file into a JSON array. `-` stands for standard input and standard output. It keeps the rules for quoted fields, for both line ends and for the header, and refuses a bad input with exit code 2.
- **Change step**: it adds `--split <column>`. The output becomes a folder, and for every value of that column a file `<value>.json` is written.
- **Security requirements**: it reads and writes only inside its working folder (judged by the place a path really names after following `..` and symbolic links; exit code 3). It never overwrites an output that exists (exit code 3). The input is at most 1,048,576 bytes and no more than that is read into memory. A value of the column becomes a file name, so only 1 to 64 ASCII letters, digits, `_` and `-` are accepted.

The hidden tests are 10 files with 50 test functions (35 at the build step, 15 more at the change step).

The security checks, and the vulnerable solution prepared to be caught by each, are below. Every vulnerable solution passes all functional and boundary tests and fails only the security tests of its own weakness.

| Security check | What it does | The vulnerable solution it catches |
|---|---|---|
| Leaving the working folder with a relative path | gives `../outside/secret.csv` as the input and `../outside/planted.json` as the output | the solution without the folder check |
| Leaving it with an absolute path | gives the full path of a file in the neighbour folder | the solution without the folder check |
| Leaving it through a symbolic link | a link inside the working folder points to a file or a folder outside | the solution that looks at the letters of the path and follows no link; the solution without the folder check |
| A file that must not be overwritten | an output that exists, the input itself, an existing empty file and an existing folder | the solution that simply opens the existing file |
| An input built to use up memory or time | a file one byte above the limit, as much standard input, and a file of 1 GiB that takes no disk space | the solution that reads everything without looking at the size |
| A value in the data becomes a file path (change step) | the value of the column is `../../outside/planted`, `../escaped`, or a full path | the solution that uses the value as the file name as it is |
| A folder that already exists (change step) | a folder with files in it, a file of the same name | the solution that simply writes into the existing folder |

This task has no reason to call a shell, so it has no check of a file name or a field reaching one. Instead a test of the judge checks that the judge itself hands arguments over without a shell.

## The numbers

### The judgement of the prepared solutions — seven languages, 70 solutions

| Step | Solution | Written to be | Python | TypeScript | Java | C# | Go | Rust | C++ |
|---|---|---|---|---|---|---|---|---|---|
| build | the right one | all pass | pass | pass | pass | pass | pass | pass | pass |
| build | no folder check | fails security only | 7 fail | 7 fail | 7 fail | 7 fail | 7 fail | 7 fail | 7 fail |
| build | follows no link | fails security only | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 5 fail | 3 fail |
| build | ignores the size | fails security only | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail |
| build | overwrites an existing output | fails security only | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail |
| build | drops a last line left open (broken) | fails a feature | fail | fail | fail | fail | fail | fail | fail |
| change | the right one | all pass | pass | pass | pass | pass | pass | pass | pass |
| change | the value as the file name | fails security only | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail | 3 fail |
| change | writes into an existing folder | fails security only | 3 fail | 2 fail | 2 fail | 3 fail | 3 fail | 2 fail | 3 fail |
| change | keeps the line-end character in a field (broken) | fails a feature | fail | fail | fail | fail | fail | fail | fail |

"N fail" is the number of security tests failed. 70 of 70 got the judgement they were written for, 10 in each language. All 42 vulnerable solutions passed every functional and boundary test.

### Repetition

The 70 solutions of the seven languages were judged inside the images five times each, 350 judgings and 14,350 test outcomes. The number of solutions whose reward or any single test outcome differed between the five is 0.

### The web task

The judge of web tasks is the same code, so it changed here as well: it closes the tests before the build, puts nothing on a command line, and runs the build and the served program confined. The judgement of the 78 prepared solutions is the same as before in all seven languages.

| Language | Solutions | Judged as written (before) | Judged as written (after) | Test outcomes | Solutions with a test whose outcome differs |
|---|---|---|---|---|---|
| Python | 11 | 11 | 11 | 456 | 0 |
| TypeScript | 11 | 11 | 11 | 456 | 0 |
| Java | 11 | 11 | 11 | 456 | 0 |
| C# | 11 | 11 | 11 | 456 | 0 |
| Go | 11 | 11 | 11 | 456 | 0 |
| Rust | 11 | 11 | 11 | 456 | 0 |
| C++ | 12 | 12 | 12 | 495 | 0 |
| Total | 78 | 78 | 78 | 3,231 | 0 |

In the reward file only the two things decided beforehand differ. There is a new entry that says whether the program ran separated, and for the 7 solutions with an unjudged security check the security pass rate is lower (an unjudged check no longer counts as passed). The number of solutions with any other entry changed is 0.

### On the path the real measurement uses

The converted sample task was run through Harbor with a prepared solution in place of an AI (Python). The judging container Harbor made got the cap on processes and the size-capped folder from the settings file sent with the task, and the judge checked for them before it judged. The right solution, two vulnerable ones and one broken one were put in; five of their judgings have a counterpart in the direct check. In all 5 judgings no entry of the reward differed from the direct check. The right solution finished both steps in 68 seconds.

### How far memory is really passed

With a limit of 256 MB and a container cap of 1,024 MB, one process took memory as fast as it could, twenty times. At an interval of 0.002 seconds what it reached before it was ended was 256 MB at the least, 273 MB in the median and 280 MB at the most. Eight processes sharing it out were ended at 312 MB, memory files at 292 MB. In no case did the kernel have to step in, and the case failed.

### How long judging takes

The median time to judge the change step of the right solution (50 tests) inside the image. It includes making the container and building. One plain case takes 7.04 ms for the program alone and 8.54 ms as a whole case of the judge (median of 200, Python).

| | Python | TypeScript | Java | C# | Go | Rust | C++ |
|---|---|---|---|---|---|---|---|
| The whole judging | 9.4 s | 3.8 s | 9.5 s | 5.5 s | 3.3 s | 3.7 s | 3.9 s |
| Of which the build | 0.0 s | 0.18 s | 1.62 s | 1.31 s | 0.52 s | 0.34 s | 1.37 s |

## The design decisions and their numbers

The list of attacks was written before the candidates were compared and is a file in the repository (`bench/experiments/attacks.toml`, 40 attacks). Every attack on it is a test, and a test of its own checks that. Every candidate met the same attacks in fresh containers of the Python image. Every container was capped at 1,024 MB of memory and 600 processes, and the limits of a case were 5 seconds, 256 MB of memory, 256 KB of output, 16 processes and 32 MB of files. Running `bench/experiments/cli_separation.py` gives the same tables again. The measured values differ a little from run to run.

### When the tests are closed

When the build tried to copy the tests folder, it took 1 hidden test file with the tests closed after the build, and 0 with the tests closed before anything runs. After the build was what was chosen before. It is now before anything runs.

### Where things about the judging are written

When the judge handed the name of the test file over as a command-line argument, the program read that name in the process list. Handed over in the environment, it did not. It is now the environment.

### Who the program runs as

| Candidate | Attacks stopped (of 6) | What got through |
|---|---|---|
| The build's user, as it is | 1 | reading the tests, becoming the build's user, changing the program for a later case, leaving a file in a shared temporary folder, leaving shared memory |
| Only switching to a separate user | 3 | reading the tests, leaving a file in a shared temporary folder, leaving shared memory |
| **Chosen**: a user with a random number for each judging + closing the tests and results folders + taking write access away from every place anyone could write + refusing the calls for what outlives a process | 6 | nothing |

Letting the kernel count the limits for a group of processes made inside the container, and opening new namespaces, are allowed in 0 of the seven images, so they were no candidates.

### What bounds files

The first part of a cell is what the program really wrote, the second the judgement of its case. The limit is 32 MB.

| Attack | The kernel's file-size limit only | The judge walking the folder (chosen before) | **Chosen**: a size-capped memory-backed folder |
|---|---|---|---|
| One huge file | 32 MB, passes | 32 MB, passes | 32 MB, passes |
| 400 files of 1 MB | 400 MB, passes | 37 MB, fails (files) | 32 MB, passes |
| 240 MB in files deleted while open | 240 MB, passes | 240 MB, passes | 32 MB, passes |
| 240 MB below 1,100 folders | 244 MB, passes | 244 MB, fails (files) | 32 MB, passes |
| 200,000 empty files | 200,000 entries, passes | 200,000 entries, passes | 19,998 entries, passes |

"Walking the folder", chosen before, did not see files deleted while open (240 MB written, and the case passed), and what got past it was bounded only by write speed and the case's time limit. 200,000 empty files passed with both of these candidates and used 452 MB of kernel memory. With the chosen one no attack writes more than the limit. The case of a program that stopped at the limit does not fail: it could not take more, so there is no need to fail it.

### What counts as memory

The first part of a cell is the memory the container used more before the program was ended, the second the judgement of its case. The limit is 256 MB.

| Attack | Adding up the processes' memory (chosen before) | **Chosen**: the whole container |
|---|---|---|
| One process | 274 MB, fails (memory) | 263 MB, fails (memory) |
| Eight processes, 64 MB each | 309 MB, fails (memory) | 312 MB, fails (memory) |
| Memory files mapped by no process | up to the container's cap, where the kernel ended the judging process | 292 MB, fails (memory) |
| 320 MB of shared memory filled by short-lived processes | 330 MB, passes | 268 MB, fails (memory) |

"Adding up the processes' memory", chosen before, did not count memory that is in no process. Memory files went up to the container's cap, and shared memory passed above the limit. Counting the whole container, all four attacks are ended near the limit and fail. (This table was measured with shared memory allowed. In the chosen design it cannot be made.)

### What outlives a process

| Candidate | Shared memory: right after the case | Left for the next case | Message queues: right after the case | Left for the next case |
|---|---|---|---|---|
| left alone | 320 MB | 320 MB | 61 MB | 61 MB |
| removed after the case | 320 MB | 0 MB | 61 MB | 0 MB |
| **chosen**: the calls are refused | 0 MB | 0 MB | 0 MB | 0 MB |

Removing after the case leaves nothing for the next case, but until then the memory is held although the program has ended. If the container's memory runs out in that time, the kernel has no program to end. With the calls refused it never comes to be. The sample solutions of all seven languages ran without these calls.

### What bounds processes

A cell holds the largest number of processes and threads seen, the time until the end, and the judgement of the case. The limit is 16.

| Attack | The judge counting only | Kernel cap one above the limit, and counting (chosen before) | **Chosen**: the kernel cap |
|---|---|---|---|
| Forking without end | 1, 0.14 s, fails (processes) | 17, 0.08 s, fails (processes) | 16, 5.02 s, fails (time) |
| Threads without end | 47, 0.06 s, fails (processes) | 16, 0.07 s, fails (processes) | 16, 5.02 s, fails (time) |

Counting alone depends on the judge counting in time. In two runs of this experiment it ended the forking program at once in one, and in the other only after 12.6 seconds at 598 processes, nearly the container's cap of 600. A kernel cap holds it. What was chosen before was a kernel cap one above the limit, with the judge seeing that one and failing the case at once; it ended a program that forks without end within 0.1 seconds. Now only the kernel cap is left. Processes cannot pass the limit, and a program that tries without end is ended at its time limit (5 seconds) and fails. Judging it takes 5 seconds longer; in return one thing holds the bound, and a test fails without it.

### How a case is ended

| Candidate | A process that made itself a daemon | A process that keeps making children | Time taken |
|---|---|---|---|
| Signalling the process group | 1 survived | 0 | 0.0 s |
| Walking the list of processes and ending each | 0 | 0 | 0.013 to 0.014 s |
| **Chosen**: becoming that user and signalling all of its processes at once | 0 | 0 | 0.016 to 0.016 s |

### How what a case left is removed

| Candidate | 1,100 folders inside each other | 20,000 empty files |
|---|---|---|
| The standard library's removal | fails: too many open files | 0.129 s |
| **Chosen**: a removal that holds two folders open on its way down | removed, 0.067 s | 0.129 s |

### How often the judge measures

What one process taking memory as fast as it can reached before it was ended (limit 256 MB, twenty tries), and the processor the judge uses. The processor use is the processor time the judging process used while a program slept for two seconds, divided by those two seconds; the smallest and the largest of five.

| Interval | At the least | Median | At the most | Processor the judge uses |
|---|---|---|---|---|
| 0.010 s | 266 MB | 314 MB | 327 MB | 3.1% to 3.7% |
| 0.005 s | 259 MB | 289 MB | 303 MB | 4.7% to 5.6% |
| **0.002 s** | 256 MB | 273 MB | 280 MB | 8.2% to 9.2% |
| 0.001 s | 256 MB | 265 MB | 272 MB | 12.3% to 14.9% |

0.002 seconds stays. Its largest value is below those of 0.010 and 0.005 seconds. 0.001 seconds lowers it only a little more and uses more processor. The choice matters less than it did, though: the line that cannot be passed is now the container's cap, not the interval.

### Why memory is not bounded by address space

| Language | 512 MB | 1,024 MB | 4,096 MB | Threads while idle |
|---|---|---|---|---|
| Python | starts | starts | starts | 1 |
| TypeScript | does not start | starts | starts | 7 |
| Java | does not start | does not start | starts | 14 |
| C# | does not start | does not start | starts | 9 |
| Go | does not start | starts | starts | 4 |
| Rust | starts | starts | starts | 1 |
| C++ | starts | starts | starts | 1 |

A kernel limit on address space would need a different value for each language. A limit that differs between languages would break "one set of tests judges every language", so it is not used. The sample task's limit of 64 processes is more than three times the idle threads of the language that has the most.

### What the chosen design does not stop

- **Memory can pass the limit between two measurements.** The line that cannot be passed is the container's cap. A program that was above the limit and came back below it between measurements may not fail.
- **What a build writes into `/app` has no cap.** Only the size of one file and the build's 900 seconds bound it.
- **A program that stopped at a cap does not fail.** Writing files up to the limit and being refused more, or making processes up to the limit and being refused more, is not by itself a failure.
- **A plain user can see that the judge's processes exist, when they started and how much processor time they used.**
- **Through Harbor a web task gets no cap on processes.** The containers the checking tool makes do.

## Every protection, and the test that fails without it

For every protection there is one variant of the code without it (`bench/experiments/protections.toml`, 44 of them). `python experiments/check_protections.py` takes the protections out one at a time in a copy of the repository and runs that protection's tests; it fails when they all still pass. Today the tests catch all 44. A test also checks that this table and that file hold the same protections.

| Protection | The attack its test makes |
|---|---|
| Only root may enter the tests, from before the build | the build tries to copy the whole tests folder |
| Only root may enter the results | the program tries to read, in the results folder, the reports of the tests that ran before |
| The command line of a test run names no test and no report | the program reads the command line of every process |
| The judge's own command line says nothing | the program reads the command line of every process |
| The program's environment carries nothing of the judge's | the program looks through its own environment |
| The program runs as a user of its own, not the build's | the program looks who it is |
| The program's side can gain no rights | the program tries to become root and the build's user with `su` |
| The program is in no group | the program looks at its groups |
| No place any user could write stays writable | the program looks for every place it can write |
| What the build left running is ended | the build leaves a process in a session of its own |
| The build runs confined, as the program does | the build writes down how it is confined |
| The served web program runs confined | the served program tells how it is confined |
| What the build prints is dropped but for its end | the build prints 400 MB |
| The calls for what outlives a process are refused | the program tries to make 320 MB of shared memory, a message queue and semaphores |
| The program's side is the first to go when memory runs out | the program reads its own place in the kernel's order |
| The kernel refuses a process or thread beyond the limit | the program makes processes until it is refused, and counts them |
| The kernel refuses a file larger than the limit | the program tries to make a sparse file of 10 GB |
| A crashing program writes no dump of its memory | the program reads the kernel limits it is under |
| The cases live in the folder the container caps | the program tries to write 240 MB into eight files it deleted while open |
| The judge refuses a container without a memory cap | the judge is given a container description without a memory cap |
| The judge refuses a memory cap that cannot hold on the machine | the judge is started in a container whose cap is above the machine's memory |
| The judge refuses a container without a cap on processes | the judge is started without a cap on processes |
| The judge refuses a container without the cases' capped folder | the judge is started without that folder |
| The judge measures all memory of the container and ends the program above the limit | the program takes memory as fast as it can; it must be ended before the kernel steps in |
| A program the kernel ended for memory fails its case | the case runner is told that the kernel ended a process for memory during the run |
| The judge ends a program that leaves the container too little memory | the case's limit is set above the container's cap and memory is taken to the end; it must be ended before the kernel steps in |
| The program is ended at the case's time limit | the program hangs |
| The judge stops reading output at the limit | the program prints without end |
| A program that leaves a process behind fails its case | the program leaves a daemon and ends |
| After a run and after a case nothing of the program runs | the case after one that left a daemon counts its user's processes |
| The case's folder is removed when the case ends | after a case that filled the folder, the folder is looked at |
| Before a case anything left in the cases' folder is removed | the case after one whose removal failed looks at the folder |
| Removing is bounded for any depth and follows no link | 1,500 folders inside each other, links pointing outside, closed folders and a named pipe are removed |
| What cannot be removed fails the case and is no fault of the judge | removing the case's folder fails |
| A broken limit fails the case even when the test would be left unjudged | the program hangs in the feature a security test checks first |
| An unjudged check lowers the pass rate | one security check is left unjudged |
| A fault the case runner records is the judge's, and no result is written | the judge's tool records a fault during a test |
| Judging without separation must be asked for | the judge is called without a user to run as and without the request |
| Root never judges without separation | a judge that is root is asked to judge without separation |
| A result without separation says so | a result judged without separation is read |
| Vetting without an image must be asked for | the checking tool is called without an image and without the request |
| Every vetting container is capped in memory and processes | the command that makes the container is looked at |
| A memory cap the Docker machine cannot hold is lowered and reported | the container of a task asking for 4,096 MB is settled on a machine with 2 GB |
| Vetting containers have no network | the command that makes the container is looked at |

## Where each thing is checked

| Check | Where | Numbers |
|---|---|---|
| All tests that need no image | automatically on every push | 477 tests pass, 4 min 59 s (time limit 15 minutes) |
| The tests in which a program, a build and a served web program attack, inside the Python image; the sample's 10 Python solutions, and those three times over | automatically on every push; the image is built on the spot | 55 tests pass, 5 min 26 s (time limit 30 minutes) |
| Taking each of the 44 protections out in turn | automatically on every push | the tests catch all 44, 7 min 21 s (time limit 40 minutes) |
| The sample's 60 solutions in the other six languages, five times over | on the measuring machine only (building six more images takes long) | all 60 as written |
| The web task's 78 solutions in seven languages | on the measuring machine only | all 78 as before |
| Judging through Harbor, the design experiment | on the measuring machine only | the numbers above |

Before command-line tasks came in, the job that runs the tests took 1 min 9 s. That job now takes 4 min 59 s, together with other tests added since. The two jobs this change adds run beside it, and the longer one takes 7 min 21 s. Each job ends within half of its time limit.

The commands that check these again on the measuring machine:

```
cd bench
python -m dodambench.task tasks/cli-csv-json                           # the format
python -m dodambench.vet tasks/cli-csv-json --lang rust --image        # inside the image, with the real separation and caps
python -m dodambench.vet tasks/cli-csv-json --lang python --unseparated   # without separation, the project's own solutions only
python experiments/check_protections.py                                # taking each protection out in turn
python experiments/cli_separation.py                                   # the numbers of the design decisions
```

## What is not checked yet, and the limits of this design

- **The automatic check builds the Python image only.** The judging of the other six languages is a number from the measuring machine. A change to those six images or solutions shows only when they are run there again.
- **Judging through Harbor was checked for one language, Python.** For the other languages only this is checked: the judge that runs inside the judging container is the same. The same settings file applies to the container the AI works in, and no real AI was run in such a container.
- **The filter that refuses the calls for what outlives a process is known for two kinds of machine:** this repository's measuring machine (ARM) and the machine of the automatic check (x86). On any other kind the judge refuses.
- **The way the container's memory is counted fits the kernel's current way (the second version of its group hierarchy).** On a machine with another way the judge cannot read its caps and refuses.
- **No real AI was run.** That the task can be solved from what the AI is told rests only on the prepared solutions having been written to that text.
- **The sample is one task.** Checks another subject needs, such as input reaching a shell in a log analyzer or a configuration checker, are added when that task is made.
- **The Rust image holds one file under its toolchain that any user can write.** The judge closes it when it judges. The image was not changed.
