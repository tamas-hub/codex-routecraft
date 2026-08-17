# Contributing

Contributions are welcome.

## Principles

Changes should preserve these invariants:

1. Sol owns architecture and final acceptance.
2. Solo remains the default for small work.
3. Delegation substitutes for root implementation rather than duplicating it.
4. Parent verification is mandatory.
5. Parallel work has explicit disjoint ownership.
6. Cross-model routing is never claimed without a supported selection mechanism.
7. High-risk review is explicit and fresh-context.

## Validate

Run:

```sh
python scripts/verify.py
```

Also inspect shell/PowerShell installer changes carefully because they write into the user's Codex agent directory.

## Pull requests

Keep changes focused, explain compatibility assumptions, and include concrete validation evidence.
