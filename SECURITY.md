# Security policy

RouteCraft is orchestration guidance and configuration. It is not a sandbox, authorization layer, secret-management system, or complete data-loss-prevention system.

## Security-sensitive routing

Changes involving authentication, authorization, secrets, cryptography, payments, personal/regulated data, destructive migrations, or recovery logic should normally use a stronger implementation lane and fresh Sol review.

## Reviewer isolation

The `routecraft_sol_reviewer` profile requests read-only sandbox mode. Treat read-only as enforced only when the active Codex runtime exposes evidence that it actually applied the requested sandbox. If isolation is broader or unobservable, use behavioral read-only instructions and verify before/after repository state.

## Persistent decision memory

Use a separate **private** repository for personal decision memory. Do not place private project memory in the public `codex-routecraft` repository.

The memory CLI rejects several common secret/token/private-key patterns, but pattern matching cannot identify every sensitive value. Before storing a learning packet:

- remove credentials, tokens, private keys, passwords, cookies, and connection strings;
- remove personal or regulated data;
- avoid raw logs and complete transcripts;
- minimize private repository URLs and internal identifiers;
- store the decision and evidence summary, not the complete source material.

Synchronization stages only approved root files and direct Markdown records/templates, rejects symlinks and Git remote-helper syntax, and requires the memory store to be the root of a dedicated Git repository. This reduces accidental commits into an application repository but does not replace repository permissions or branch protection.

## Reporting a vulnerability

Do not publish secrets or exploitable private-system details in a public issue. For problems in the RouteCraft repository itself, open a GitHub security advisory when available or contact the repository owner through GitHub.
