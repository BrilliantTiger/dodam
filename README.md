# Dodam

A general-purpose language that AI writes correctly and robustly: secure, fast to run and fast to compile. No human needs to read it: AI refines it for AI, and people look only at the result and a security report. Security is enforced by the compiler, and the rules only ever tighten. Its first domain is the web, with one codebase for server and browser.

> **Status: design phase.** There is no compiler yet. The current work is the charter and the benchmark that every design decision will be measured against.

## Why

AI now writes a large share of code, and the languages it writes in were designed for humans. Hidden behavior, implicit conversions, null, exceptions and global state make AI reason wrongly; missing input validation, leaked secrets and forgotten access checks slip through review. Safe languages tend to be slow or hard for AI to get right, and fast languages tend to be unsafe. Dodam makes those mistakes compile errors while aiming to be fast both to run and to compile.

## What Dodam guarantees

- **Memory safety** — no data races and no undefined behavior.
- **Declared capabilities** — a function lists the side effects it uses (`Db`, `Net`, `Secret`, `Mail`, …). Anything undeclared does not compile.
- **Server/browser boundary** — code that touches server-only capabilities can never end up in the browser bundle. Secrets cannot be serialized.
- **Taint tracking** — external input is `Untrusted<T>` until validated. SQL, HTML, shell and URL sinks accept only safe types.
- **Mandatory access policy** — every server function callable from the UI declares `access public` or `access auth(role)`.
- **No hidden behavior** — no null, no exceptions, no implicit conversions, no global mutable state, no macros or reflection.
- **Machine-readable diagnostics** — every error is JSON with a fix, and the compiler ships with an MCP server for agents.
- **A security report instead of code review** — people do not read the code. The compiler reports capabilities, where data leaves the program and every `trust` block; anything new must be approved by a person before a release build succeeds.
- **A language AI refines itself** — AI changes syntax details, libraries and rules and merges them when benchmark numbers improve. Only relaxing a rule needs human approval, with proof that security metrics do not regress.

## Name

*Dodam* (도담) comes from the Korean *dodam-dodam*: a child growing up healthy and sound, without mishap.

## Documents

- [Charter (Korean)](docs/charter.ko.md) — guarantees, AI-first design, the self-improving rule loop, DodamBench, roadmap. An English version will follow before the first public release.
- [README in Korean](README.ko.md)

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT license](LICENSE-MIT) at your option.
