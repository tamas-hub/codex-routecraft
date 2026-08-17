# Codex compatibility strategy

Codex multi-agent controls are evolving. RouteCraft therefore separates **routing policy** from **spawn mechanism**.

## Capability order

At runtime, use the strongest mechanism actually exposed by the current Codex surface.

### Path A: direct spawn overrides

If the spawn tool exposes `model` and `reasoning_effort`, use a fresh child (`fork_turns: none`) and explicitly request the lane model/effort. Put the lane behavior and full implementation packet in the child message.

This path provides the most flexible dynamic routing.

### Path B: named custom agents

If direct model/effort overrides are not exposed but `agent_type` is exposed, select one of the installed namespaced RouteCraft agents:

- `routecraft_luna_low`
- `routecraft_luna_medium`
- `routecraft_luna_max`
- `routecraft_terra_medium`
- `routecraft_terra_high`
- `routecraft_sol_reviewer`

Use `fork_turns: none` so the child gets a fresh bounded packet rather than a duplicated parent history.

### Path C: no verifiable lane controls

If neither explicit model/effort overrides nor a named agent selector are available, RouteCraft cannot prove that a cheaper model lane is active.

In that case:

- prefer `solo` for cost-sensitive work;
- a generic inherited-model child may be used only for genuine latency/concurrency benefit;
- do not report Luna/Terra savings;
- state that child model/effort could not be independently selected or verified.

## Why this exists

The Codex open-source runtime has exposed model/reasoning spawn overrides, while some 5.6-era desktop/tool-backed builds have temporarily exposed narrower schemas. Custom agents also currently require a companion installation outside the plugin package on some builds.

RouteCraft treats these as compatibility conditions, not reasons to silently route incorrectly.

## Verification language

Use precise wording:

- "verified" only when runtime/public metadata shows the effective role/model/effort;
- "requested" when the spawn call included the value but the runtime does not expose confirmation;
- "configured" when a TOML pins the value but the active child cannot be proven to have loaded it;
- "unverified" when none of the above establishes runtime use.
