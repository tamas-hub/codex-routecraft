# Publishing to GitHub

The public plugin repository is:

`tamas-hub/codex-routecraft`

## Public/private boundary

Publish plugin code, bundled empty templates, tests, and documentation here. Do **not** publish personal decision records from an external RouteCraft memory store.

Personal `cases/`, `candidates/`, and `rules/` belong in a separate private repository.

## Publish this checkout

From the repository root:

```sh
git remote add origin https://github.com/tamas-hub/codex-routecraft.git
git branch -M main
git push -u origin main
```

If `origin` already exists, inspect it before changing it:

```sh
git remote -v
```

## Pre-publish verification

```sh
python scripts/verify.py
python -m unittest discover -s tests -v
```

Check that the bundled decision store contains only its sentinel, templates, empty record directories, and explanatory index.

## After publishing

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Install companion agents and create a separate private memory store as described in `docs/INSTALL.md`.

## Suggested GitHub metadata

Description:

> Sol-led adaptive Codex orchestration with cheapest-viable GPT-5.6 routing, parent verification, and persistent decision memory across sessions and computers.

Suggested topics:

- codex
- openai
- gpt-5-6
- multi-agent
- orchestration
- ai-agents
- developer-tools
- cost-optimization
- persistent-memory
- decision-retrieval
