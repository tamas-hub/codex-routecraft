# Changelog

## 0.4.0 - 2026-08-20

- Added an idempotent multi-device bootstrap for Windows and macOS.
- Standardized RouteCraft source at `~/codex-routecraft` and private decision memory at `~/routecraft-memory`.
- Added a shared non-secret fleet configuration inside the tracked Decision Store sentinel.
- Added device-local profiles under `~/.codex/routecraft/` while keeping credentials, absolute paths, plugin cache, and generated agent files local.
- Added cross-platform tests, bootstrap syntax checks, and English/Japanese fleet-operation guides.
- Added an opt-in Source Guard for every Codex task on a managed device.
- Injected a private-by-default GitHub source-of-truth policy at session start and added a Stop gate for task-created uncommitted or unpushed source changes.
- Kept raw transcripts, credentials, databases, uploads, caches, and device-local configuration outside project repositories.
- Added local per-device Source Guard configuration and safe baseline fingerprints that preserve pre-existing dirty work.

## 0.3.2 - 2026-08-19

- Made redirected JSON stdin deterministic UTF-8 instead of inheriting a Windows legacy code page.
- Japanese `learn --input -` and `promote --input -` packets no longer require a process-specific UTF-8 override.
- Added regression coverage for plain UTF-8 and UTF-8 BOM input while Python starts with a legacy `PYTHONIOENCODING`.

## 0.3.1 - 2026-08-19

- Prevented GitHub GH007 push rejection when a workstation's inherited global Git email is private.
- Memory commits now use a neutral no-reply author and committer identity by default.
- Added `ROUTECRAFT_GIT_NAME` and `ROUTECRAFT_GIT_EMAIL` overrides for users who want their own public or GitHub no-reply identity.
- Added cross-platform regression coverage proving that a private global Git identity is not written into synchronized memory commits.

## 0.3.0 - 2026-08-19

- Added the Persistent Decision Layer with bounded recall, structured learning, and evidence-gated rule promotion.
- Added a standard-library-only `routecraft_memory.py` CLI and cross-platform wrappers.
- Added Japanese-aware retrieval, character budgets, local generated indexes, and decision-focused excerpts.
- Added a dedicated private Git store workflow for multi-computer synchronization.
- Added stable per-device record IDs, bounded push retry, pull/rebase synchronization, and conflict checks.
- Added safeguards against common secrets, oversized records, remote-helper execution, symlinks/non-Markdown payloads, accidental writes into the public bundled store, and syncing a product-repository subdirectory.
- Added transactional learning/promotion rollback, captured-Case promotion gates, case/candidate/rule schemas, examples, English/Japanese operations guides, and automated tests.
- Integrated pre-task recall and post-task learning into the RouteCraft orchestration contract.

## 0.1.0 - 2026-08-17

- Initial public-ready RouteCraft scaffold.
- Sol-led solo/delegate/parallel routing policy.
- Curated Luna/Terra fallback role matrix.
- Capability-aware direct-override vs custom-agent spawning.
- Mandatory parent diff/test verification.
- Risk-gated fresh Sol review.
- Cross-platform companion agent installers and repository verifier.
