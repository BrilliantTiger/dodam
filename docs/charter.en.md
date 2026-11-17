# Dodam Charter — Design Draft

- Version: v0.8 (2026-11-17)
- Status: Draft. We revise it as we review. An English version is made before going public.
- This is the English version. If it and the Korean version differ, the Korean version is the source of truth.
- It is written so that this one document alone tells you Dodam's goals, guarantees and schedule. The numbers and sources behind a decision are written next to the sentence they support.

## One-line definition

**A general-purpose language that AI writes correctly and robustly: secure and fast.** No human needs to read it. AI refines it for AI, and people look only at the result and a security report. The compiler enforces the security rules, and those rules only ever tighten. The first domain is the web, with one codebase for server and browser.

> A general-purpose language that AI writes correctly and robustly: secure, fast to run and fast to compile. No human needs to read it: AI refines it for AI, and people look only at the result and a security report. Security is enforced by the compiler, and the rules only ever tighten. Its first domain is the web, with one codebase for server and browser.

## Name

**Dodam (도담).** "Dodamdodam" describes a child growing up sturdy and without trouble. This language also grows by itself, without trouble (security incidents). Read in English, the `dam` at the end can also mean a wall that blocks dangerous data flows.

- Command: `dodam`
- File extension: `.dodam`

## Who the language is for

- **The users are AI coding agents.** People do not read code. What people look at is the result (the running program) and the security report the compiler produces (a list of the capabilities and risk points of the program).
- Whether a person can read it easily is not a design criterion. Syntax, diagnostics and tools are all chosen to be what AI uses best.
- **The makers are AI too.** AI agents do the design, implementation, measurement and judging. The owner handles only payments, contracts, accounts, and the approval of changes that weaken security. AI models are used only through subscriptions (official CLIs), one account per company, and never through the API.
- **The scope is general-purpose.** It is not tied to the web. Tools and libraries are widened in the order below, but the core design, such as the memory model, types and capabilities, is decided from the start to carry the last domain too (embedded, games, numerical computing).

| Order | Domain | Start |
|---|---|---|
| 1 | Web — API servers and browser screens | March 2027 (together with the minimal language) |
| 2 | CLI tools, data processing | December 2028 |
| 3 | Network services, systems programming | May 2030 |
| 4 | Embedded, games, high-performance numerical computing | After 1.0 |

### What the goals mean

- **Easy for AI to develop in** = AI writes it correctly and robustly.
  1. Accuracy — it is right from the first try, as the requirements say.
  2. Robustness — it does not break on boundary values, bad input or concurrent runs, and fixing it later does not break other places.
  3. Wrong code is caught at compile time, not while running.
- **Fast** = both run speed and compile speed. AI loops through compile, read the error, fix, dozens of times, so compile speed is AI development speed.
- **Secure** = the compiler enforces the ten guarantees listed under "Guarantees we must keep" below. Security is not traded for any other goal.
- **No duplication** = if two definitions differ only in name, only one of them exists.
- **Data does not wander on its own** = data is defined in one place and owned by one place.

### Order of judgment (draft)

When goals collide, choose in this order.

1. The ten guarantees we must keep — we do not give these up.
2. Accuracy and robustness of the code AI writes
3. Run speed
4. Compile speed

## Guarantees we must keep

The ten below are the core of Dodam. Code that breaks one **does not compile.**

| Guarantee | Content | Incident it prevents |
|---|---|---|
| **Memory safety** | No memory errors, no data races, no undefined behavior | Buffer overflow, use after free, bugs where values break under concurrent runs |
| **Capability declaration** | A function writes the side effects it uses in its signature, down to the resource scope: `Net(host)` · `Fs(path)` · `Db(table, read\|write\|ddl)` · `Env(name)` · `Secret(name)` · `Proc` · `Mail` · `Clock` · `Random` · `Log` …. Using a capability that is not written, or a resource outside the scope, is an error. Capabilities propagate to the caller. `Proc` (running another program), foreign function calls (FFI) and `trust` count as "all capabilities". Deno also treats running programs and foreign function calls as "the same as allowing everything". Release builds do not allow foreign function calls in user code. External libraries are wrapped by the runtime host and handed out as capabilities, one by one (the Roc platform way) | AI code that secretly sends data out, access beyond the allowed scope, side effects missed in review |
| **Execution location boundary** | Each capability has fixed execution targets where it may run. On the web, `Db` · `Fs` · `Secret` belong to the server and `Dom` belongs to the browser. Code that touches a capability the target does not have is not built for that target (server-only code cannot enter the browser bundle). `Secret<T>` cannot be serialized, so it cannot leave through a response, a screen or a log | Secret keys or DB code exposed to the frontend, code that uses a feature the execution target does not have |
| **External input must be validated before use** | External input (request body, forms, URLs, headers, files, command-line arguments, environment variables, responses from external APIs) is `Untrusted<T>`. It becomes `T` only after validation. Output points such as SQL, HTML, shell, URL and logs accept only safe types. The only way to turn a string into HTML is `trust`. A URL built from external input reaches `Net` only through an allow list or a public-address validation type (after DNS resolution, private and metadata addresses are blocked) | SQL injection, XSS, command injection, path traversal, log forgery, attacks that make the server call internal network addresses (SSRF) |
| **No hidden behavior** | No null (`Option`), no exceptions (`Result`), no implicit type conversion, no global mutable state, no macros, reflection, monkey patching or operator overloading | Behavior that cannot be known from the code alone, wrong inference by AI |
| **One way to do the same thing** | One syntax for doing the same thing. One official formatter, no settings | Different style in every piece of code, loss of consistency |
| **Reproducible builds and package capabilities** | A lock file is required. A package declares the capabilities it uses, and the user approves them. Using an unapproved capability fails the build. There are no install scripts, and package code does not run even in the build step. New versions are taken after a waiting period, and for each version only the added capabilities are approved separately. In the August 2026 crates.io attack, it was a new version of an old package, not a newly registered package, that spread through a build script. Adding a new dependency is a security change that a person approves. Unknown names and newly registered packages are not taken. 19.7% of the package names AI suggested did not exist (USENIX Security 2025), and attackers register such names first | Supply chain attacks, attacks aimed at package names AI made up, "it worked on my machine" |
| **No duplication** | Of definitions with the same normal form (names erased, syntax sugar expanded, dependencies replaced by hashes), only one exists. A data type with the same shape, and validation code or a DTO that copies a type by hand, are errors too. The rules are in the table right below | Bugs from fixing only one side, AI recreating code that already exists |
| **Data in one place** | Stored data is declared once, and the compiler generates the DB schema, types, serialization, validation, migration and sync. Computed values are declared with `derived`. The rules are the eight below | Different data definitions in each layer, drifted copies and caches, broken references, invariants that break under concurrent runs |
| **Resource limits** | Size limits on external input are on by default. Only linear-time regex engines, paging required for list responses, a default time limit on external calls. An `access public` function must declare a rate limit. Cloudflare's 27-minute outage in 2019 was caused by a single backtracking regex | Denial of service, resource exhaustion |

**Access policy is required too (web).** Every server function that a screen can call must write `access public` or `access auth(role)`. On top of that, **every stored data type must write a read policy and a write policy** (for example, "own records only"). A role check on each function alone cannot stop the attack of changing only an ID to read someone else's record. A missing policy is a compile error. It is checked at compile time and blocked once more by row-level security in the DB. Broken access control is the number one web security failure (OWASP 2025, 1,839,701 occurrences). OWASP's advice is also "deny by default" and "enforce record ownership".

**Passwords, secrets and cryptography.** A password is a `Password` type and is hashed only with Argon2id from the standard library. A `Secret` cannot be made from a string in code, and comparison takes constant time. The cryptography API offers only hard-to-misuse designs that manage their own nonces. Of the Rust cryptography code AI wrote, 57% of what compiled had misuse, and the mistake of reusing the same nonce kept repeating. A value marked as personal data cannot leave through a log.

**There is only one escape hatch.** When truly needed, a `trust rule "reason" { … }` block can step over one named rule. The capability check still applies. The reason string is required, and every `trust` block goes into the security report with its reason. It does not enter a release build until a person approves it. There is no way to turn off a security rule with a settings file.

**The threat model is set in the direction-fixing phase.** We decide whether the compiler still keeps its capability and boundary guarantees when it compiles malicious code, and if it does, we set the response deadlines and the announcement procedure before 0.1. Rust by default does not count attacks that come from compiling a malicious project as vulnerabilities. If Dodam says it keeps them, one hole in the checker becomes a vulnerability.

### No duplication — in detail

| Kind | What | Handling |
|---|---|---|
| Error | The normal-form hash of a new definition is the same as an existing definition (including dependency packages and the standard library) | Fix: "use the existing definition" (can be applied automatically) |
| Error | Code pieces with the same normal form (N tokens or more) appear twice | Fix: "extract a function" (can be applied automatically) |
| Error | Two data types in one package have the same field names and types | Allowed with `distinct "reason"`, and it goes into the security report |
| Error | A validator, DTO or serialization code that copies a type by hand | The compiler generates it from the type |
| Warning | Code that differs only in constants or the function it calls, code with large overlap | Fix: "add a parameter" (needs review). When evidence builds up, it is raised to an error. Rules only tighten and never loosen again |

- Normal form: names are erased, variables are replaced by positions, syntax sugar is expanded, dependencies are replaced by hashes, and then only fixed rewrite rules that always end are applied. The Unison language uses this method.
- Data types are told apart by name even when the shape is the same. But defining the same shape twice in one package is an error, as the table above says.
- N and the warning thresholds are set by benchmark.
- Because there are tuples, expression-based control flow and `Result`, the "extract a function" fix is always made mechanical. In existing languages, the reasons the same code could not be extracted into a function were mostly multiple return values (49%), different return types (30%) and conditional returns (16%).
- The check uses an incremental hash index, and it must fit within the compile speed goal of "check again within 1 second after changing one file".
- **Limit**: duplicates that behave the same but look different cannot be decided in general. This is theoretically undecidable (Rice's theorem), and existing detection tools also found only 0 to 1% of such duplicates. We measure this in the benchmark and publish it. Forcing similar code together creates wrong abstractions, so the warning level is not enforced. One study found that up to 71% of clones were actually good for maintenance.

### Data in one place — in detail

1. **One definition** — stored data is declared once. The compiler generates the DB schema (DDL), server and screen types, serialization, validation and cache keys. Hand-written DDL or a second schema file is an error, and the only way to write SQL directly is `trust`.
2. **One owner** — each piece of data has one owner: `store` (server DB), `local` (screen) or `external` (external API). Only the owner writes, and other places hold a read-only copy made by the compiler. The app is the only writer of its own DB, and at start-up it stops if the DB schema hash differs from the source. Prisma only warns on a mismatch and keeps running queries. Dodam does not.
3. **Derived values are declared** — a value computed from other data is declared with `derived` and must be a pure function. The system updates it incrementally or computes it on read (incremental computation such as DBSP or Differential Dataflow). Code that writes to a cache by hand is an error. The consistency level (`strict` · `snapshot` · `eventual`) shows in the type.
4. **References are types** — a `Ref<T>` can be obtained only by a lookup (returned as an `Option`) or by an insert in the same transaction. A delete policy (`restrict` · `cascade` · `nullify`) is required, and `nullify` works only on `Option` fields. It compiles to DB foreign keys and unique constraints. Ur/Web and Gel do it this way.
5. **Invariants live in the DB** — types that write value ranges (for example `Text(1..40)`) and invariants compile to DB constraints or serializable transactions. "Check that it exists, then write" breaks under concurrent runs, so it is rejected. A study of 67 Rails apps found that such app-level checks were not safe under concurrent runs, and 53 of 91 hand-written transactions in 8 web apps had correctness problems.
6. **Transactions** — every `server fn` runs in a serializable transaction and retries automatically on conflict. This is possible because there is no hidden behavior (global mutable state, exceptions), so functions are deterministic. Convex works the same way.
7. **The compiler makes migrations** — the deployed schema and wire types are kept in the lock file. When they change, the compiler makes the migration. Ambiguous changes such as renames and narrowing a range are written in the source, because AI does not answer interactive questions. A change that deletes data goes into the security report. The compiler also decides the deploy order (for additions, DB then API then screen; for deletions, the reverse). The Lamdera compiler is the closest precedent for enforcing migrations.
8. **The compiler makes sync** — a screen's subscriptions are updated by tracking the range the query read, and all subscriptions on one screen see the same point in time. Optimistic updates are allowed only for changes whose invariants hold without coordinating with the server. Which changes are like that can be decided in advance. The server is the source of truth, and screen code that does not handle the server rejecting a change does not compile.

- **Limits**:
  - Writing to the DB directly from outside the app is blocked only by DB permissions.
  - External API data can drift at any time, so it is shown in the type like `Stale<T>`.
  - Serializable transactions are slow when there are many writes.
  - Offline writes are left out of the first version.

## What people look at — the result and the security report

People do not read code. Instead the compiler produces a **security report** (`dodam report`). JSON comes first, and the screen people look at is a rendering of it.

What the report contains:

- All capabilities the program uses, and where each one is used
- Where data leaves — addresses it connects to, mail, logs
- Server functions a screen can call, and the access policy of each
- Where external input comes in, and the validation it goes through
- The read and write policy of each stored data type
- `trust` blocks and their reasons, and `distinct` marks that bypassed the duplication check
- Dependencies, packages and package capabilities
- Schema changes that delete data
- The rate limit of `access public` functions
- Results of tests and benchmark security tests

**The only thing a person approves is a security change.** If capabilities, outbound destinations, `access public` functions, loosened data policies, `trust` blocks, dependencies, package capabilities or data-deleting changes have increased since the last deploy, the report shows just that difference separately. The approved state is kept in the lock file, and if there is an unapproved change, the release build fails. The development build is not blocked. Changes that did not increase anything, and other code changes, need no approval.

**Report format.**
- For each capability, it holds the resource scope, an example call path, the entry point and the package. Capslock, Go's capability analysis tool, works this way.
- Each item has a stable identifier, so `dodam report --compare` compares against the approved version.
- Differences are shown as concrete items (host, table, path), not as categories. People ignore broad permission notices. When installing apps, 17% of users paid attention to the permission notice and 3% understood it (Felt et al., 2012).
- The versions of the compiler and of the rule set are written in as well.

Because people do not read the code, **a compiler bug is a security hole.**
- A conformance test suite that turns the spec into tests, and a slow reference interpreter that carries the spec over as it is, are built before the compiler. Every case where AI built large software leaned on a ready-made judge of correct answers (such as GCC's tests), and a new language has no such judge.
- The core rules (types, capabilities, taint, execution location) are modeled in Lean, and soundness and non-interference (secret and tainted values do not leak) are proved. Random programs are fed to both the model and the compiler's checker, and their verdicts are compared every day. AWS Cedar did this and found 4 bugs by proof and 21 by comparison testing.
- The checker, code generation and runtime are fuzzed. Generated code is compared with the reference interpreter for output, and the two backends are compared with each other. Cranelift also had critical advisories in 2021, 2023 and 2026 where a code generation bug broke the sandbox.
- Each guarantee has a test suite that tries to break it, kept together with the compiler.
- Output points such as HTML and SQL keep a runtime check as well, to block checker bugs once more.
- We take two external security audits. Before 0.1 it focuses on the endpoint decoder, the capability checker and the execution location boundary. Before 1.0 it looks at everything.
- Security reports are accepted only with a reproduction test, and there is no cash reward. In 2026, curl, Google and others stopped cash rewards because of fake AI reports.

## Design for AI

| Principle | Criterion |
|---|---|
| **The core spec that is always given is 10,000 tokens or less** | The core spec always given to AI is **10,000 tokens or less**. The tokenizer used to measure is written in this charter. The standard library summary and the detailed spec are served through MCP lookup. The line between core and lookup is set in the direction-fixing phase by accuracy measured when the spec is given at 5,000, 10,000 and 20,000 tokens. It is measured at every release, and a release that exceeds the limit is not made. Among existing specs, the only one within 10,000 tokens was Oberon-07 (9,800), which has no library (Gleam tour 15,002, Go spec 56,334, by the OpenAI tokenizer) |
| **Machine-readable diagnostics** | Every error comes out as JSON (kind, location, cause, fix, related spec). Only a few errors are given, starting from the root cause. At that spot, it lists the names that can be used, the expected type and the correct methods. In one experiment, fixing the diagnostics raised AI success from 14 of 50 to 36 of 50, and most of the effect came from listing usable alternatives (a text-based game environment, not a compiler). Each fix marks whether it can be applied automatically. The message people see is a rendering of the JSON |
| **A stable name for each error kind** | Each error kind has a fixed name, an explanation page and an example. AI can search for it and cite it |
| **An AI tool connection server is provided by default** | An MCP server that lets AI agents call the compiler directly is shipped with the language. Tools: `compile` · `check` · `check_partial` (checks code that is being written) · `test` · `explain_error` · `spec_lookup` · `interfaces` (a list of signatures and contracts instead of source) · `find_similar` (finds similar definitions) · `run_bench`. The reuse rate was 30.0% when AI was not given the code written earlier and 29.2% when given the full source, and it rose to 67.8% when given a list of signatures and contracts (RepoReuse, 2026) |
| **Reading one place is enough to understand** | Reading one file or one function is enough to understand it. Imports are explicit only, and there are no wildcard imports |
| **Version stability** | After 1.0 the syntax stays backward compatible. If it must change, an automatic migration tool comes with it. Even if AI mixes in old syntax, the compiler fixes it. The promise covers what is written in the spec, and changes that fix soundness, security or compiler bugs are the exception. Even those changes are handled through the edition system and automatic migration. Rust, Gleam and Nim also wrote such exceptions into their 1.0 promises |
| **Contracts in code** | Functions carry `requires` · `ensures` and property tests. They are checked by property tests from the start, and widened to Dafny-style SMT static verification. `assume` and trivial postconditions (`ensures true`) are rejected, because AI cheats verification this way. Property test feedback raised AI's first-try correctness by a relative 23 to 37% more than test-driven feedback |
| **Start from the look AI already knows** | Human readability is not weighed. We use a TypeScript, Rust or Kotlin family shape, do not change widely used conventions such as counting from 0, and use only ASCII. When Python was changed to count from 1, GPT-4 accuracy fell from 87.4 to 68.2 (NAACL 2024). AI-only syntax that gives up human readability is also compared as a candidate, but it is chosen only if it wins the benchmark. A syntax redesigned for AI saved about 10% of tokens, which is not a reason to give up familiarity |
| **Fast compile** | AI gets things right faster only if the compile and fix loop is fast. Goal (draft): re-check within 1 second after changing one file, and a clean build equal to or faster than Go |
| **Code being written is checked too** | Code in the middle of being written must be checkable so that AI can fix it while generating. Compiler feedback during generation cut compile failures from 65.9% to 13.1%, and type-constrained decoding cut compile errors by up to 75%. So declarations come first. Function parameters and return types are required, and type inference works only inside functions. There is no `any`, and even an empty literal has a type. The grammar is read by a standard LR parser and published as a machine-readable grammar file |
| **A collection of verified examples** | Dodam code that passes tests, and code translated from other-language tasks into Dodam and verified, are collected and released as reference and training material for AI. Fine-tuning on code verified this way roughly doubled the Racket and OCaml scores |

Diagnostic example:

```json
{
  "kind": "server-only-call-in-page",
  "message": "Function loadUser, which uses the server-only capability Db, was called directly from screen code.",
  "at": "app/profile.dodam:18:5",
  "fixes": [
    { "title": "Declare loadUser as a server fn and call it through the API", "auto": true, "edit": "…" }
  ],
  "spec": "Execution location boundary"
}
```

## Memory model — the first thing to decide

This is where safety, speed and AI accuracy collide hardest. We do no manual memory management, so there are four candidates.

| Candidate | Representative languages | Expected strength | Expected weakness |
|---|---|---|---|
| Tracing GC | Go · Java · C# | AI has less to worry about | Uses more memory and has latency spikes. Does not fit embedded |
| Ownership + lifetimes | Rust | Fastest, no data races | On the same problems the first compile failure is about 3.3 times Java's. Most are recovered by the fix loop, though |
| Automatic reference counting | Swift · Koka | No GC pauses | Handling reference cycles, cost of counting |
| Mutable value semantics | Hylo | References are not exposed, so almost no lifetime annotations | Few cases of real use |

The strengths and weaknesses in the table are expectations. The evidence so far is this.
- Among Rust compile errors, ownership and lifetimes are only 16.7%, and missing imports are more at 20.5% (ISSRE 2026).
- What is risky is the lifetime annotation written in signatures.
- There is not yet a study that compares the four candidates from the viewpoint of AI.

So the default hypothesis is "no lifetime annotations in signatures". After the direction-fixing phase (March to June 2027), we build a small prototype of each candidate, measure accuracy, robustness and run speed by benchmark, and choose. The concurrency model is tied to the same decision, because it is entangled with the memory model, stack maps and WASM output. The run speed of a prototype before tuning is shaken by implementation maturity (the first MMTk version that Ruby attached was about 4 times slower than the existing GC), so we use it in the decision only when the gap is large, and we write it separately as "room for tuning". The resulting numbers are recorded with the decision. Whichever candidate wins, there must be no data races. With data races, the capability check can be bypassed and any function can be called (a point the Go Capslock documentation makes).

## The first domain: web — server and screen in one codebase

- A server function and screen code can sit in one module. **The compiler looks at the capabilities and decides where to run.**
- When a screen calls a `server fn`, the compiler makes the API. The HTTP endpoint, serialization, input validation and CSRF defense are attached automatically.
- Arguments that arrive at the server automatically become `Untrusted`, and validation code is generated from the types written in the function declaration. A person cannot forget to validate.
- The screen has a **state, message, update, view** structure (the Elm way). The view is a pure function and returns a typed HTML tree. There is no hidden state.

A taste of the syntax (temporary — the real syntax is decided by benchmark in the syntax candidate comparison and the first version of the syntax):

```dodam
type SignupForm = { email: Email, name: Text(1..40) }

server fn signup(form: SignupForm) -> Result<UserId, SignupError>
  access public
  limit 5 per minute
  uses Db(users, write), Mail
{
  let id = Db.insert(users, { email: form.email, name: form.name })?
  Mail.send(form.email, welcomeMail(form.name))?
  Ok(id)
}

page Signup {
  state = { form: SignupForm.empty, status: Idle }

  update(state, msg) = match msg {
    Submit       => (state with { status: Sending }, call signup(state.form) then Done)
    Done(result) => state with { status: Finished(result) }
  }

  view(state) = form(onSubmit: Submit) [
    textField(state.form.email, label: "Email"),
    textField(state.form.name, label: "Name"),
    button("Sign up"),
  ]
}
```

What the compiler does with this code:

- `signup` uses `Db` and `Mail`, so it runs only on the server. Only the call code goes into the screen bundle.
- It sees `call signup(...)` and makes a `POST` endpoint and validation code.
- `Email` is a type that can be made only after validation. `Text(1..40)` gets a length check automatically.
- Without `access public` it does not compile. An `access public` function must also have a rate limit (`limit`).
- `Db(users, write)` is a scope that only writes to the `users` table. The `users` data type and its read and write policy are left out of this example.

## A structure that grows by itself

AI refines Dodam by itself. AI proposes syntax details, the standard library, diagnostics and rules, and AI merges them when the numbers improve. But a change that weakens security cannot be put in by AI alone. What people want from this language is security, and an AI that can change the security rules can also change them to be weaker.

AI also does the work of making the language. The owner handles only payments, contracts, accounts, and the approval of changes that weaken security. Gate judgments and core design decisions are made by AI against numeric criteria written beforehand, and are reported in a weekly report. Work does not stop waiting for the owner's answer.

### Three layers

| Layer | Content | Who merges |
|---|---|---|
| 1. Core | The ten guarantees we must keep, the rules of types, capabilities and taint tracking | AI merges changes that do not weaken a guarantee, with a design proposal and benchmark numbers, and reports them. A person approves changes that weaken a guarantee |
| 2. Rule set | Lint and security rules | AI merges additions and tightenings. A person approves loosening and deletion |
| 3. Knowledge and syntax details | Standard library, example collection, error messages, fixes, syntax details before 1.0 | AI merges |

### Conditions for AI to merge alone

All of the following must be met. If even one is not met, the change is not merged. Only changes that weaken a guarantee or loosen a rule are approved by a person.

1. No security metric gets worse. Security metrics are measured deterministically on a fixed set of programs and a fixed set of attacks (including hidden tasks).
2. Accuracy and robustness are within a predetermined non-inferiority margin (5 percentage points), and at least one of accuracy, robustness and speed improves. This judgment needs 71 to 193 tasks. Run speed and compile speed are measured on the same machine, repeated in shuffled order, and must not get worse by more than 5% (draft). Metrics measured on AI-written code are noisy, so if we judged by "none gets worse", even a change that changes nothing would be blocked about 45% of the time on one metric.
3. It does not touch the ten guarantees we must keep.
4. There is no loosening or deletion of rules, and no newly added item in the security report (capability, `trust` block, package capability).

### Improvement loop

1. Collect failures from benchmarks, fuzzers and real use.
2. Sort the failures by type.
3. AI proposes a rule, function or message that prevents the failure. **It submits a test that reproduces the failure with it.**
4. Run it over all the code collected so far and measure the false positive rate.
5. If the merge conditions are met, AI merges. If not, it is discarded or fixed again. Only proposals that weaken security wait for human approval.
6. Code that passes every check is added to the example collection and becomes reference material for the next AI.

### The principle that only tightens (ratchet)

- **Adding or tightening** a rule is done by AI once the merge conditions are met.
- To **loosen or delete** a rule, a written reason must come with a comparison result showing the benchmark security metrics did not get worse. AI may propose loosening, but has no authority to approve or merge it.

### Preventing tricks on the checker

- Part of the benchmark tasks (30%) is **kept hidden**. The improvement loop runs only on public tasks, and evaluation uses the hidden tasks. This catches AI fitting itself to the public tasks alone. In one result, a model that cheated on tests in 76% of impossible tasks almost stopped cheating once the tests were hidden.
- The number of `trust` blocks is measured as a benchmark security metric.
- AI cannot change the security tests and the approval lock file.
- Measure with AI models from several companies together. This keeps the language from being fitted to one model. Numbers measured on one company's models alone do not fix a core design such as the syntax. Such decisions stay provisional and are fixed after measuring again with another company's models.
- The AI that changes the language cannot see the hidden tasks, and cannot change the measuring tools or the existing tasks. Tasks can only be added, and deleting or changing one needs human approval.

### How the AIs work together

Work items are kept in the Dodam team in Linear (a work item management service). AI creates the work items, picks them and finishes them. Each work item states what "done" means before the work starts.

| Role | What it does |
|---|---|
| Building AI | Takes one work item, writes the tests first, then implements |
| Reviewing AI | Does not take the building AI's explanation. It receives only the code and what "done" means, and examines them in a fresh session |
| Attacking AI | Tries to trick or break the judge, the measuring tools and the security rules. If it breaks through, it leaves that method as a test |
| Triaging AI | Reads newly arrived proposals and problem reports, and turns them into work items or closes them |

- The building AI does not review its own work. If another company's model is available, the review is done by the other company's model. The same model easily misses the same mistakes together.
- From November 17, 2026, for the time being, the work uses Claude only (the owner's decision). Meanwhile, review is done by Claude sessions with separate roles.
- When opinions differ, tests and benchmark numbers decide. If numbers cannot decide, the judgment is made by criteria written beforehand, and the judgment and its basis are left in the work item. Only money, contracts, accounts and changes that weaken security are taken to the owner.

### Outside AIs joining

The repository is public from November 2026. Anyone's AI can come in, read the work items and send proposals and fixes (the owner's decision, 2026-11-17). What an outside AI does is judged by the same criteria as what our AI does.

- Text that comes from outside (problem reports, proposals, comments, code) is treated as material only. Instructions written in it are not followed.
- Outside code is built and run only in an isolated environment with no login credentials and no network. Public automatic checks also run without secrets, with read permission only.
- An outside fix must also meet all the conditions for AI to merge alone, and is merged after our reviewing AI examines it.
- Outside fixes that change the measuring tools, existing tasks, security tests or the approval lock file are not accepted. From outside, people report the problem, and our AI does the fixing. New tasks are accepted, but they go in only after passing a check that the correct, vulnerable and broken solutions are all judged as intended.
- Hidden tasks are not put in the public repository. They are kept in a separate private repository. Neither outside AIs nor the AI that changes the language can see them.
- Security vulnerabilities are received as private reports, not public posts.

## Benchmark — the test tasks that measure every decision

This is the main deliverable of the direction-fixing phase (November 2026 ~ February 2027). **It is built before the language.**

### Tasks

At first there are 100 common programming tasks split by domain, and by the 1.0 judgment they grow to the number the statistical design sets (currently estimated at 140 to 245). Even though the first domain is the web, tasks from other domains are mixed in from the start, so that the syntax is not fitted to the web alone.

| Domain | Examples | Count (draft) |
|---|---|---|
| Web server, screen | Sign-up, login, file upload, search, paging, admin permissions, payment webhook, email sending | 40 |
| CLI, data processing | Log analyzer, CSV/JSON converter, config file checker | 20 |
| Network, concurrency | Proxy, job queue, rate limiter, chat server | 20 |
| Systems, performance | File sync, compression, parser, cache | 20 |

What one task consists of:

- Natural-language requirements
- Hidden functional tests, hidden boundary tests (empty input, maximum values, wrong formats)
- Hidden security tests — injection, XSS, permission bypass, reading someone else's record, secret exposure, SSRF, path traversal, memory errors, resource exhaustion, credential storage. We run real attacks to check. When BaxBench measured this way, about half of the backend code that was correct was broken by attacks, for every model (ICML 2025). Even for the top model on the May 2026 leaderboard, about 35% of the correct answers were broken
- A performance load scenario
- One or two **change requests** — after the implementation is done, the requirements are changed, and after the fix we see whether the existing tests break.

Besides tasks written from scratch, the task kinds include the following.
- Tasks that add a feature to an existing repository — the form where security failures happen most. On such tasks 57% of the features were correct but only 11.8% were safe (SusVibes)
- Schema change tasks
- Tasks that check whether invariants break under concurrent runs
- Tasks that tempt the AI to use a package that does not exist

Tests are run **from outside, independent of language.** They look only at HTTP requests, browser actions, command-line input and output, and file results, so every language is measured with the same tests.

### What we compare against

Include a language that is likely to be number one for each goal.

- **Core 7 (measured every time)**: Python · TypeScript · Java · C# · Go · Rust · C++
- **Reference 11 (measured once at 0.1)**: C · Kotlin · Swift · Zig · PHP · Ruby · Elixir · Haskell · OCaml · Gleam · MoonBit

| Goal | Likely number one |
|---|---|
| Most AI training data | Python · TypeScript |
| Security | Rust |
| Run speed | C++ · Rust · Go |
| Compile speed | Go |
| Mainstream web practice | Java · C# · PHP |
| Close competitors | MoonBit · Gleam |

- Web tasks are fixed to the one most-used framework of each language (for example Next.js for TypeScript, Spring Boot for Java, FastAPI for Python).
- Combinations that cannot be used in that domain are left out (for example browser screen tasks only for languages that run in the browser).
- The existing-language baseline (10 tasks, 630 runs) and the Dodam measurement are solved 3 times each by 3 or more AI models. The 0.1 and 0.3 measurements are 3 models once each, and the 1.0 judgment has only the number-one language for each goal solved twice each by 3 models. The 11 reference languages are measured once at 0.1 with 30 tasks on 1 model. Measuring 18 languages × 100 tasks × 3 models × 3 times (16,200 runs) at every stage, as the first charter did, would take about 500 agent working days of subscription capacity and would stop other work for more than seven months.
- AI models are run through each company's official CLI, unmodified, at a pinned version, with a subscription login. There is one account per company. Google models are used only after Google allows unattended runs in writing, and until then the 3 models are filled from two companies. While there is no OpenAI subscription (for the time being, from November 17, 2026), we measure with Claude models and write in the results table that it was measured with one company's models only. When OpenAI models come in, we measure their share too. Every language is measured with the same CLI for each model, so the language comparison is fair. The same model can differ by up to 17 percentage points in score depending on the runner, so we do not compare models with each other.

### What we measure

| Goal | Metric |
|---|---|
| Accuracy | First-try compile success rate, functional test pass rate, boundary test pass rate |
| Robustness | Number of existing tests broken after a change request, number of crashes found by fuzzing, number of fixes until it passes |
| Security | Security test pass rate (number of remaining vulnerabilities), number of `trust` blocks |
| Duplication | Share of commits containing a duplicate block of 5 lines or more (the GitClear way), duplication rate per 1,000 lines measured by the same external detector, share of existing features recreated after a change request |
| Data consistency | Number of queries broken after a schema change, number of invariants broken under concurrent runs |
| Run speed | Response time (p50 · p99), throughput per second, memory, start-up time, executable and browser bundle size |
| Compile speed | Clean build time, rebuild time after changing one file |
| Cost | Tokens and time AI used |

Run speed and compile speed are measured for every language on the same machine with the same load tool. The headline number is the probability of passing in one run (the average of 3 runs), and we also report the share that passed all three times. With 630 runs, the only differences between languages we can tell apart are about 15 to 30 percentage points or more, so the first measurement gauges rough position and spread, not a ranking of languages.

### Public and hidden, and the target

- **Of the first 100, 70 are public and 30 are hidden.** 30 hidden tasks cannot see overfitting smaller than about 11 to 14 percentage points, so we use them as an alarm for large overfitting. The 1.0 judgment uses new hidden tasks that the AI that changed the language has never seen.
- **1.0 target (draft)**: compare against the number-one language for each goal among the compared languages.
  - Security: fewer remaining vulnerabilities than the language with the fewest
  - Accuracy and robustness: equal to or higher than the highest language ("equal" means the lower end of the 95% confidence interval of the difference is above −5 percentage points)
  - Run speed: equal to or faster than Go, and within 1.5 times even when slower than Rust or C++
  - Compile speed: equal to or faster than Go

## What we will not do

- Tools and libraries for embedded, games and high-performance numerical computing (until 1.0. The core design is made to carry this domain from the start)
- Manual memory management
- Macros, reflection, user-defined operators
- Syntax compatibility with existing languages (we only borrow a look AI already knows)
- Building our own VM, or building a code generator from scratch (we borrow Cranelift and LLVM)
- Building our own database (we build on PostgreSQL)
- Turning off security by a setting
- Cash security rewards
- Using AI models through the API (we use subscriptions only)

## Technology choices (defaults)

| Item | Choice | Reason |
|---|---|---|
| Compiler implementation | Rust | It has rich compiler tooling, and the compiler itself is memory safe |
| Server and CLI output | Native machine code. Development builds use Cranelift, release builds use LLVM | Cranelift compiles fast and LLVM runs fast. We get both compile speed and run speed |
| Browser output | WASM + a thin JS glue layer | A format that runs close to machine code in the browser. DOM access goes through the JS glue layer |
| Isolated execution | WASM (WASI) | The capabilities declared in the language are blocked once more in the runtime environment |
| First database | PostgreSQL | The DB enforces constraints, row-level security and serializable transactions. Gel (formerly EdgeDB), which built its own DB, closed the company, and Gel Cloud ended its service on January 31, 2026 |
| AI model use | A subscription with one account per company and the official CLI (Claude Code · Codex CLI). Unmodified CLIs at pinned versions. Google only after it allows unattended runs in writing. No API | It costs little (the top plans are Claude Max 20x at 200 dollars a month, ChatGPT Pro at 200 to 500 dollars a month, Google AI Ultra at about 250 dollars a month). In exchange, the subscription limits (5-hour and weekly) set the speed of development and measurement. Two companies directly document how to log in to the unmodified official CLI with your own subscription and run it non-interactively. Using the subscription login information from another program, or relaying it for someone else, is forbidden, and Codex uses one login file at a time on a private runner that a person owns, for one job at a time. Using several accounts to raise the limit breaks the policies of all three companies, so capacity is raised only with a higher plan. Google stopped allowing Gemini CLI on personal plans from June 18, 2026, and its successor Antigravity CLI does not support being wrapped and used by another program |
| License | MIT OR Apache-2.0 | The same dual license as Rust. It gives both patent protection (Apache) and simplicity (MIT) |
| Repository | Public repository `dodam` in the GitHub organization `BrilliantTiger`. Only the hidden tasks go in the private repository `dodam-hidden-tasks` in the same organization | Kept in an organization the owner already uses (the owner's decision, 2026-11-17). A dedicated organization for the language (`dodam-lang`, still free on 2026-11-17) is created and the repositories moved there if it becomes necessary. GitHub keeps forwarding the old addresses after a move. It is public from November 2026 to accept the participation of outside AIs. The owner's real name and contact details are never written in the repository, in commits or on work items. In Korea there is a prior software trademark ('DoDam' filed 2025-12-15, the company name '(주)도담솔루션' (Dodam Solution) registered), so we take a trademark attorney's opinion before the direction-fixing gate, and if the name must change, it changes before 0.1 |
| Work item management | The Dodam team in Linear | Our AI can create and change work items from the command line. The free plan allows up to 2 teams and 250 work items, and if we exceed that we use the 10-dollars-per-person-per-month plan. Outside AIs come in through GitHub, and the triaging AI links their posts to Linear work items |
| Website | `dodam-lang.org` (bought when the website opens) | A common form for language sites, like `rust-lang.org`. `dodam.com` · `.dev` · `.org` · `.app` · `.ai` · `.kr` are already registered |

TypeScript, the output of the first draft, was dropped. It is easy to borrow existing JS libraries, but it does not fit the run speed goal.

## Schedule

| Period | Name | Done when |
|---|---|---|
| November 2026 ~ February 24, 2027 | Direction fixing | Charter fixed, 30 benchmark tasks (20 web · 10 CLI and data processing) and the measuring tools (including performance measurement), a baseline measured with 7 existing languages (the numbers Dodam must beat), the 1.0 date reissued from the measured subscription capacity |
| ~ May 10, 2028 | Minimal language | Memory and concurrency model decision (early June 2027), first version of the syntax, conformance tests and reference interpreter, capability, taint and boundary type checker, checking of code being written, exact-duplicate check, schema and migration generation, security report (`dodam report`), native output (Cranelift) and browser output (WASM), JSON diagnostics, one server+screen example app |
| ~ April 4, 2029 | AI improvement loop and first public release | AI tool connection (MCP) server, editor support (LSP), 100 benchmark tasks, the AI-merged improvement loop running, screen sync (reads first), the CLI and data processing domain, focused security audit before 0.1, first public version (0.1) |
| ~ October 30, 2030 | Real use | Standard library (HTTP · DB · auth · JSON · files · concurrency), package manager (0.2), LLVM release build, network and systems domain, one real service made with Dodam run for more than 3 months (0 security incidents), 0.3 and the 1.0 scope freeze |
| ~ February 25, 2032 | 1.0 | Core frozen and the backward compatibility promise, full security audit before 1.0, 1.0 judgment on new hidden tasks, benchmark results published. After that, the embedded, games and numerical computing domain |

The dates are the middle estimates of the schedule simulation. We rolled the 140 days of work in the task breakdown forward one week at a time in order of earlier work first, with work running in the same week sharing the subscription capacity, with work lengths stretching and risks striking, and ran it 2,000 times. The date by which 1.0 is done with 80% probability is January 18, 2034. This assumes one account per company at the top plan, with the subscriptions together giving about 15 agent working days a week (the work one agent does in a day). If Google cannot be used (about 10 agent working days a week), the middle date slips to September 2033, and if capacity is twice this assumption, it moves up to January 2031. Raising the limit by adding accounts breaks company policy, so capacity is raised only with a higher plan. The first charter's "1.0 from 2029" was dropped because the simulation gave under 1% probability of finishing within that year. Even languages made by human teams took at the fastest 3.7 to 4.9 years to reach 1.0 (Elixir · Go · Mojo · Rust). We use the subscription capacity measured in early January 2027, and after that we rerun it at every gate and correct the dates.

From November 17, 2026, for the time being, we work with one Claude account only. In the same simulation (1,200 runs), if we go all the way with Claude alone (about 5 agent working days a week), the middle date for 1.0 is November 2038. If that account is shared with the owner's other personal work (about 4 agent working days a week), it is July 2040. In both cases the 80% date is later than October 2042, where the simulation stops. With two companies, the middle date is August 2033. For every month we use only Claude, it is about two weeks later than with two companies.

## Precedents to learn from

| Name | What we learn |
|---|---|
| MoonBit | A language and toolset built for AI collaboration. The closest competitor |
| Rust | Memory safety without GC (ownership). How hard the borrow checker is for AI is the key question when choosing the memory model |
| Go | Fast compile, a small language, one formatter with no settings |
| Swift | Automatic reference counting (ARC) |
| Hylo | Mutable value semantics — memory safety without references and lifetime annotations |
| Zig | No hidden control flow, no hidden memory allocation |
| Ur/Web · Links | Research languages with server and screen in one codebase that block injection and XSS by type. The compiler makes the DB schema and checks foreign keys by type. The lesson is that they could not build an ecosystem and did not become mainstream |
| Unison | Treats definitions that differ only in name as one by the normal-form hash |
| Convex | Deterministic functions + auto-retry transactions, subscriptions that track the range they read |
| Lamdera | Migrations enforced by the compiler |
| Gel (formerly EdgeDB) | Type links with delete policies. And the lesson of closing the company while building its own DB |
| DBSP · Differential Dataflow | Incremental maintenance of derived data |
| Storm · Sesame | Access policies attached to data |
| Go Capslock | Capability report format and version comparison |
| Trusted Types | A record of removing XSS by type. Across more than 130 Google services that applied it, there were 0 cases of DOM XSS |
| Gleam | A practical case of supporting both the server and JS with one language |
| Elm | A screen structure with no runtime exceptions |
| Koka | Effect (capability) system, Perceus reference counting |
| Pony · WASI | Capability-based security |
| Dafny · Verus · Lean | Contracts and static verification. AI's verification success rates were Dafny 82.2%, Verus 44.2%, Lean 26.8% |
| Vow | A small language that verifies contracts with a model checker |
| AWS Cedar | The way it guarded its verifier with a small Lean model + proofs + random comparison tests |

## Next steps (October 29, 2026 ~ February 24, 2027)

| Period | What to do | Done when |
|---|---|---|
| 10/29~11/1 | Research, charter and document commit, research for the direction-fixing phase (subscription terms and unattended runs, the measurement runner and task format, measuring tool licenses) | Commit, research report |
| 11/2~11/9 | Pay for subscriptions (Claude Max 20x · ChatGPT Pro) and secure the domain and GitHub organization, ask Google in writing about unattended runs (the owner), check the terms, fix the task format, start the measuring tool skeleton | A trial run works with Claude Code and Codex CLI, the format checker accepts a sample task |
| 11/2~11/23 | The measuring machine and run images for the 7 existing languages. To avoid spending money, the measuring machine is the owner's Mac mini (Apple M4, 10 cores, 16 GB memory) instead of a dedicated server (the owner's decision, 2026-11-17). Timing runs happen at night when little else runs, with the order of languages shuffled and repeated | The same program runs 10 times and the spread (the slowest time minus the fastest, divided by the median) is measured. Within 2%, this is done. Above it, the runs go up to 30 and the spread is measured again. If it is still above 5%, speed is not judged on this machine, and decisions that depend on speed wait. The speed rule (no more than 5% worse) stays as it is. When the speed difference between two versions cannot be told apart, it does not count as an improvement, and it does not count as shown to be no worse |
| 11/8~11/13 | One sample task (sign-up) all the way through | One run in TypeScript gives numbers |
| 11/15~12/11 | 10 tasks (6 web · 4 CLI and data processing), moving over BaxBench tasks, finishing the measuring tool | The correct implementation passes all the hidden tests, and the measuring tool outputs all the metrics |
| 11/17~11/30 | Opening the repository and preparing AI collaboration: Linear work items, the English version of the charter, rules for outside participation, public automatic checks, the hidden task repository | On the public repository, an outside AI can read work items and send proposals |
| 11/17 (finished; planned for 11/29~12/6) | Trial run and measuring subscription capacity, rescheduling November | The sign-up task was run once in each of the 7 existing languages. In one run the AI worked 79.2 seconds on average and the whole run took 223.9 seconds on average. For the subscription limits only an upper bound is known. The weekly utilization did not move by its display step (1 percentage point) over 6 runs, so one run uses less than 0.17 percentage point, and the tool did not report the 5-hour limit |
| 11/18~12/6 | One-night batch measurement: the same task with the same model 21 times (7 languages 3 times each), and with the largest model 7 times | We know how many percentage points the weekly utilization moves per run, the spread of a repeated run, and how many times the measured value a larger model works |
| 12/12~1/1 | 630 measurements of existing languages (3 AI models each write 10 tasks in 7 existing languages 3 times), collecting samples from public repositories | The existing-language baseline table: the numbers Dodam must beat |
| 1/2~1/11 | Failure classification, statistical design, reissuing the 1.0 date from measured subscription capacity | The corrected 1.0 date, the number of tasks needed for the 1.0 judgment |
| 1/8~2/3 | 30 tasks (9 split off as hidden), syntax candidate comparison (including the spec size experiment), reading the source of earlier implementations | Numbers for each syntax candidate |
| 2/4~2/17 | Direction-fixing result report | One report shows all the results |
| 2/18~2/24 | Final charter (spec size rule, threat model), direction-fixing gate judgment | Final charter commit |

The trial run (2026-11-17) finished 12 days before the day it was to start. As measured, the 630 baseline runs take 14 nights, running at night only (8 hours a night) and keeping the runner's guard (30 minutes of AI working time within 5 hours). If the AI working time of one run is twice the measured value it is 27 nights, and 53 nights at four times. To end inside the 21 nights of 12/12~1/1, that time must not exceed 2.14 minutes on average (1.62 times the measured value). In the schedule simulation (2,000 runs per case, one account per company) the chance that the baseline measurement ends by January 1 was 37% with the inputs before the trial run; after it, it is 53% as measured, 18% at twice and 0% at four times. Larger models and other tasks have not been measured yet, so the dates of December and of the rows after it stay, and they are judged again after the one-night batch measurement. The time freed by finishing early goes to the 10 tasks, to finishing the measuring tool and to the one-night batch measurement.

The trademark attorney's opinion is received by the end of January 2027 (the owner signs the contract).

## Version history

| Version | Date | What changed | Reason |
|---|---|---|---|
| v0.1 | 2026-10-28 | First draft | — |
| v0.2 | 2026-10-28 | Widened the scope from the web to general-purpose (the first domain stays the web). Added run speed and compile speed to the goals, defined "easy for AI" as accuracy and robustness, added a draft order of judgment. Added no data races to the memory safety guarantee, widened the server and browser boundary to execution targets in general, added command-line arguments and environment variables to external input. Added the fast compile principle. Added a new memory model part. Changed the compared languages from 3 to core 7 + reference 11, added robustness, run speed and compile speed to the measurements, and set the 1.0 target against the number-one language for each goal. Changed the output from TypeScript to native (Cranelift · LLVM) and WASM. Reflected the memory model decision and the domain expansion in the schedule | The owner's direction (2026-10-28): it must be compared with every language, a language that is secure and fast (both run and compile), the language easiest for AI to develop in = accuracy and robustness of development, beyond the web |
| v0.3 | 2026-10-28 | Decided that people do not read code (deleted "easy for people to review" from the order of judgment). Added the security report and the approval lock for security changes. A `trust` block cannot enter a release build before human approval. Turned the "look AI already knows" principle into a hypothesis and added an AI-only syntax candidate. Added the conditions for AI to merge alone, AI merging the addition and tightening of rules, and protection of hidden tasks and measuring tools | The owner's direction (2026-10-28): a language people need not look at, AI refines it by itself so that AI uses it best, for people only the result and the security of use matter |
| v0.4 | 2026-10-28 | Added three guarantees: no duplication, data in one place, resource limits. Required read and write policies per data type, added rules for passwords, secrets and cryptography, `trust` lifts only one named rule. Added resource scope to capabilities, SSRF blocking and HTML conversion limits to the external input rule, approval of new dependencies and rejection of unknown packages to the package rule. Added security report items and format. Added to design for AI: listing alternatives, AI tool additions, SMT verification and rejecting tricks, keeping conventions, checking code being written, the verified example collection. Added evidence and the "no lifetime annotations" hypothesis to the memory model. Added the `trust` count metric and lock file protection. Added the benchmark task kinds and the duplication and data metrics. PostgreSQL as the first DB. Reflected the schedule and precedents. Thresholds are set by benchmark. Removed all the numbers, section numbers and marks pointing to other documents that were attached to guarantees and principles, and wrote the evidence numbers in their place. Rescheduled the next steps by date through December 31 | The owner's direction (2026-10-28): always care about security, a language fully optimized for AI with no need for people to look, there must be no duplicate code, data must not wander on its own — about 50 sources were researched and reflected. In documents, do not put codes or references to other documents, and write so it is clear at a glance |
| v0.5 | 2026-10-29 | Rescheduled using the middle estimates of the schedule simulation (direction fixing 2027-02-03, minimal language 2028-01-12, 0.1 2028-08-16, 0.3 2029-10-31, 1.0 2030-12-25, 80% at 2031-12-17). The makers are AI too, and people handle only payments, contracts, accounts and the approval of changes that weaken security, so core changes and gate judgments are made by AI by numbers. AI models are used only through subscriptions (official CLIs). Cut the measurement scope to fit subscription capacity (18 languages once at 0.1, core 7 at 0.3, the number-one language for each goal at 1.0). Grew the tasks to the number the statistics set by the 1.0 judgment, and the 1.0 judgment uses new hidden tasks. Changed the spec rule to "core of 10,000 tokens always given + MCP lookup" and set the line by experiment. In AI self-merge, security metrics are measured deterministically, and noisy metrics are judged with a 5 percentage point margin. Foreign function calls are not used in release builds, and the runtime host wraps them per capability. Package code does not run at build time and new versions are taken after a wait. Wrote the scope and exceptions of the backward compatibility promise. The threat model is set in the direction-fixing phase. Added verification work (conformance tests, reference interpreter, Lean proofs, comparison tests, two external audits). Tied concurrency to the memory decision. Added the latest BaxBench numbers. The domain and organization are secured in the first week of November 2026 | The owner's decision (2026-10-29): build with subscriptions only and no API, judge the 1.0 date properly, keep human involvement to a minimum and let AI develop on its own. Basis: a 2029 1.0 had about a 1% chance in the simulation of finishing within that year, and even human team precedents took 3.7 to 4.9 years at the fastest. Among existing specs, only Oberon-07 was within 10,000 tokens. 16,200 measurements are about 500 agent working days of subscription capacity. The "none gets worse" judgment blocks even a change that changes nothing about 45% of the time (pre-development research, 2026-10-29) |
| v0.6 | 2026-10-29 | AI models are used with one account per company. The official CLIs are Claude Code and Codex CLI, used unmodified at pinned versions. Google only after it allows unattended runs in writing, and until then the 3 models are filled from two companies. Capacity is raised with a higher plan, not with accounts. Rescheduled with one account per company (about 15 agent working days a week) (direction fixing 2027-02-24, minimal language 2028-05-10, 0.1 2029-04-04, 0.3 2030-10-30, 1.0 2032-02-25, 80% at 2034-01-18). Moved the domain starts and the memory and concurrency model decision dates together | Research for the direction-fixing phase (2026-10-29): using several accounts to raise the limit breaks Anthropic's usage policy (in force 2026-11-12) ban on "getting around limits with several accounts", OpenAI's ban on getting around limits, and Google's non-support of automation that rotates accounts. The v0.5 dates assumed "two per company" (about 30 agent working days a week). Gemini CLI has not accepted Google AI Pro or Ultra requests since 2026-06-18, and the guidance of its successor Antigravity CLI says that wrapping it in another program is not supported on personal plans. Anthropic and OpenAI directly document how to run the unmodified official CLI non-interactively with your own subscription. Not one guarantee or rule was loosened |
| v0.7 | 2026-11-17 | Added how the AIs work together: work items are kept in Linear, roles are split into making, reviewing, attacking and triaging AIs, and the building AI does not review its own work. Added rules for outside AIs joining: outside text is treated as material only, outside code runs only in an isolated environment, the merge conditions are the same, the measuring tools, tasks and security tests cannot be changed from outside, hidden tasks are kept in a separate private repository, and vulnerabilities are received privately. The repository is public from November 2026 in the owner's GitHub organization, and the owner's real name and contact details are never written in the repository, in commits or on work items. The measuring machine is the owner's Mac mini instead of a dedicated server (the owner's decision not to spend money on a measurement server). For the time being we measure with Claude models only, and numbers measured on one company's models do not fix the core design. Added the dates for going with Claude alone to the schedule | The owner's decision (2026-11-17): put the Dodam team in Linear and let AI create and run work items by itself. Build an AI community where other AIs can join and complement each other, and accept outside AIs from now. For the time being only Claude works together. Schedule simulation (1,200 runs): with Claude alone to the end the middle date for 1.0 is 2038-11-10, and with two companies it is 2033-08-31. Not one guarantee or rule was loosened |
| v0.8 | 2026-11-17 | Recorded the trial run as finished, with its measured values (the AI worked 79.2 seconds per run on average, the whole run took 223.9 seconds on average, and for the weekly limit only the upper bound of less than 0.17 percentage point per run). Added the one-night batch measurement to the next steps. Wrote down the estimate that the 630 baseline runs take 14 nights at night only (as measured), 27 nights (twice) or 53 nights (four times), and the chance of ending by January 1 (from 37% to 53%, 18% and 0%). The dates of December and of the rows after it were not changed | The pilot measurement (2026-11-17): one task, once in each of the 7 existing languages, with one model. The trial run finished earlier than planned (11/29~12/6), and two inputs of the simulation changed: the AI working time the baseline measurement uses and the shortest it can be. The capacity the subscriptions give in a week could not be measured and was left as it was. The number that would move the dates earlier or later (how long a larger model works) has not been measured yet. Not one guarantee or rule was loosened |
