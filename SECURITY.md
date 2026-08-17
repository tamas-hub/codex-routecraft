# Security policy

RouteCraft is orchestration guidance and configuration. It is not a sandbox, authorization layer, or secret-management system.

## Security-sensitive routing

Changes involving authentication, authorization, secrets, cryptography, payments, personal/regulated data, destructive migrations, or recovery logic should normally use a stronger implementation lane and fresh Sol review.

## Reviewer isolation

The `routecraft_sol_reviewer` profile requests read-only sandbox mode. Treat read-only as enforced only when the active Codex runtime exposes evidence that it actually applied the requested sandbox. If isolation is broader or unobservable, use behavioral read-only instructions and verify before/after repository state.

## Reporting a vulnerability

Please do not publish secrets or exploitable private-system details in a public issue. For problems in the RouteCraft repository itself, open a GitHub security advisory when available or contact the repository owner through GitHub.
