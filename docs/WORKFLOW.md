# OpenMatrix Engineering Workflow

> Status: repository engineering workflow & review gates

## State Machine

```text
INTAKE → DISCOVER? → DESIGN → ISOLATE? → BUILD → VERIFY → REVIEW → CHECKPOINT
                                              ↑       |
                                              └─ DEBUG

DESIGN / ISOLATE / BUILD → WAIT_APPROVAL → original state
```

GitHub stores source, Issues, milestones, PRs, and test results. It is not an autonomous developer; every PR passes automated verification and maintainer review before merge.

## Artifact Mapping

| Artifact | State | Purpose |
|---|---|---|
| Repository | ISOLATE | Baseline, branches, commits, rollback points |
| Issue | INTAKE / DESIGN | One testable unit with goals and non-goals |
| Pull Request | BUILD / VERIFY / REVIEW | One Issue or tightly coupled change |
| GitHub Actions | VERIFY | Run repeatable tests only |
| Release / Tag | CHECKPOINT | Record objective, evidence, and verification status |

## Approval Gates

Stop before:

- selecting or migrating the main development base;
- adding a production dependency;
- changing core data structures or databases;
- executing an unfamiliar external script;
- opening a public port or deploying;
- making the repository public;
- modifying production secrets;
- merging, releasing, or publishing automatically;
- deleting data or performing irreversible operations;
- changing core PRD scope.

An approval applies only to the listed action and artifacts. It does not authorize similar future actions.

### Maintainer delegation policy

Routine component selection, reversible adapter changes, and minimal permissive dependencies still require passing test coverage, license verification, and rollback documentation.

Automatic approval requires mature maintenance, verified source, compatible license, Windows fit, bounded permissions, test coverage, and rollback. MIT, Apache-2.0, BSD, ISC, and MPL-2.0 may pass this gate. LGPL requires isolation/obligation review. AGPL, GPL, SSPL, BSL, non-commercial, source-available, custom, unknown, or conflicting licenses remain blocked.

The following gates cannot be delegated: merge, deploy, release, public visibility, production secrets, destructive or irreversible data changes, and acceptance without passing verification.

## Issue Intake Contract

Every executable Issue must define:

```text
objective:
non_goals:
allowed_files_or_paths:
acceptance_criteria:
verification_commands:
max_attempts:
branch_push_draft_pr_authorized:
risks:
rollback:
```

Research recommendations require Issue approval and acceptance verification before implementation.

## Isolation

- Feature work uses an isolated branch or worktree from the current remote base.
- Existing uncommitted user or Agent work must be preserved.
- Third-party repositories are cloned separately and are never OpenMatrix worktrees.
- Do not use privileged containers, Docker socket mounts, host networking, user secret mounts, or unpinned images.

## Build

- Use test-driven and incremental implementation.
- Use an integration-first product strategy: search for mature GitHub modules before implementing PDF, OCR, extraction, retrieval, grid, citation, highlighting, queue, or storage capabilities from scratch.
- Keep third-party code behind a narrow internal interface or adapter. Do not spread vendor-specific APIs across the application.
- A small wrapper, adapter, or configuration change is allowed when it stays inside the Issue scope, adds no unapproved production dependency, is reversible, and has verification.
- External components require source, activity, license, API stability, security history, permissions, transitive dependency, data-handling, platform-compatibility, performance, testability, and rollback review before use.
- Prefer existing repository modules, Git tooling, and GitHub Actions for the engineering workflow.
- Keep each batch atomic, reversible, and normally within 1-3 implementation files.
- Follow existing interfaces and avoid unrelated cleanup.
- New production dependencies require approval.

## Debug

- First local failure may be corrected in BUILD.
- A repeated failure enters DEBUG.
- DEBUG must produce new root-cause evidence or a new repair strategy.
- Do not repeat the same ineffective fix.

## Verify

- Run the Issue's focused tests and the repository test suite.
- Browser-facing behavior requires browser or documented smoke verification when practical.
- Failed verification returns to DEBUG and blocks the next feature.
- Exit code 0 is necessary but not sufficient; compare behavior against acceptance criteria.

## Review

- The final review is read-only.
- Security-sensitive changes involving files, keys, authentication, paths, or external input receive a security review.
- Any code change made during review invalidates prior verification and must return to BUILD/VERIFY.

## Checkpoint

Record:

- source root, branch, and commit;
- Issue and PR number;
- changed files;
- commands and exit codes;
- passed, failed, and unrun checks;
- known risks and blockers;
- rollback path;
- next authorized node.

## Controlled Continuation

Automatic continuation is allowed only when the next node is already authorized, prerequisites have evidence, budget remains, and no conflicting work exists.

Stop when scope grows, dependencies change, a secret or public action is required, the same failure repeats, verification cannot be identified, external side effects are uncertain, or budget is exhausted.
