# Security

Dodam is in its design phase, and there is no compiler yet. What exists today is the benchmark that every design decision will be measured against: the task format, the judge that builds and attacks AI-written programs in isolated containers, and the images for the seven existing languages it measures.

## Reporting a vulnerability

Report it privately through this repository's Security tab, with "Report a vulnerability". Do not put it in a public issue, pull request or discussion.

Say what is affected, how to reproduce it and what an attacker gains. The most useful report is a test that fails now and passes once the problem is fixed.

Things we especially want to hear about:

- A program under test that can read the hidden tests, change its own score or affect another run.
- A way for the agent being measured to reach credentials or hosts it should not reach.
- A benchmark task that a program can pass without doing the work.
- A rule or guarantee in the design that can be bypassed.

The project's AI agents and the owner read the reports. There is no cash reward.
