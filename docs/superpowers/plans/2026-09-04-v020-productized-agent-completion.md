# v0.2.0 Productized Agent Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Coordinate the complete, reviewed delivery of the approved v0.2.0 terminal Agent product path and its live `echo 1` acceptance.

**Architecture:** Six focused plans serialize shared backend and migration work, freeze product contracts before UI work, then build the persistent constrained deployment and execute tiered acceptance. Fresh implementer agents perform TDD; each slice receives specification and quality review before its dependent slice starts.

**Tech Stack:** TermFlow Python/FastAPI/SQLAlchemy, Vue/TypeScript, Rust/Tauri, Docker Compose/OpenCode, pytest/Vitest/Playwright.

---

**Approved design:** `docs/superpowers/specs/2026-09-04-v020-productized-agent-completion-design.md`

**Repository policy for this execution:** Preserve all pre-existing dirty work.
Do not commit, push, reset, clean, tag, merge, or delete old deployment resources
without a separate user instruction. Newly created disposable acceptance
resources may be removed only by exact recorded ID.

### Task 1: Runtime Authority

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-runtime-authority.md`.
- [ ] Complete specification review, then code-quality review.
- [ ] Require migration head `0011` and focused runtime/auth gates to pass.

### Task 2: Product Setup API

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-product-setup-api.md`.
- [ ] Complete specification review, then code-quality review.
- [ ] Freeze generated setup/runtime/disclosure contracts before downstream work.

### Task 3: Cleanup Receipts and Startup Fencing

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-cleanup-startup.md`.
- [ ] Complete specification review, then code-quality review.
- [ ] Require migration head `0012`, truthful 202/204 semantics, and degraded-startup tests.

### Task 4: Terminal Agent Sidecar

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-terminal-agent-sidecar.md`.
- [ ] Complete specification review, then code-quality review.
- [ ] Require unit/type/build gates and terminal-session non-recreation proof.

### Task 5: Durable Live Deployment

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-durable-live-deployment.md`.
- [ ] Complete specification review, then code-quality review.
- [ ] Require static Compose/security/preflight gates before any stable deployment mutation.

### Task 6: Tiered Product Acceptance

- [ ] Execute every task in `docs/superpowers/plans/2026-09-04-v020-agent-acceptance.md`.
- [ ] Complete final whole-diff specification and code-quality reviews.
- [ ] Run the full repository verifier fresh.
- [ ] Update M4/M8/release evidence only for gates actually executed.

