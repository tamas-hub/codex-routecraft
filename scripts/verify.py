#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-routecraft"

EXPECTED_AGENTS = {
    "routecraft_luna_low.toml": ("routecraft_luna_low", "gpt-5.6-luna", "low"),
    "routecraft_luna_medium.toml": ("routecraft_luna_medium", "gpt-5.6-luna", "medium"),
    "routecraft_luna_max.toml": ("routecraft_luna_max", "gpt-5.6-luna", "max"),
    "routecraft_terra_medium.toml": ("routecraft_terra_medium", "gpt-5.6-terra", "medium"),
    "routecraft_terra_high.toml": ("routecraft_terra_high", "gpt-5.6-terra", "high"),
    "routecraft_sol_reviewer.toml": ("routecraft_sol_reviewer", "gpt-5.6-sol", "high"),
}

errors: list[str] = []

def fail(msg: str) -> None:
    errors.append(msg)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"JSON parse failed: {path.relative_to(ROOT)}: {exc}")
        return {}


def load_toml(path: Path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"TOML parse failed: {path.relative_to(ROOT)}: {exc}")
        return {}


manifest_path = PLUGIN / ".codex-plugin" / "plugin.json"
market_path = ROOT / ".agents" / "plugins" / "marketplace.json"
skill_path = PLUGIN / "skills" / "orchestration" / "SKILL.md"

for required in [manifest_path, market_path, skill_path, ROOT / "README.md", ROOT / "LICENSE"]:
    if not required.is_file():
        fail(f"Missing required file: {required.relative_to(ROOT)}")

manifest = load_json(manifest_path) if manifest_path.is_file() else {}
market = load_json(market_path) if market_path.is_file() else {}

if manifest.get("name") != "codex-routecraft":
    fail("plugin.json name must be codex-routecraft")
if manifest.get("version") != "0.1.0":
    fail("plugin.json version must be 0.1.0")
if manifest.get("skills") != "./skills/":
    fail("plugin.json skills must be ./skills/")

plugins = market.get("plugins", []) if isinstance(market, dict) else []
entry = next((p for p in plugins if p.get("name") == "codex-routecraft"), None)
if not entry:
    fail("marketplace missing codex-routecraft entry")
else:
    if entry.get("source", {}).get("path") != "./plugins/codex-routecraft":
        fail("marketplace plugin source path mismatch")
    if entry.get("policy", {}).get("installation") != "AVAILABLE":
        fail("marketplace installation policy must be AVAILABLE")

agent_dir = PLUGIN / "agents"
actual = {p.name for p in agent_dir.glob("*.toml")}
expected = set(EXPECTED_AGENTS)
if actual != expected:
    fail(f"agent file set mismatch; expected {sorted(expected)}, got {sorted(actual)}")

for filename, (name, model, effort) in EXPECTED_AGENTS.items():
    path = agent_dir / filename
    if not path.is_file():
        continue
    data = load_toml(path)
    if data.get("name") != name:
        fail(f"{filename}: name mismatch")
    if data.get("model") != model:
        fail(f"{filename}: model must be {model}")
    if data.get("model_reasoning_effort") != effort:
        fail(f"{filename}: effort must be {effort}")
    if not data.get("developer_instructions"):
        fail(f"{filename}: missing developer_instructions")

reviewer = load_toml(agent_dir / "routecraft_sol_reviewer.toml")
if reviewer.get("sandbox_mode") != "read-only":
    fail("reviewer must request read-only sandbox")

if skill_path.is_file():
    skill = skill_path.read_text(encoding="utf-8")
    required_terms = [
        "ROUTECRAFT PLAN",
        "execution: solo | delegate | parallel",
        "Parent verification is mandatory",
        "cheapest viable lane",
        "fork_turns: none",
        "fresh-sol-high",
    ]
    for term in required_terms:
        if term not in skill:
            fail(f"SKILL.md missing required contract text: {term}")

for path in ROOT.rglob("*"):
    if path.is_file() and path.suffix.lower() in {".md", ".json", ".toml", ".py", ".sh", ".ps1"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        if ("[" + "TODO:") in text or ("TODO" + "_PLACEHOLDER") in text:
            fail(f"unfinished placeholder in {path.relative_to(ROOT)}")

if errors:
    print("RouteCraft verification FAILED")
    for e in errors:
        print(f"- {e}")
    sys.exit(1)

print("RouteCraft verification OK")
print(f"- manifest: {manifest.get('name')} {manifest.get('version')}")
print(f"- marketplace: {market.get('name')}")
print(f"- agents: {len(EXPECTED_AGENTS)}")
print("- orchestration contract: present")
