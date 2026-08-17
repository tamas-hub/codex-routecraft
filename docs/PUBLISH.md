# Publishing to GitHub

The intended public repository is:

`tamas-hub/codex-routecraft`

## One-time repository creation

Create a new **public** GitHub repository named `codex-routecraft` under `tamas-hub`.

Recommended options:

- Visibility: Public
- Initialize with README: No
- Add .gitignore: No
- License: No

The local tree already contains those files, so the remote should start empty.

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

## After publishing

Verify the public installation path:

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

Then install the companion RouteCraft agents as described in `docs/INSTALL.md`.

## Suggested GitHub metadata

Description:

> Sol-led adaptive orchestration for Codex: cheapest-viable GPT-5.6 routing, bounded parallelism, parent verification, and risk-gated fresh Sol review.

Suggested topics:

- codex
- openai
- gpt-5-6
- multi-agent
- orchestration
- ai-agents
- developer-tools
- cost-optimization
- coding-agent
