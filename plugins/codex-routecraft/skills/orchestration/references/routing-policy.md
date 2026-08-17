# Routing policy

RouteCraft optimizes for **total delivery cost subject to acceptable correctness**, not minimum per-token price.

## Model intent

The GPT-5.6 family is treated as a capability ladder:

- Luna: efficient, high-volume implementation when the work is bounded.
- Terra: balanced intelligence and cost for work requiring more judgment or context.
- Sol: frontier reasoning for architecture, acceptance, and the most consequential decisions.

Reasoning effort is also a cost/quality dial. Use the lowest effort that is credible for the task. Reserve `max` for bounded work that is genuinely difficult and benefits from extra reasoning.

## Fast decision table

| Situation | Preferred execution | Lane | Review |
|---|---|---|---|
| Tiny local edit, simple fix | solo | root | self |
| Repetitive/mechanical multi-edit | delegate | luna-low | self |
| Clear routine feature | delegate | luna-medium | self |
| Hard algorithm under frozen design | delegate | luna-max | self or fresh Sol if impact is high |
| Multi-file integration with judgment | delegate | terra-medium | self |
| Wide/high-risk implementation | delegate | terra-high | fresh-sol-high usually |
| Architecture still unsettled | solo | root | self or fresh Sol |
| 2-3 independent bounded workstreams | parallel | mixed | risk-dependent |
| Security/payment/migration critical path | solo or delegate strong lane | root/terra-high | fresh-sol-high |

## Risk dimensions

Score the task qualitatively across these dimensions:

1. ambiguity: are product/technical decisions still unresolved?
2. blast radius: how much of the system can regress?
3. reversibility: is rollback easy and lossless?
4. sensitive data: auth, secrets, PII, payments, regulated data?
5. persistence: schema, migrations, durable formats, caches?
6. concurrency/recovery: races, retries, idempotency, rollback?
7. interface impact: public API, ABI, protocol, CLI contract?
8. verification strength: are deterministic tests available?
9. implementation boundedness: can exact files/interfaces be assigned?
10. delegation overhead: is the task large enough to justify a child?

A cheap lane is appropriate only when boundedness and verification are strong enough to compensate for lower capability.

## Effort policy

### Luna

- low: mechanical edits, narrow fixes, docs/tests with obvious expected output.
- medium: default routine implementation.
- max: difficult bounded implementation only; do not use max as the routine default.

### Terra

- medium: default judgment-aware implementation.
- high: broad context, higher risk, difficult integration, or meaningful blast radius.

### Sol

- high: root architecture and acceptance; fresh reviewer.
- stronger effort is user-controlled for exceptional tasks; RouteCraft v0.1 does not require a pinned `xhigh` child lane.

## Escalation

Escalate when evidence appears, not from vague anxiety. Valid signals include:

- worker reports unresolved interface ambiguity;
- tests expose hidden coupling;
- changed-file scope expands materially;
- migration/security/concurrency implications emerge;
- Luna needs architecture decisions rather than implementation decisions;
- repeated verification failures suggest misclassification.

Examples:

- luna-low -> luna-medium
- luna-medium -> luna-max or terra-medium
- luna-max -> terra-high when the problem is judgment/risk, not raw bounded difficulty
- terra-medium -> terra-high
- delegate -> root when architecture must be reopened
- self review -> fresh-sol-high when risk becomes high

Do not force an unnecessary retry on the same weak lane before escalating when the first result already proves misclassification.

## Cost interpretation

RouteCraft deliberately makes no fixed percentage savings claim. Savings depend on:

- fraction of work that can substitute into Luna/Terra;
- reasoning effort actually used;
- context size and tool usage;
- delegation packet overhead;
- parent verification overhead;
- fresh review frequency;
- retries/escalations.

The optimization target is fewer expensive duplicated reasoning cycles, not a marketing percentage.
