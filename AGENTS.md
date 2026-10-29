# Working on Dodam

Rules for AI agents and humans contributing to this repository.

- The charter (`docs/charter.ko.md`) is the source of truth. If a change conflicts with it, change the charter first, in its own commit, with the reason.
- Tests first: push the failing tests in one commit, let CI run on it, then push the implementation.
- Every design decision about syntax or semantics is settled by benchmark numbers, not taste. Record the numbers with the decision.
- Never weaken one of the charter's guarantees or relax a rule without a written reason and a benchmark comparison showing security metrics do not regress.
- Keep the core spec that is always given to AI under 10,000 tokens, measured with the tokenizer the charter names; the standard library summary and the rest of the spec are served through the MCP lookup.
- AI agents do the work through subscriptions and official CLIs, not the API. Gates and design decisions are judged by AI against criteria written beforehand and reported to the owner. The owner handles payments, contracts and accounts, and approves only changes that weaken a guarantee or relax a rule.
- Compiler diagnostics are JSON first; the human-readable message is rendered from it.
- Documents people read (Markdown, HTML, video) carry no internal codes or section numbers and never send the reader to another document; write the meaning in place.
- Paths, file names and identifiers are in English.
- Commit messages carry no AI co-author or attribution trailers.
