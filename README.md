# Dodam

A language for web applications written by AI and reviewed by humans: one codebase for server and browser, security enforced by the compiler, and rules that only ever tighten.

> **Status: design phase.** There is no compiler yet. The current work is the charter and the benchmark that every design decision will be measured against.

## Why

AI now writes a large share of web code, and the languages it writes in were designed for humans. Hidden behavior, implicit conversions, null, exceptions and global state make AI reason wrongly; missing input validation, leaked secrets and forgotten access checks slip through review. Dodam makes those mistakes compile errors.

## What Dodam guarantees

- **Declared capabilities** — a function lists the side effects it uses (`Db`, `Net`, `Secret`, `Mail`, …). Anything undeclared does not compile.
- **Server/browser boundary** — code that touches server-only capabilities can never end up in the browser bundle. Secrets cannot be serialized.
- **Taint tracking** — external input is `Untrusted<T>` until validated. SQL, HTML, shell and URL sinks accept only safe types.
- **Mandatory access policy** — every server function callable from the UI declares `access public` or `access auth(role)`.
- **No hidden behavior** — no null, no exceptions, no implicit conversions, no global mutable state, no macros or reflection.
- **Machine-readable diagnostics** — every error is JSON with a fix, and the compiler ships with an MCP server for agents.
- **Rules that only tighten** — AI proposes new rules from real failures; humans approve them. Relaxing a rule requires proof that security metrics do not regress.

## Name

*Dodam* (도담) comes from the Korean *dodam-dodam*: a child growing up healthy and sound, without mishap.

## Documents

- [Charter (Korean)](docs/charter.ko.md) — guarantees, AI-first design, the self-improving rule loop, DodamBench, roadmap. An English version will follow before the first public release.
- [README in Korean](README.ko.md)

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT license](LICENSE-MIT) at your option.
