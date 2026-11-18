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
memory_mb = 1408         # at least the case's memory + its files + 256 for the judge

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

1. It closes the tests folder and the results folder to everyone but root, and makes sure no other user can rename or replace them from the folder above. It removes whatever lies where its own result files go. This comes before the build.
2. It looks whether its container can bear the judging, for a web task as for a command-line task. Without a memory cap, with a memory cap above the memory of the machine the container runs on, without a cap on processes, or, for a command-line task, without the size-capped folder the cases live in, it runs nothing and refuses. It also reads how much the container may move out to swap and writes that into its result.
3. It ends whatever the working user left running, hands `/app` to the plain user `agent` and puts the image's pinned copies where the dependency folders go, whatever the program brought there.
4. It builds `/app`. The build runs as `agent`, confined as the program is: it cannot gain rights, the kernel ends it first when the container's memory runs out, and it cannot make anything in the kernel that outlives it. Of what the build prints only the end is kept. When the build ends, every process of that user is ended, also when the container holds so many processes that the judge cannot make one more. A build that was ended from outside (at its time limit, by a signal, or because the kernel ended one of its processes when memory ran out) has failed, and the result says by what.
5. It makes `/app` readable and runnable by everyone and changeable by nobody but its owner, and removes every mark that lends a file's owner's rights to whoever runs it. It takes write access away from every place in the container that any user could write (`/tmp`, `/var/tmp`, `/dev/shm` and others).
6. It runs the test files one by one. For every case a test runs the program as **a user made for this judging alone**. The user's number is drawn at random from a wide range, so it is no user of the image. It owns no file, is in no group, and gains nothing by running a program that grants rights (such as `su`). The only place it can write is the folder of its case, and that folder lives in a memory-backed folder whose size the container caps.
7. After every run it ends all processes of that user at once, and after every case it removes the case's folder. The outcome of a case is written down the moment the case ends.
8. It writes the pass rates of the functional, boundary and security tests, the number of tests it could not judge, the number of weaknesses found, the number of earlier steps' tests a change step broke, and whether the program ran separated from the judge, into the result file. Only the judge writes the result file.

Nothing about the judging is on the command line of any process, because every user of the container can read command lines. Which test file is run, where the outcomes go and which step it is travel in the environment, which only root can read. The judge of web tasks is the same code, so steps 1 to 4 and this paragraph hold for web tasks as well, and a web task's build and served program run under the same confinement. Between two starts of a served program the judge ends every process it left, also one that moved out of the group of processes the judge started, and removes the data folder that start wrote.

What a plain user can still see of the judge's processes is that they exist, the name of their program (`python3`), their state (running or waiting), how much memory they hold, when they started, how much processor time they have used and how often the kernel switched them in and out, and the kernel limits they run under. It can also read the list of file systems mounted in the container. None of that names a test or says which test is being run.

**Whatever the program's side left as files** (in `/app`, where the dependency folders go, in the temporary folders, in a case's folder, in a served program's data folder) the judge removes, hands over, opens for reading or searches with routines that take one step per entry, hold two folders open at a time however deep the tree is, and follow no link out of it. They give up after 500,000 entries, and the search for places any user may write gives up after 10,000 such places or 300 seconds. When they give up, the step fails with that reason and a result is written. A writable place so deep that no path can name it is closed all the same: the judge closes the folder above it that a path can still name to other users, who then reach nothing below it.

## When something is ended while a case runs

Whatever is ended, by the kernel because the container's memory ran out or by anything else, the judging ends in a judgement.

| What is ended | What is reported |
|---|---|
| A process of the program | The case that was running fails, for memory when the kernel ended it. The cases before it keep their outcome and the cases after it are judged |
| The served program of a web task, by the kernel | The case that was running fails with that reason, even when its test would have passed. The cases after it are judged against a program started afresh |
| The process that runs the cases of a test file | The case that was running fails with the reason (the kernel ended it for memory, or the signal that ended it). The cases before it keep their outcome. The judge ends what that case left running, empties the cases' folder or starts the served program afresh, and a new process runs the cases after it. The other test files are judged as usual |
| The build | The build has failed and the result says by what it was ended. Functional and boundary tests of the step fail, and its security tests are unjudged |
| The judge itself, or the whole container | Nothing inside can write a result any more. The judge removed the result files of any earlier judging before it ran anything, and it writes a result under another name and moves it into place only when it is complete, so no file reads as a judgement. The exit code is not zero. The script that starts the judge outlives it where the container does, and then writes `judge-fault.json` saying that the judging ended without a result |

The outcomes of a test file do not wait for the file to finish: each is written down when its case ends, in a place only root can enter. A process that runs the cases and is ended twice in a row without any case being decided costs the next case, which fails with that reason, so the judging always ends.

On the path the real measurement uses, a step without a result file is a step with an error and no reward: Harbor records the error for that step and goes on to the next step. The measurement's runner reads the step's reward as missing, lists the step among the missing ones and gives the run a status other than "ok". It does not count such a step as passed. Harbor's own number for the whole run is the mean over the steps that have a result, so it leaves the step out; the runner's status is what tells that a step is missing.

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
| The program cannot write, replace or remove the result file | Only root can enter the results folder, and nobody else can rename it from the folder above. Whatever lay where a result goes is removed before anything runs. The judge writes the result to a new file and renames it into place, so it never follows a planted link |
| A limit is a bound that holds whatever the program does | The section "The limits" says for each resource what enforces it and how far it can really be passed |
| What one case or one step takes is given back before the next starts | Every process is ended and the case's folder removed. Shared memory and message queues that would stay after their processes cannot be made in the first place. What the working user left running is ended before the build, what the build left running before the first case, and what a served program left running and wrote before its next start |
| The judge survives whatever the program's side leaves behind | Every place where the judge removes, hands over, opens or searches what the program's side wrote is bounded in time, memory and open files for any tree of folders, and follows no link. If a case's folder cannot be removed, that case fails; it is no fault of the judge, and the other cases are judged as usual. A tree with more entries than the judge handles fails the step, with a result |
| A kill ends in a judgement | The section "When something is ended while a case runs" |
| What could not be judged never counts as passed | The three points of the section "A check that could not be judged is no pass" |
| A fault of the judge itself is told apart from a failure of the program | On a fault the judge writes no result file, records the fault separately and ends with a different exit code |
| Cases do not affect each other | A fresh folder for every case. Of the files, processes and kernel objects an earlier case tried to leave, the next case can see none |
| One set of tests judges every language | The tests folder that goes into the judging container differs between languages in one file only, the one that names the commands. The environment the tests get is the same for every language |
| Every container has caps that hold | Every container that the checking tool, the tests and the experiments start is capped in memory (with no swap beyond it), processors and processes. A converted task of either kind carries the cap on processes and the swap setting to Harbor, and both tasks ask for a memory cap the measuring machine can hold. The judge checks the caps of its own container for every kind of task |
| The format checker accepts the sample and refuses a malformed command-line task with a JSON diagnostic | The checker prints each problem as JSON with its file, key and message first; the lines a person reads are made from that JSON |

## The limits

A limit is a bound, not a measurement. For each resource there is a line that cannot be passed whatever the program does, and the kernel or the container holds it.

| Resource | The line that cannot be passed | What holds it | A case that broke the limit |
|---|---|---|---|
| Time | the case's time limit plus the time it takes to end the processes | when the time is up the judge ends every process of that user | fails |
| Output | the output limit plus what one pipe holds | the judge keeps what fits in the limit, reads no more and ends the program | fails |
| Processes and threads | the limit itself | the kernel refuses that user a process or thread beyond the limit; the user is this judging's alone | cannot be taken |
| Files | the limit itself, and 20,000 entries | the container caps the memory-backed folder the case's folder lives in at that size. Many small files, deep folders, files deleted while open, the home and temporary folders are all inside it, so together they come to that size. One file cannot be made larger than the limit, not even a sparse one (the kernel) | cannot be taken. A program the kernel ended for making one file too large fails |
| Memory | the container's memory cap, and beside it only the swap the container is allowed (none on every path of this repository) | as often as it gets to run, and at most every 0.002 seconds, the judge measures the memory of the whole container and ends the program above the limit. After the run it reads the highest value the kernel kept for the container; a new high above the limit is written into the result and fails no case. What gets past the measuring is stopped by the kernel at the container's cap, where the kernel ends the program first | fails; so does a case during which the kernel ended a process |

**What counts as memory.** Not the memory of the processes alone but everything the kernel counts for the container: the memory of all processes, memory files that belong to no process, pipes and what the kernel keeps for them. Memory the kernel moved out to swap is counted as memory. Of the pages of files on disk, those that stand on the kernel's two lists of pages it can give back are taken out. A page of a file that stands on neither list counts as memory, whatever keeps it off them. The files in the case's folder are taken out (the case's files count against the file limit). So is the ordinary memory of the case runner, the judge's own process that starts the program, feeds its input and reads its output. A program gains nothing from that: what it makes the runner hold is not memory the program can use, and it is bounded by the limit on output.

**One starting figure for a case.** A case has one starting figure, read before its first run, when the test's own files are written. Every run of the case is measured against it, so what an earlier run of the case left in the container counts in a later one. When something that was there at the start goes away during the case, the case is counted from the lowest figure it reached: its going makes no room. Honest programs fall a little below their start by themselves. Over the sample's 70 judgings the largest fall was 1.49 MB, so a case can be counted that much higher than it would be from its start alone. Between the measure before these rules and the measure with them, the counted memory of a case of the sample changed by 1.73 MB at the most (2,870 cases), and no judgement changed. What the rule cannot see is memory that goes away at the very moment the program takes its own, because then no measurement shows a fall. A program that deletes the 19,000 files a test put into its case while it takes memory is a test: the judge's count stayed above what the container really held more than at the case's start.

**The kernel's records of names and files.** For every name and file that is looked at, and for every page of the cache of files, the kernel keeps a record of its own. It gives these records back whenever memory is needed, as it does the cache, and says how much they are. They are no small thing: a program that held 4 MB and asked for 900,000 names that are not there made the kernel hold 669 MB of them. But the kernel cannot give a record back while something holds on to what it describes: an open file, a file mapped into memory, a file in the case's folder. Its figures do not tell the two kinds apart, and a program can hold on to a great many: 300,000 small memory files, mapped and closed again, kept 206 MB of records in place.

So these records are left out of a case's memory only up to a bound: a quarter of the case's memory limit, and never more than 16 MB. What they grow by beyond that during the case counts as the case's memory like everything else. A rise counts and a fall gives nothing back, so that the kernel giving back older records makes no room for newer ones that are held. 16 MB of records is about 22,000 names that are not there, or about 24,000 small files that are held mapped. No prepared solution comes near it: over the sample's 70 judgings in seven languages the most the records of the whole container grew by during one case was 0.10 MB, and nothing was counted in any case. A program that only reads files still never fails for memory, however much it reads. A program that only looks at files is free up to that amount and pays for what goes beyond it: asking for 400,000 names that are not there fails a case with a limit of 64 MB.

The bound is in the measure itself, so it does not depend on how the records are held. In five ways of holding on to them (open files, memory files that are mapped and closed, shared mappings without a file, the image's files mapped, far-apart pages in the case's folder), each at limits of 64, 256 and 512 MB, what the container held beyond the judge's count was 16.3 MB at the most, and a program that held clearly more than its limit and that amount failed for memory.

**How the tests know what a container really holds.** Not from the kernel's figures for the forms memory can take: a sum of those would leave out whatever form nobody thought of. The tests ask the kernel. While the program holds what it holds, a helper outside the judged container stops that container, has the machine write the pages that wait to be written to disk, has the kernel give back everything of the container that it can give back, reads what is then still charged to it, memory and swap together, and lets the container run on. The container stands still because a program that reads a file again and again would bring pages back while the kernel gives them back, and they would read as held although nothing keeps them. The waiting pages are written first because the kernel cannot give back a page of a file before it is on disk; a case's own user cannot make such pages, since everything it can write is memory-backed, but the build and the judge's own files can. The same reading is taken when the case begins, and the difference is what the case really holds. Whatever keeps a page shows in it. This reading is the tests' and not the judge's. The helper holds what a judged container must never get, the kernel's own files for that container, open for writing. It exists only in the tests, on the measuring computer and in the two container jobs of the automatic check, and never in a judging that counts. It has no network and nothing mounted beyond what it writes to, and a test shows that the judged container, even as root, cannot write the helper's files or ask the kernel itself. What the reading cannot show is memory that the kernel charges to no container. For that the helper also reads how much memory the whole machine has in use, at the same two moments; the difference to the container's charge is printed with every figure. Other things on a machine move that figure, so nothing is asserted on it. The tests allow 3 MB on top of the bound, by a rule: the largest distance between a reading and what it should be, over every turn of the reading's own test and every bound test on the machine of the automatic check, is doubled and rounded up to a whole MB, and is never under 2; above 6 the reading would not be good enough to use. The reading's own test runs ten times in every run of the check and prints every figure. In the two runs on the commit that made this change the largest distances were 1.46 and 1.32 MB, and ten readings of the same program lay up to 1.31 MB apart (0.58 MB on the measuring computer). So a case that passes holds at most 19 MB beyond the judge's count.

The room a container gets beyond the limits of a case is 256 MB, and the judge ends a program that leaves the container less than 128 MB: both are larger than that, so what is left out can neither fill the container nor get past that stop. The result says for every case how many bytes of records were left out and how many were counted (`records_left_out_bytes` and `records_counted_bytes` in `details.json`), so that a case that failed for memory shows why. A case whose runner was ended in the middle of it has neither field: the runner that would have reported them is gone, and the case fails with that reason.

**What cannot be made.** Memory that stays when its processes have ended, which is System V shared memory, message queues and semaphores, and POSIX message queues: the kernel refuses the calls themselves. The folder for POSIX shared memory (`/dev/shm`) is closed to a case. The build can write there, and what it leaves there is memory that would stay charged to the container through every case, so the judge empties that folder after the build, before the first case. So once the program's processes are ended, nothing of its memory is left but the files in the case's folder.

**Where each user can write.** Measured in all seven images. A case's user can write its own case folder and nothing else, beside the six device files every process may write (the null device and its kind). The build's user can write `/app`, `/home/agent`, `/tmp`, `/var/tmp`, `/run/lock` and `/dev/shm`; in the Go image also `/go`, in the Rust image `/usr/local/cargo`, `/usr/local/rustup` and `/opt/cargo-build`, in the Java image `/var/lib/pebble/default`. The folder of POSIX message queues can be opened by it, but the call that makes a queue is refused. Of all these only `/dev/shm` is memory. What one case leaves charged to the container when the next begins was 0.24 to 0.43 MB.

**Memory cannot be locked.** Nothing of the program's side, in the build or in a run, can lock memory: its limit on locked memory is 0. That covers locking a range (both calls for it), locking everything a process has, and a mapping asked for as locked. Locking shared memory of the old family needs such memory, which cannot be made. A locked page of a file is a page the kernel cannot give back, and the usual limit of 8 MB holds for each process again. The runtimes of all seven images start and run with the limit at 0.

**Pages cannot be handed to a pipe or a socket without copying.** Four calls of the kernel move pages into a pipe or a socket without copying them: sending a file into a socket (`sendfile`), moving data between a file and a pipe (`splice`), duplicating what a pipe holds (`tee`) and giving a program's own pages to a pipe (`vmsplice`). For a judged program, in the build and in every run, these four calls answer "invalid argument". That is the answer the kernel itself gives for a kind of file whose pages it cannot move. The reason: a page of a file that a pipe or a socket holds cannot be given back, and no figure of the kernel counts the pages held this way. With the calls allowed, a container held up to 416 MB beyond the judge's count while its case passed. The libraries of the runtimes copy instead when they get this answer, and copied memory counts. That is a test in each of the seven images, through the calls a program of that language uses, which compares the bytes that arrive with the bytes that were sent: copying a file, sending a file over a local socket, copying one connection into another, in Java the transfer between a file and a socket in both directions, and serving a file through the web framework that the language's prepared web solutions use. Library calls that do not work, because they have no way of copying: in C#, `Socket.SendFile`, `Socket.SendFileAsync` and `Socket.SendPacketsAsync` with a file in the packet, which fail with a socket error; A C# program that needs them reads the file and writes it. In C++, the Drogon framework sends a file larger than about 200 KB with the kernel's file-sending call and has no way of copying instead: without more, nothing of such a file arrives and the server logs an error, for a file response (`newFileResponse`) and for a file of a served folder alike. The framework has a switch of its own for this. A judged C++ program that serves a file above that size turns the kernel's file-sending off with one line, `drogon::app().enableSendfile(false)`, or with `"use_sendfile": false` in the framework's configuration file; then every byte arrives. Smaller files are not affected: a file of 100 KB arrives either way. Tests pin all of this, the failures included, so that a runtime that learns to copy shows up. All prepared solutions were judged this way: the sample's 70 and the web task's 78 keep their judgement. The calls of the kernel's newer asynchronous input and output and its cryptography sockets hold pages for a program in the same way; they are refused as "not permitted". The container's default call filter refuses them already, with the same answer, so nothing changes for a program; the judge's own filter refuses them as well, so that the judge does not rest on that default. A call made the way another kind of machine makes it is refused altogether.

**The rings of the older asynchronous input and output cannot be made.** The kernel has an older family of calls for asynchronous input and output. Each ring a program makes for it is kept in pages that are charged as pages of a file and stand on the lists of pages that can be given back, but cannot be given back while the ring is there. How many there can be is bounded only by a setting of the machine: at 65,536 events, the value on the measuring computer and on the machine of the automatic check, a program's rings held 4.1 MB beyond the judge's count, and the figure grows with that setting. For the program's side the call that makes such a ring answers that the kernel has no such call, which is the answer of a kernel built without this family. No runtime in the seven images uses it: the sample's 70 and the web task's 78 keep their judgement.

**No pair of terminals can be made.** What a pair of terminals buffers is memory the kernel charges to no container, so no figure of a container shows it, and neither does the tests' reading. With as many pairs as the machine allows (3,071 on the measuring computer; the machine's setting is 4,096 there and on the machine of the automatic check), filled in both directions, the machine had 126 to 181 MB more in use than was charged to the container (four runs; 1 to 6 MB around a program that holds as much in other ways), and the program was counted 8 MB for it. So the judge closes the device that makes such pairs to every user but root before anything of the program's side runs, the build included. A program whose output goes to a terminal is not affected: the judge reads a program's output through pipes.

**How far memory can really be passed.** Memory alone can be passed between two measurements. The line that cannot be passed is the container's memory cap; the most a program can be above its limit is that cap less the limit and less what the judge uses. How far programs that try got, and how long the judge went without measuring while they kept both processors busy, is in "The numbers": the judge does not measure every 0.002 seconds under such a load.

**The highest value the kernel kept.** The kernel keeps the highest memory use of the container, whatever happens between two measurements. The judge reads it before and after every run, and it fails no case by itself. A case fails for memory when the judge measured the program above its limit, or when the kernel ended a process during the run.

The reason is what that value counts. It counts the cache of files, which is no memory of the program's, and the kernel keeps no count from which the judge could tell afterwards how much of a new high was cache. Reading a file that is on disk fills the cache, and that much the kernel counts for the container as bytes read from disk. But the cache grows in other ways that the kernel counts nowhere:

- reading a file that has holes, or one whose room on disk was set aside and never written, by reading it or by mapping it into memory: the cache fills with empty pages and nothing is read from disk;
- reading a file in a folder that does not lie on one of the Docker machine's disks;
- writing a file: what is written is in the cache before it is on disk. The judged program can write nowhere on disk, but the build does, and the judge does;
- pages of a file that several processes map together, which the kernel counts for whichever container touched them first;
- the kernel's own records of names, files and pages of the cache, which are in that value too.

A judge that failed a case on a new high after taking out what the container had read from disk was tried here first. A program that held 10 MB and read a file of 400 MB with nothing but a hole in it failed for memory in every judging, at limits of 256 MB and of 64 MB. No count closes all of these ways, so the value judges nothing.

What the judge does with it. When a run made a new high for the container, and that high lies further above the memory that could not be given back when the run began than the case may take in memory and files together, the result names the case and gives the numbers (`memory.highs_above_the_limit` in `details.json`: how many such runs there were, and for the ten that lay furthest above, the limit, the high before and after, what the container held before, and how far above the limit the high lay). That figure is the most the program can have held at once, not what it held. For a program that held 10 MB and read a file of 400 MB it says about 120 MB above a limit of 256 MB; for one that took 400 MB for a moment under the same limit it says the same.

What this record cannot show. A run is in it only when it made a new high for the container. The value never goes down, and it counts the cache of files: so everything the container ever held at one moment before, the cache of the build and of earlier cases included, hides a later run that stays below it. The value does not count memory moved out to swap. A run that used less of the case's file limit than it may is not named until its high lies above the memory limit and the whole file limit together. The result gives the numbers of the ten runs that lay furthest above; when more than ten are named, the others are only counted. And a run that is in the record need not have taken anything: reading alone makes a new high, and so does a plain run after a reader, which is then named with the height the reader's cache left, so that ten such runs can push the run that mattered out of the ten. For all of these the judge's own measuring and the container's cap remain.

**The stop at 128 MB.** Whatever the case's limit is, the judge ends a program when the container has less than 128 MB left. This is a second line of defence. A case during which the kernel ended a process fails anyway, and the other cases are judged whatever the kernel ends. What the stop does is keep the kernel from having to choose: in the runs of "The numbers" the kernel ended nothing with the stop in place, and without it ended a process in every run of the programs that fill the container. What the kernel chooses then is the judge's decision and not a matter of which process happens to hold more. Every process of the program's side carries the kernel's highest mark for being ended first (1,000), the case runner carries 500 and the judge none. In a container of 640 MB the kernel's score, by which it chooses, was 1,339 for the program, 1,016 for the case runner and 676 for the judge. Before the case runner had a mark it stood at 683 against the judge's 677, only because it holds more memory (about 51 MB against about 32 MB), and in one run of the automatic check without the stop the hidden test handed nothing back: the kernel had ended the case runner in the moment the program's memory was being released. So without the stop the kernel ends the program and, when that does not free memory soon enough, the case runner; the case fails for memory and a new runner judges the cases after it. The judge is the kernel's last choice. If it were ended, the judging would end without a result.

**A machine with swap.** A container that may swap could hold more than its memory cap. The checking tool gives its containers no swap beyond the memory cap, and the container settings file of a converted task takes away the swap Harbor would otherwise allow (as much again as the memory cap). The judge does not rest on that: it reads at its start how much swap its container may use, writes it into the result (`swap_mb`, 0 on every path of this repository), and counts swapped memory as memory, so a program above its limit fails also when part of it was swapped out. Where a container is allowed swap, the line that cannot be passed is the memory cap plus that allowance.

**The build.** The build is no case, so it runs under the container's caps, not under a case's limits: 900 seconds, and the container's memory and process caps. A build that forks without end, one that fills every process the container may hold, one that eats more memory than the container has and one that prints 400 MB are tests, and in all of them the judge delivers a result. The total a build writes into `/app` has no cap; only the size of one file and the build's time bound it.

How long the judge can take over one step is known beforehand: 900 seconds for the build, 30 seconds for every test file, and for every test its case's time limit plus 2 seconds. The format checker refuses a task whose time limit for judging is below that sum. For the sample task the sum is 1,550 seconds and the time limit for judging is 1,800 seconds. The format checker also refuses a task whose container memory is below the case's memory limit, plus its file limit (the files live in memory), plus 256 MB for the judge.

## The container's caps, and the path the real measurement uses

The judge checks these things about its container, for a web task as for a command-line task, and without any one of them it runs nothing of the program's side, prints a JSON diagnostic and ends. The last one is asked of a command-line task only, since a web task has no cases' folder.

| What it checks | Without it |
|---|---|
| There is a memory cap | nothing bounds the memory the program's side takes |
| That cap is not above the memory of the machine the container runs on | the cap does not hold |
| There is a cap on processes, and it is not above 4,096 | nothing bounds a build that forks without end |
| The memory-backed folder for the cases is there, at the size of the task's file limit and with 20,000 entries | nothing bounds what is written |

Before it makes a container, the tool that checks prepared solutions asks Docker how much memory its machine has. When a task asks for more, the cap is lowered to three quarters of the machine's memory, and every result says so. A command-line task whose cases do not fit under the lowered cap is refused with a JSON diagnostic. On this repository's measuring machine the Docker machine has 1,959 MB, so three quarters are 1,469 MB. Both tasks ask for 1,408 MB, which that machine can hold a container to, so there the cap is given as asked.

In the real measurement Harbor makes the containers. What Harbor's task settings **can say** is memory and processors, no network, and a container of its own for judging. What they **cannot say** is a cap on processes, a size-capped memory-backed folder, and that a container gets no swap beyond its memory cap: Harbor sets the memory cap alone, and Docker then allows as much swap again. These go with the task in a container settings file that Harbor merges into its own. A converted task of either kind holds that file: the cap on processes and the swap setting for every task, and the cases' folder for a command-line task, with the same values the checking tool gives its containers. The file applies to the container the AI works in as well. Beside each of these containers Harbor runs a helper container of its own that has no caps; it runs nothing of the program's side.

The caps bound each container, not their sum. On the path through Harbor the container the AI works in and the container that judges the first step exist at the same time for a while (about 20 seconds in the review's runs), each capped at 1,408 MB on a Docker machine of 1,959 MB. When the machine itself runs out of memory the kernel may end a process in any container, and a case during which that happens in the judging container fails for memory though its program did nothing. How often that happens has not been measured.

## A fault of the judge

A problem that is not the program's is a fault of the judge: it cannot make the case's folder, cannot start the program as the judging user, cannot end the processes of the program's side, the task's settings are incomplete, the container has no caps that hold, the cases of a command-line test file cannot be listed, or such a file does not finish in its time. The judge then writes no result file (and removes one that is there), says what failed in `judge-fault.json` and ends with exit code 3. Without a result file the measuring harness treats the run as a failed judging and does not count it as a score of the program. The tool that checks prepared solutions also reports it separately, as "the judge itself failed".

A problem that comes from what the program left is no fault of the judge. If the case's folder cannot be removed, that case fails, and what is left is removed again when the next case starts. If the program's folder holds more than the judge handles, the step fails and a result is written.

A judge that is ended from outside cannot write `judge-fault.json` itself. The script that starts it does, where it survives: it removes any result file, writes that the judging ended without a result, with the exit code, and ends with an exit code that is not zero. The message is JSON first, like every other diagnostic.

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

After the last change the 70 solutions of the seven languages were judged inside the images once: 70 of 70 as written, 2,870 test outcomes. Before that change they had been judged five times each, 350 judgings and 14,350 test outcomes, and no solution's reward or single test outcome differed between the five. The run after the change differs from those five in no reward and in no test outcome.

### The web task

The judge of web tasks is the same code, so it changed here as well: it closes the tests before the build, puts nothing on a command line, runs the build and the served program confined, checks the caps of its container and ends in a judgement whatever is ended. The 78 prepared solutions were judged once more with the caps the converted task carries (1,408 MB of memory with no swap beyond it, 1,024 processes). Their judgement is the same as before in all seven languages.

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

**The web task's memory cap.** The task asked for 4,096 MB. The Docker machine of the measuring computer has 1,959 MB, so a cap of 4,096 MB bounded nothing there. The task now asks for 1,408 MB, which that machine can hold a container to. The value was chosen from the highest memory use of the container while a solution is built and judged, the cache of files on disk included, measured in these 78 judgings:

| | Python | TypeScript | Java | C# | Go | Rust | C++ |
|---|---|---|---|---|---|---|---|
| The right solution, build step | 504 MB | 243 MB | 400 MB | 469 MB | 494 MB | 425 MB | 560 MB |
| The right solution, change step | 503 MB | 167 MB | 249 MB | 274 MB | 340 MB | 210 MB | 506 MB |
| Highest over all prepared solutions | 504 MB | 243 MB | 400 MB | 469 MB | 494 MB | 425 MB | 560 MB |
| Room left under 1,408 MB | 904 MB | 1165 MB | 1008 MB | 939 MB | 914 MB | 983 MB | 848 MB |

The highest value of all is 560 MB, which leaves 848 MB. In none of the 78 judgings did the container reach its cap, and the kernel ended nothing. These figures include the cache of files on disk and differ from run to run: in the review the highest values were 529, 372, 282 and 511 MB for Python, Java, Rust and C++.

### On the path the real measurement uses

Converted tasks were run through Harbor with a prepared solution in place of an AI.

**The web task, exactly as converted.** The right solution in Python, in Java and in C++, and one vulnerable solution in Python. Every container Harbor made for the task, the one the solution is put into and the one that judges, had a memory cap of 1,408 MB, the same value for memory and swap together (so no swap beyond the cap) and a cap of 1,024 processes. The judge wrote the same caps into its result. Harbor's helper container had no caps. Of the 8 judgings, 7 have a counterpart in the direct check, and all 7 gave the same reward file, entry by entry.

**The command-line sample, in Python.** This was run before the last change to the judge and not again. The judging container Harbor made got the cap on processes and the size-capped folder from the settings file sent with the task, and the judge checked for them before it judged. The right solution, two vulnerable ones and one broken one were put in; five of their judgings have a counterpart in the direct check. In all 5 judgings no entry of the reward differed from the direct check.

### How far memory is really passed

Four programs that try to get past the limit were each judged twenty times in a fresh container, under a limit of 256 MB. The figure is the highest memory use the kernel kept for the container during the run, less everything the container held when the run began. It includes the cache of files on disk and the case's files.

| Program | Container 1,024 MB: lowest, median, highest | Container 640 MB: lowest, median, highest | Longest time between two measurements |
|---|---|---|---|
| One process, block after block | 257 · 269 · 284 MB | 257 · 266 · 276 MB | 5.8 ms |
| Eight processes, 64 MB each | 258 · 278 · 313 MB | 258 · 272 · 296 MB | 14.1 ms |
| One process with two threads asking for memory the kernel fills at once | 256 · 275 · 311 MB | 257 · 270 · 287 MB | 4.6 ms |
| Fifteen processes that take memory at the same moment | 258 · 281 · 311 MB | 260 · 276 · 312 MB | 19.5 ms |

In all 160 runs the case failed for memory, the cases before and after it passed, and the kernel ended nothing. The programs reached 256 to 313 MB. While a program kept both processors busy the judge did not measure every 0.002 seconds: the longest time between two measurements was 19.5 ms, and in the shortest run the judge measured 8 times before the program was ended. Every one of these cases failed by the judge's own measuring; the kernel's highest value fails no case.

The table gives the ranges measured in this run, not a bound. In the review a program of the same kind (fifteen processes starting together, limit 256 MB, container 1,024 MB) reached 334, 344, 387, 488 and 506 MB in five runs, 193 MB above the highest value of this table. The bound is the container's cap.

Under a limit the container cannot give, in the container of 640 MB, the only stop is the judge's own line 128 MB below the cap, at 512 MB of the container's memory. There the same four programs reached 455 to 502 MB above what the container held before. The container's highest use was 567 MB, 55 MB past that line and 73 MB below the cap. The kernel ended nothing in 80 runs. With that line taken out, the three programs that take memory without end filled the container, and the kernel ended a process in 60 of 60 runs. In all 60 it ended the program and the hidden test handed back what it had measured. In the full run before this one it went on, in 1 run of the two threads, to end the case runner, which is what the judge's marks make it choose after the program: then the hidden test hands nothing back, and the result says that the kernel ended the case runner. In all 60 the case failed for memory and the cases before and after it passed. The fourth program takes 512 MB and waits, so it does not fill that container; in its 20 runs the kernel ended nothing and the case failed at the time limit. A run without that line may be one of these two things and nothing else: the script ends in an error for a run that did not start or that gave no judgement.

### How long judging takes

The time to judge the change step of the right solution (50 tests) inside the image, in the run after the last change. It includes making the container and building. In the two full runs of the design experiment after the last change, one plain case took 6.81 and 12.5 ms for the program alone and 8.40 and 8.73 ms as a whole case of the judge (median of 200, Python); in the full run after this round's change, 7.49 ms and 9.46 ms. The figure for the program alone moves with what else the machine is doing.

| | Python | TypeScript | Java | C# | Go | Rust | C++ |
|---|---|---|---|---|---|---|---|
| The whole judging | 9.5 s | 4.0 s | 10.2 s | 6.2 s | 3.4 s | 4.8 s | 4.0 s |
| Of which the build | 0.0 s | 0.18 s | 1.62 s | 1.76 s | 0.51 s | 0.39 s | 1.33 s |

## The design decisions and their numbers

The list of attacks was written before the candidates were compared and is a file in the repository (`bench/experiments/attacks.toml`, 71 attacks). Every attack on it is a test, and a test of its own checks that. Every candidate met the same attacks in fresh containers of the Python image. Every container was capped at 1,024 MB of memory and 600 processes, and the limits of a case were 5 seconds, 256 MB of memory, 256 KB of output, 16 processes and 32 MB of files. Running `bench/experiments/cli_separation.py` gives the same tables again; the tables below are that script's full run before the last change to the judge, except the one row that says otherwise. The script was run in full again on the judge as it is now, and every other row came out with the same outcome. The measured values differ a little from run to run. The script and the memory experiment each have a quick mode that goes once through every table with the smallest counts. The automatic check runs both, and fails when a table no longer comes out.

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
| 400 files of 1 MB | 400 MB, passes | 41 MB, fails (files) | 32 MB, passes |
| 240 MB in files deleted while open | 240 MB, passes | 240 MB, passes | 32 MB, passes |
| 240 MB below 1,100 folders | 244 MB, passes | 244 MB, fails (files) | 32 MB, passes |
| 200,000 empty files | 200,000 entries, passes | 200,000 entries, passes | 19,998 entries, passes |

"Walking the folder", chosen before, did not see files deleted while open (240 MB written, and the case passed), and what got past it was bounded only by write speed and the case's time limit. 200,000 empty files passed with both of these candidates and used 452 to 454 MB of kernel memory. With the chosen one no attack writes more than the limit. The case of a program that stopped at the limit does not fail: it could not take more, so there is no need to fail it.

### What counts as memory

The first part of a cell is the memory the container used more before the program was ended, the second the judgement of its case. The limit is 256 MB.

| Attack | Adding up the processes' memory (chosen before) | **Chosen**: the whole container |
|---|---|---|
| One process | 272 MB, fails (memory) | 267 MB, fails (memory) |
| Eight processes, 64 MB each | 174 MB, fails (memory) | 270 MB, fails (memory) |
| Memory files mapped by no process | 1,005 MB, up to the container's cap, where the kernel ended the program; fails (memory). This row is the run on the judge as it is now; in the run before it the kernel ended the judging process itself and there was no result | 267 MB, fails (memory) |
| 320 MB of shared memory filled by short-lived processes | 330 MB, passes | 260 MB, fails (memory) |

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
| Forking without end | 598, 18.49 s, fails (processes) | 17, 0.08 s, fails (processes) | 16, 5.03 s, fails (time) |
| Threads without end | 26, 0.07 s, fails (processes) | 18, 0.07 s, fails (processes) | 16, 5.01 s, fails (time) |

Counting alone depends on the judge counting in time. In this run it ended the forking program only after 18.5 seconds at 598 processes, nearly the container's cap of 600; in an earlier run it had ended it at once. (The number seen is counted while threads start and end, and can read one high: 18 where the kernel allows 17.) A kernel cap holds it. What was chosen before was a kernel cap one above the limit, with the judge seeing that one and failing the case at once; it ended a program that forks without end within 0.1 seconds. Now only the kernel cap is left. Processes cannot pass the limit, and a program that tries without end is ended at its time limit (5 seconds) and fails. Judging it takes 5 seconds longer; in return one thing holds the bound, and a test fails without it.

### How a case is ended

| Candidate | A process that made itself a daemon | A process that keeps making children | Time taken |
|---|---|---|---|
| Signalling the process group | 1 survived | 0 | 0.0 s |
| Walking the list of processes and ending each | 0 | 0 | 0.013 to 0.015 s |
| **Chosen**: becoming that user and signalling all of its processes at once | 0 | 0 | 0.013 to 0.017 s |

### How what a case left is removed

| Candidate | 1,100 folders inside each other | 20,000 empty files |
|---|---|---|
| The standard library's removal | fails: too many open files | 0.145 s |
| **Chosen**: a removal that holds two folders open on its way down | removed, 0.063 s | 0.128 s |

### How often the judge measures

What one process taking memory as fast as it can reached before it was ended (limit 256 MB, twenty tries), and the processor the judge uses. The processor use is the processor time the judging process used while a program slept for two seconds, divided by those two seconds; the smallest and the largest of five.

| Interval | At the least | Median | At the most | Processor the judge uses |
|---|---|---|---|---|
| 0.010 s | 267 MB | 304 MB | 315 MB | 1.8% to 2.0% |
| 0.005 s | 265 MB | 278 MB | 304 MB | 2.2% to 2.5% |
| **0.002 s** | 261 MB | 269 MB | 277 MB | 3.8% to 4.6% |
| 0.001 s | 262 MB | 267 MB | 279 MB | 5.8% to 7.1% |

0.002 seconds stays. Its largest value is below those of 0.010 and 0.005 seconds. 0.001 seconds does not lower it further in this run and uses one and a half times the processor. The choice matters less than it did, though: the line that cannot be passed is now the container's cap, not the interval. The table compares intervals with one process that takes memory block after block. What programs that try harder reach is under "How far memory is really passed".

The processor figure depends on the state of the Docker machine more than on the judge. One measurement reads four of the kernel's counter files for the container, and what that costs is the kernel's work. At 0.002 seconds the judge used 3.8% to 4.6% in the full run after this round's change; in the round before, on one and the same code, 2.1% to 2.5% in two full runs and 9.9% in a short run a few hours earlier; the review measured 9.6% to 10.6%, and the run before the swap count was added 8.2% to 9.2%. That count is the fourth file. Read in a loop, one measurement costs 45 to 49 millionths of a second with it and 38 to 45 without. The decision does not rest on which of these days it is: in every run 0.001 seconds used about one and a half times the processor of 0.002 seconds or more, and lowered the largest value by 8 MB at most, in the last run not at all.

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

- **Memory can pass the limit between two measurements.** The line that cannot be passed is the container's cap, plus the swap a container is allowed (none on any path here). A program that was above the limit and came back below it between two measurements passes its case. The kernel's highest value for the container fails no case, because it counts the cache of files and a program that only reads would fail by it. The result names such a run only when it made a new high for the container, and every earlier high hides it: whatever the container held at one moment before, the cache of files left by the build or by an earlier case included, a later run can take for moments without being named. How far programs that try got above the limit while the judge measured is in "The numbers".
- **What a build writes into `/app` has no cap.** Only the size of one file and the build's 900 seconds bound it.
- **A program that stopped at a cap does not fail.** Writing files up to the limit and being refused more, or making processes up to the limit and being refused more, is not by itself a failure.
- **A plain user can see that the judge's processes exist, their state, how much memory they hold, when they started, their processor time and scheduling counters, the kernel limits they run under, and the list of mounted file systems.** None of it names a test.
- **The helper container Harbor runs beside a task has no caps.** It runs nothing of the program's side.

## Every protection, and the test that fails without it

For every protection there is one variant of the code without it (`bench/experiments/protections.toml`, 106 of them). `python experiments/check_protections.py` takes the protections out of a copy of the repository one at a time and runs that protection's tests. The tests catch 99 of them, and the check fails when a protection can be taken out without a test failing. The other 7 are second lines of defence: without them the attack still gets nothing, and the table says which other protections see to that. For those the check runs the attack tests without the protection and fails if one of them fails. A test also checks that this table and that file hold the same protections. The list was made from the code and not from this document: both judges, the converter and the script that starts the judge were read statement by statement, and every statement that is there only for separation, for a limit or for giving back got an entry. Where a protection sits in more than one place of the code, each place is an entry of its own: the text a variant takes out occurs exactly once in its file, and the check refuses an entry whose text occurs twice or not at all.

| Protection | The attack its test makes |
|---|---|
| Only root may enter the tests, from before the build | the build tries to copy the whole tests folder |
| Only root may enter the results | the program tries to read, in the results folder, the note a test that ran before left there |
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
| The judge ends a program that leaves the container too little memory | the case's limit is set above the container's cap and memory is taken to the end, with this stop and without it. **Second line of defence:** the attack gets nothing without it either, because a program the kernel ended for memory fails its case, and whatever the kernel ends the other cases are judged; this stop only keeps the kernel from having to choose. What the kernel chooses is settled by the marks the judge sets: the program first, then the case runner, the judge last. When it is the case runner, the hidden test hands nothing back, the case fails for memory and a new runner judges the rest; the test accepts both. It is run twice, with the judge measuring every 0.002 seconds as it does and with the measuring switched off, so that the case runner sleeps while the kernel ends the program. The measuring cannot end the program in either: the case's limit is above the container's cap |
| The program is ended at the case's time limit | the program hangs |
| The judge stops reading output at the limit | the program prints without end |
| A program that leaves a process behind fails its case | the program leaves a daemon and ends |
| After a run nothing of the program runs | the case after one that left a daemon counts its user's processes |
| When a case closes nothing of the program runs | a program leaves a daemon in a session of its own; one case leaves what it can for the next. **Second line of defence:** the attack gets nothing without it either, because every run of the program ends with all processes of the program's user ended, a hidden test can start the program only through a run, and between two files of tests the judge ends them once more |
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
| A memory cap the Docker machine cannot hold is lowered and reported | the container of a task is settled on a machine with 1 GB |
| Vetting containers have no network | the command that makes the container is looked at |
| The dependency folders are the image's pinned copies, whatever the app brought | the app brings its own copy of a dependency folder |
| Dropping what the app brought holds two folders open at any depth and takes any kind of file | 3,000 folders inside each other and a named pipe lie where dependency folders go, and the judge may hold 100 files open |
| Handing the program's folder to the build's user reaches every entry at any depth | 3,000 folders inside each other are in the program's folder when the judging begins |
| Removing gives up after a set number of entries | a folder of ten files is removed with a bound of five |
| Walking a tree gives up after a set number of entries | a folder of ten files is walked with a bound of five |
| A program's folder with too many entries fails the step, with a result | the program's folder holds more entries than the judge walks |
| Whatever sits where the results go is removed before anything runs | a folder, a link and an earlier fault lie where the results go |
| A result is moved into place, never written through a link | the program plants a link where the result goes. **Second line of defence:** the attack gets nothing without it either, because only root may enter the results, and whatever sat where a result goes is removed before anything runs |
| Calls made the way another kind of machine makes them are refused | the filter is read line by line |
| The second way of calling an x86-64 kernel is refused | the filter is read line by line |
| The judge refuses a machine it has no call filter for | the judge is told it runs on a machine it has no filter for |
| The judge refuses a cases' folder that may hold more than 20,000 entries | the judge is started with a cases' folder that has no cap on entries |
| The container's caps are checked for a web task as for a command-line task | the judge of a web task is started without a cap on processes, and with a memory cap above the machine's memory |
| The judge's result says how much its container may swap | the judge is given container descriptions with and without a swap allowance |
| Memory moved out to swap counts as memory | the judge is given a container description with 50 MB moved out to swap |
| A run that made a new high of the container's memory above the limit is named in the result | the program takes 400 MB and gives it back while the judge does not measure; the case passes and the result is read |
| The kernel ends the case runner before the judge | a hidden test reads the kernel's mark and score of the program, of the case runner and of the judge in a container of 640 MB |
| A run reads its starting values before the program is started | the case runner is watched: what the kernel had ended, the highest value and what the container held are read, and only then the program is started |
| The kernel's records of files that were looked at are left out of a case's memory | a program that holds 44 MB under a limit of 64 MB looks at every file of the image and at names that are not there until the records have grown by 13 MB |
| The kernel's records are left out of a case's memory only up to a quarter of its limit and 16 MB | the same program asks for 400,000 names that are not there; and programs hold on to such records in five ways, at limits of 64, 256 and 512 MB, up to well above the limit and the amount left out |
| A fall of the kernel's records takes nothing off what a case made them grow by | the case runner is shown records that rise by 10 MB, fall by 10 MB and rise by 10 MB again |
| The result says for each case how much of the kernel's records was left out and how much was counted | the result of the case that failed for asking for 400,000 names is read |
| Nothing of the program's side can lock memory | a build and a run each try to lock a range in both ways, to lock everything, and to map as locked |
| The calls that hand pages to a pipe or a socket without copying answer invalid argument | the filter is run on every such call for both kinds of machine; a build and a run make the four calls in a container that has no call filter but the judge's; and programs hand a file to pipes and sockets in four ways at limits of 64, 256 and 512 MB |
| The calls for asynchronous work are refused by the judge's own filter | the filter is run on those calls; a build and a run make them in a container that has no call filter but the judge's |
| A cryptography socket of the kernel cannot be made | the filter is run on the making of a socket of that family and of three others; a build and a run ask for one in a container that has no call filter but the judge's |
| A page of a file that the kernel cannot give back counts as memory | the judge is given a container description in which 128 MB of pages of files stand on neither list; and fourteen processes lock pages of a file under a judge without the limit on locked memory, at limits of 64, 256 and 512 MB |
| A case has one starting figure for all its runs | the case runner is shown a container that holds 30 MB more before a second run; and a first run leaves attributes and empty folders, a second holds memory beside them |
| A case is counted from the lowest figure it reached | the case runner is shown a container whose figure falls by 70 MB during a run and rises again; and a second run removes what the first left, or a run deletes the test's own files, and holds more than its limit |
| The making of a ring of the older asynchronous work answers that there is no such call | the filter is run on that call for both kinds of machine; a build and a run make it in a container that has no call filter but the judge's |
| No user but root can make a pair of terminals | a build and a run each ask for a pair of terminals |
| What the build left in the container's memory-backed folder is removed before the first case | a build leaves 60 MB there; the first case reads what the folder holds and how much shared memory the container has |
| A build during which the kernel ended a process fails | the build runs the container out of memory and then ends as if all were well |
| Processes are ended even when no new process can be made | three processes are to be ended while no process can be made |
| When no new process can be made, the processes are stopped before they are ended | three processes are to be ended while no process can be made, and the order of the signals is read: a stopped process makes no new ones, so every one is stopped before any is ended |
| Each judging draws a user number of its own | twenty numbers are drawn |
| The search for places any user may write gives up after a set number of places | six places any user may write, with a bound of three |
| What a build left that the judge gives up on fails the step, with a result | a build leaves 10,200 files any user may write |
| A writable place too deep for a path to name is closed off all the same | a build leaves a folder any user may write 3,000 folders down in the temporary folder |
| After the build everyone may read and run the program | a build makes the program and its folder readable by its owner alone |
| Opening the built program for reading follows no link out of it | a link in the program's folder points at a file outside it |
| After the build nobody but its owner may change the program, and no file of it lends its owner's rights | a build leaves a folder any user may write and a file that lends its owner's rights, also 3,000 folders down. **Second line of defence:** the attack gets nothing without it either, because no place any user could write stays writable, and the program's side can gain no rights, so a file that lends its owner's rights lends nothing |
| No user can rename or replace the tests and results folders from the folder above | the folder above the results is one any user may write, and the build tries to move the results folder aside |
| What the working user left running is ended before the build | a process of the working user is running when the judging begins |
| When the judge takes back what cases left, it ends the program's processes | a case runner is ended while the program of its case still runs; the next case counts its user's processes |
| Between two runners the judge takes back what the ended runner left | a case runner is ended while the program of its case still runs; the next case counts its user's processes |
| After each file of tests the judge takes back what its cases left | cases leave files and processes, a case runner is ended in the middle of a case, and programs break each limit in turn. **Second line of defence:** the attack gets nothing without it either, because after every run of the program the case runner ends what it left running, before every case anything left in the cases' folder is removed, and the judge takes back once more when the judging ends |
| Before the first file of tests the cases' folder is emptied | a case leaves files, and a case runner is ended in the middle of a case. **Second line of defence:** the attack gets nothing without it either, because before every case anything left in the cases' folder is removed |
| When the judge takes back what cases left, it empties the cases' folder | a case leaves files, and a case runner is ended in the middle of a case. **Second line of defence:** the attack gets nothing without it either, because before every case anything left in the cases' folder is removed |
| A served program that left its process group is ended before the next start | a served program moves its server out of the group of processes the judge started |
| What one start of a served program wrote is removed before the next | a served program fills its data folder 3,000 folders deep; the next start looks for it |
| A case during which the kernel ended a process fails, whatever its test says | a served program takes memory until the kernel ends it, in a test that would pass by itself |
| The case that was running when its runner was ended fails with that reason | a case runner is ended in the middle of the second of three cases |
| Before a new runner starts, what the last one left is taken back and a served program is started afresh | a case runner is ended while its program runs; a served program is ended by the kernel |
| The converted task carries a cap on processes to Harbor, for every kind of task | the converted web task's container settings are read |
| The converted task takes away the swap Harbor would allow beyond the memory cap | the converted web task's container settings are read |
| The converted command-line task carries the cases' capped folder to Harbor | the converted command-line task's container settings are read |
| The verifier's script says so when the judge ends without a result | the verifier's script is run with a judge that is ended |
| A result the judge was ended over does not stay | the verifier's script is run with a judge that is ended after it wrote a result |
| What a test reads of the program's files follows no link | the program leaves links where the test reads its files |
| A case's file is never written through a link the program left | the program leaves a link where the test writes a file |

## Where each thing is checked

| Check | Where | Numbers |
|---|---|---|
| All tests that need no image | automatically on every push | 941 tests pass, 5 min 56 s (time limit 15 minutes) |
| The tests in which a program, a build and a served web program attack, inside the Python image; the sample's 10 Python solutions, and those three times over; the attack that runs the container out of memory, judged 15 times over; programs that only read files that are not in the cache, of three kinds; programs that hold on to the kernel's records in five ways; programs that lock pages, hand them to pipes and sockets, leave things for a later run or a later case; the reading from outside, ten times over; what the Python runtime's library calls do under the filter; the quick mode of both experiment scripts | automatically on every push; the image is built on the spot | 159 tests pass, 16 min 54 s (time limit 40 minutes) |
| Taking each of the 106 protections out in turn | automatically on every push | the tests catch 99, the 7 second lines hold, 14 min 31 s (time limit 40 minutes) |
| The sample's 60 solutions in the other six languages | on the measuring machine only (building six more images takes long) | all 60 as written, once after the last change and five times over before it |
| The web task's 78 solutions in seven languages, with the caps the converted task carries | on the measuring machine only | all 78 as before |
| Judging through Harbor, the design experiment, the memory experiment | on the measuring machine only | the numbers above |

The times are the longer of the two runs of the automatic check on the commit that made this change. Before command-line tasks came in, the job that runs the tests took 1 min 9 s. Before the last change the three jobs took at most 5 min 53 s, 14 min 47 s and 14 min 58 s; the count of the first includes the tests main gained meanwhile. Each job ends within half of its time limit; the rest of the limit is room for a run in which tests fail.

The commands that check these again on the measuring machine:

```
cd bench
python -m dodambench.task tasks/cli-csv-json                           # the format
python -m dodambench.vet tasks/cli-csv-json --lang rust --image        # inside the image, with the real separation and caps
python -m dodambench.vet tasks/cli-csv-json --lang python --unseparated   # without separation, the project's own solutions only
python experiments/check_protections.py                                # taking each protection out in turn
python experiments/cli_separation.py                                   # the numbers of the design decisions
python experiments/memory_overshoot.py                                 # how far memory can be passed
```

## What is not checked yet, and the limits of this design

- **The automatic check builds the Python image only.** The judging of the other six languages is a number from the measuring machine. A change to those six images or solutions shows only when they are run there again.
- **Judging through Harbor was checked with the right solution of the web task in three languages and with the command-line sample in Python.** For the other languages only this is checked: the judge that runs inside the judging container is the same. The same settings file applies to the container the AI works in, and no real AI was run in such a container. Whether the caps are enough for what an AI builds there by itself is therefore not measured; it needs a real run.
- **The highest memory use the kernel keeps cannot be set back on these kernels.** A run that stays below an earlier high of the same container is seen only by the judge's own measuring.
- **The tests' reading of what a container really holds cannot show memory that the kernel charges to no container.** It shows everything charged to the judged container, in whatever form.
- **The per-language tests of what the runtimes' libraries do under the filter run on the measuring machine only**, like everything else in the six images the automatic check does not build. The Python one runs in the automatic check.
- **The filter that refuses the calls for what outlives a process is known for two kinds of machine:** this repository's measuring machine (ARM) and the machine of the automatic check (x86). On any other kind the judge refuses.
- **The way the container's memory is counted fits the kernel's current way (the second version of its group hierarchy).** On a machine with another way the judge cannot read its caps and refuses.
- **No real AI was run.** That the task can be solved from what the AI is told rests only on the prepared solutions having been written to that text.
- **The sample is one task.** Checks another subject needs, such as input reaching a shell in a log analyzer or a configuration checker, are added when that task is made.
- **The Rust image holds one file under its toolchain that any user can write.** The judge closes it when it judges. The image was not changed.
