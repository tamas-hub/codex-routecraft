# Changelog

## RouteCraft Local Runtime 0.6.0 - 2026-08-24

- Added a privacy-safe schema-v3 Unified Collector while preserving the existing v1/v2 run and memory-task contract.
- Added optional Control Center transport, disabled by default and outside the core import boundary.
- Added Context Engine, conservative AGENTS preview/apply, deterministic Benchmark Lab, safe Security Hardener, and additive CLI surfaces.
- Added a narrow atomic Observatory endpoint migration helper that preserves token references, interval, and intentional OFF state.

## RouteCraft Memory Local 1.0.0 - 2026-08-23

- Added a separate local-only SQLite project memory product without replacing the existing Markdown Decision Store or its CLI.
- Added project and twelve-type memory CRUD, Japanese search/ranking, legacy imports, safe exports, conflict detection, backups, and confirmed restore.
- Added bounded Context Packs, six-file Handoff Packs, read-only Git summaries, and a Japanese-first Web UI bound only to `127.0.0.1`.
- Added an opt-in RouteCraft Loop bridge for registered-project Context injection and idempotent Git-metadata session summaries without transcript access.
- Isolated the optional bridge behind lazy loading, bounded combined Hook Context to 6,000 characters, and forced UTF-8 Hook streams on Windows without changing the disabled Loop path.
- Made SQLite FTS5 the primary search candidate source with Unicode substring fallback, and made JSON/JSONL batch imports one transaction.
- Made project-package imports one transaction, including project, memories, FTS rows, and conflict records, so late failures leave no partial import.
- Added a durable `(project_id, source_ref)` Loop-summary key with an immediate SQLite transaction so concurrent Stop hooks converge on one summary without rewriting legacy records.
- Connected project-package export/import to the localhost UI and emitted POSIX ZIP metadata so the macOS launcher retains mode `0755`.
- Refused to reuse a Markdown Decision Store directory as the Memory Local SQLite data directory.
- Added Windows/macOS ZIP builders, checksums, demo data, an offline retrieval evaluator, and v1 product/security/release documentation and tests.

## 0.5.1+codex.20260823011912

- Make every measured Memory task close explicitly with a learned record or a finite skip reason.
- Normalize free-form task labels into the fixed evaluation taxonomy instead of rejecting the CLI call.
- Link open evaluation tasks to a one-way session hash and make the lifecycle hook flag an unfinished first Stop without auto-learning.
- Add privacy-safe schema v2 execution telemetry for explicit task summaries and Recall/usefulness/Learn state; retain nullable compatibility for older runs.
- Add lifecycle, unsafe-summary, late-marker, duplicate-finish, and Stop-guard regression coverage.

## 0.5.1

- Add a windowless Windows notification-area host for Observatory heartbeat.
- Show ON, OFF, and delivery-error state through a small colored tray icon and context menu.
- Start the long-lived tray host once at user sign-in instead of launching a scheduled process every five minutes.
- Add explicit install/uninstall scripts and a contract that heartbeat persistence requires user permission.

## 0.5.0 - 2026-08-20

- Added an opt-in local RouteCraft Memory effectiveness evaluator.
- Added `off`, `recall`, and `full` task modes plus opt-in round-robin A/B/C assignment.
- Added post-verification usefulness labels for recalled records: useful, misleading, stale, or neutral by omission.
- Added live effectiveness metrics including useful-task rate, observed precision, useful-record MRR, cross-project reuse, cross-device reuse, time/tool-call/failed-hypothesis comparisons, estimated saved time, and Decision Compression Ratio.
- Added a local retrieval benchmark runner for Hit@K, Precision@K, Recall@K, and MRR without persisting raw benchmark queries.
- Added a coverage-aware RouteCraft Memory Score that withholds the 100-point score while less than 50% of weighted evidence is available.
- Kept evaluation logs local under `~/.codex/routecraft/evaluation/`; raw prompts, queries, transcripts, source code, logs, credentials, secrets, and absolute user paths are excluded.
- Added compact evaluation aggregates to RouteCraft Observatory heartbeats without sending repository names, task IDs, record IDs, queries, prompts, or local paths.
- Added English/Japanese evaluation methodology and regression coverage.

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
- Made Windows bootstrap resolve the packaged native `codex.exe` instead of launching an npm CMD shim through Python.

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
