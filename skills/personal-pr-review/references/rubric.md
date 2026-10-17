# Review rubric

A starting point: sections 1, 2, 5 and 6 apply to most codebases as they are. Sections 3 and 4 are
**where your repo's own rules go** — replace the examples with your team's conventions and the
pitfalls your codebase actually has. A rule only counts if it is written in the repo (CLAUDE.md,
CONTRIBUTING, ADRs) or agreed by the team; don't invent conventions the project doesn't have.

A real review, not a rubber-stamp: read the diff and trace the logic — never approve on the PR
description alone. **There is no "approve with nits".** Every violated item below is a finding, and any
finding means **Request Changes**. That covers real problems (a correctness bug, a missing regression
test, a real CI red, a data-isolation gap) and also small ones that often pass as nits (a leftover
comment, one missing edge-case test, a doc left out of sync, a deferred follow-up). Approve only when
nothing is left to do. When genuinely unsure, raise it as a finding rather than approving.

Tag each finding with its section — `[correctness]` `[tests]` `[conventions]` `[gotchas]` `[ci]`
`[reuse]` `[tooling]` — so a re-review knows where each open point came from.

**Reading surface.** The diff (`getBitbucketRepoPullRequestDiff`) is the primary read surface. Open
other files with `getBitbucketRepoFileContent` only for specific questions the diff raises (callers of
a changed function, the fixture a test should reuse, the schema a query relies on) — a few reads per
PR, not a crawl.

## 1. Correctness

- Does the change actually do what the PR/ticket says? Trace the logic in the diff, not the prose.
- Edge cases: null/empty/absent inputs, error paths, concurrency (locks, compare-and-set), off-by-one,
  timezone/date handling.
- No obvious regressions to callers of changed functions.
- No secrets, no debug logging left in, no commented-out code, no dead code introduced.

## 2. Tests

- **A bug fix ships with a regression test that would have caught it**, on the same branch, at the
  lowest level that reproduces it (unit, API, or browser e2e for a UI-reachable bug). A bug PR with no
  regression test is a finding.
- A new feature or endpoint is covered at the levels the repo expects (typically unit + API tests, and
  a happy-path e2e when it is reachable from the UI).
- Tests assert real behaviour (no tautologies), and mocks don't hide the thing under test.
- **Reuse fixtures instead of re-mocking in each test.** If a shared fixture or test helper already
  provides the setup, re-mocking it in `beforeEach` / a single test is duplication: point at the
  fixture to reuse.
- A signature change whose test doubles no longer match it (`toHaveBeenCalledWith`, provider overrides,
  spies) is a finding. Typecheck often can't see those, so CI can go green while runtime breaks.

## 3. Conventions (replace with your repo's)

Examples of the kind of rule that belongs here:

- **Feature flags**: user-visible work ships behind a flag with the production default the team agreed
  on, and fails to a safe default. If the reviewer can't see the flag service, say "flag not verified"
  rather than claiming it was.
- **PR size**: the repo's preferred size and hard cap (excluding generated files, snapshots and
  lockfiles), and what justification is required above it.
- **Commits and branches**: the agreed format (e.g. Conventional Commits, ticket key in the branch).
- **i18n**: new UI text goes through the translation helper, with the key present in every locale.
- **Shared types and generated clients**: use the shared enums/types and the generated API client
  instead of literals or hand-written requests; regenerate the client when the API surface changes.
- **No internal ids or personal data** in logs, commit messages, PR text or exports.
- **Comments**: minimal, only the non-obvious *why*. Comment noise that restates the code is a finding.

## 4. Repo gotchas (replace with your repo's)

The traps a newcomer to *this* codebase falls into. Typical examples:

- **Data isolation**: in a multi-tenant system, every tenant query is scoped explicitly, and new
  tenant-scoped data comes with its isolation rule and a test. A cross-tenant leak is a finding.
- **Background jobs**: serialization limits of the job runner, concurrency/deduplication semantics,
  where cleanup on failure belongs.
- **Database**: locks that need a dedicated connection; migrations that stay linear and additive (no
  unexpected destructive changes, no hand-edited migration metadata).
- **Infrastructure and secrets**: ordering constraints between deploy steps; secrets never in code or
  docs.

## 5. CI honesty (verify, don't assume)

State CI truthfully from the head commit's statuses, and never call a failing required check green. CI
**still running** is stated as pending (`<k> of <m> checks still running`) — never guessed green, never
a finding, never a reason to hold the review.

If the repo has a **documented known flaky check** and that is the only red, verify before calling it
the flake:

- Does the diff plausibly cause the failure, or touch the failing flow? A pure docs change almost
  certainly doesn't.
- Do the related suites that share the same specs pass? If they do, the failure is likely the flake.
- If the change is flag-gated, is **every** consumer gated? A gate that reads something independent of
  the flag is live immediately, and then the red is not a flake.

A **real** red (a check this change caused) is a `[ci]` finding. A verified flake is not a finding;
name it as the known flake in the comment.

## 6. Reuse over duplication

- **Extend an existing endpoint before adding a new one.** A new endpoint that does what an existing
  one already does (or nearly does, with a param or branch) is a finding: point at the endpoint to
  extend or the shared handler to reuse. A genuinely new capability with no existing home is fine.
- The same goes for services, helpers, DTOs and test setup (see the fixture bullet in §2): extend the
  shared abstraction instead of adding a parallel copy.

## 7. Tooling (only when `.claude/` or other dev tooling changed)

- No machine-specific paths, real personal or client identifiers, chat/workspace ids or secrets in
  shared tooling. Local-only values belong in gitignored config.
