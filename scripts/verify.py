#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-routecraft"
MEMORY = PLUGIN / "intelligence"
MEMORY_SCRIPT = PLUGIN / "scripts" / "routecraft_memory.py"
MEMORY_PACKAGE = PLUGIN / "scripts" / "routecraft_memory_lib"
EVALUATION_SCRIPT = PLUGIN / "scripts" / "routecraft_evaluation.py"
OBSERVATORY_SCRIPT = PLUGIN / "scripts" / "routecraft_observatory.py"

EXPECTED_VERSION = "0.5.1+codex.20260823011912"
EXPECTED_AGENTS = {
    "routecraft_luna_low.toml": ("routecraft_luna_low", "gpt-5.6-luna", "low"),
    "routecraft_luna_medium.toml": ("routecraft_luna_medium", "gpt-5.6-luna", "medium"),
    "routecraft_luna_max.toml": ("routecraft_luna_max", "gpt-5.6-luna", "max"),
    "routecraft_terra_medium.toml": ("routecraft_terra_medium", "gpt-5.6-terra", "medium"),
    "routecraft_terra_high.toml": ("routecraft_terra_high", "gpt-5.6-terra", "high"),
    "routecraft_sol_reviewer.toml": ("routecraft_sol_reviewer", "gpt-5.6-sol", "high"),
}

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


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
memory_reference = PLUGIN / "skills" / "orchestration" / "references" / "persistent-decision-layer.md"
evaluation_reference = PLUGIN / "skills" / "orchestration" / "references" / "memory-evaluation.md"
memory_skill = PLUGIN / "skills" / "memory" / "SKILL.md"
sentinel_path = MEMORY / ".routecraft-store.json"

required_files = [
    manifest_path,
    market_path,
    skill_path,
    memory_reference,
    evaluation_reference,
    memory_skill,
    sentinel_path,
    MEMORY / "README.md",
    MEMORY / "INDEX.md",
    MEMORY / "templates" / "case.md",
    MEMORY / "templates" / "candidate.md",
    MEMORY / "templates" / "rule.md",
    MEMORY_SCRIPT,
    EVALUATION_SCRIPT,
    OBSERVATORY_SCRIPT,
    PLUGIN / "scripts" / "routecraft_observatory_tray.ps1",
    PLUGIN / "scripts" / "install-observatory-tray.ps1",
    PLUGIN / "scripts" / "uninstall-observatory-tray.ps1",
    MEMORY_PACKAGE / "__init__.py",
    MEMORY_PACKAGE / "common.py",
    MEMORY_PACKAGE / "search.py",
    MEMORY_PACKAGE / "learning.py",
    MEMORY_PACKAGE / "git_sync.py",
    MEMORY_PACKAGE / "commands.py",
    MEMORY_PACKAGE / "cli.py",
    PLUGIN / "scripts" / "routecraft-memory.sh",
    PLUGIN / "scripts" / "routecraft-memory.ps1",
    ROOT / "docs" / "PERSISTENT_DECISION_LAYER.md",
    ROOT / "docs" / "PERSISTENT_DECISION_LAYER.ja.md",
    ROOT / "docs" / "MEMORY_EVALUATION.md",
    ROOT / "docs" / "MEMORY_EVALUATION.ja.md",
    ROOT / "tests" / "test_routecraft_memory.py",
    ROOT / "tests" / "test_routecraft_evaluation.py",
    ROOT / "tests" / "test_routecraft_git_privacy.py",
    ROOT / "tests" / "test_routecraft_observatory.py",
    ROOT / "tests" / "test_observatory_tray.py",
    ROOT / "tests" / "test_routecraft_source_guard.py",
    ROOT / "tests" / "test_routecraft_stdin_utf8.py",
    ROOT / "README.md",
    ROOT / "README.ja.md",
    ROOT / "LICENSE",
    PLUGIN / "hooks" / "hooks.json",
    PLUGIN / "scripts" / "routecraft_source_guard.py",
]
for required in required_files:
    if not required.is_file():
        fail(f"Missing required file: {required.relative_to(ROOT)}")

manifest = load_json(manifest_path) if manifest_path.is_file() else {}
market = load_json(market_path) if market_path.is_file() else {}
sentinel = load_json(sentinel_path) if sentinel_path.is_file() else {}
for example in [
    ROOT / "docs" / "examples" / "case-packet.json",
    ROOT / "docs" / "examples" / "promotion-packet.json",
]:
    if example.is_file():
        load_json(example)
    else:
        fail(f"Missing example packet: {example.relative_to(ROOT)}")

if manifest.get("name") != "codex-routecraft":
    fail("plugin.json name must be codex-routecraft")
if manifest.get("version") != EXPECTED_VERSION:
    fail(f"plugin.json version must be {EXPECTED_VERSION}")
if manifest.get("skills") != "./skills/":
    fail("plugin.json skills must be ./skills/")
keywords = set(manifest.get("keywords", [])) if isinstance(manifest.get("keywords"), list) else set()
for keyword in {"persistent-memory", "decision-retrieval", "cross-device", "github-source-of-truth", "memory-evaluation", "observability"}:
    if keyword not in keywords:
        fail(f"plugin.json missing keyword: {keyword}")

plugins = market.get("plugins", []) if isinstance(market, dict) else []
entry = next((item for item in plugins if item.get("name") == "codex-routecraft"), None)
if not entry:
    fail("marketplace missing codex-routecraft entry")
else:
    if entry.get("source", {}).get("path") != "./plugins/codex-routecraft":
        fail("marketplace plugin source path mismatch")
    if entry.get("policy", {}).get("installation") != "AVAILABLE":
        fail("marketplace installation policy must be AVAILABLE")

if sentinel.get("schema_version") != 1:
    fail("persistent memory sentinel schema_version must be 1")
if sentinel.get("purpose") != "RouteCraft persistent decision memory":
    fail("persistent memory sentinel purpose mismatch")

agent_dir = PLUGIN / "agents"
actual_agents = {path.name for path in agent_dir.glob("*.toml")}
if actual_agents != set(EXPECTED_AGENTS):
    fail(f"agent file set mismatch; expected {sorted(EXPECTED_AGENTS)}, got {sorted(actual_agents)}")

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

reviewer_path = agent_dir / "routecraft_sol_reviewer.toml"
reviewer = load_toml(reviewer_path) if reviewer_path.is_file() else {}
if reviewer.get("sandbox_mode") != "read-only":
    fail("reviewer must request read-only sandbox")

if skill_path.is_file():
    skill = skill_path.read_text(encoding="utf-8")
    for term in [
        "ROUTECRAFT PLAN",
        "execution: solo | delegate | parallel",
        "Parent verification is mandatory",
        "cheapest viable lane",
        "fork_turns: none",
        "fresh-sol-high",
        "Recall before rediscovering",
        "routecraft_memory.py",
        "Learn after verified meaningful work",
        "Measure memory effectiveness when enabled",
        "routecraft_evaluation.py",
    ]:
        if term not in skill:
            fail(f"SKILL.md missing required contract text: {term}")

if memory_skill.is_file():
    text = memory_skill.read_text(encoding="utf-8")
    for term in ["Safety contract", "Recall", "Learn", "Promote", "Sync"]:
        if term not in text:
            fail(f"memory/SKILL.md missing section: {term}")

if memory_reference.is_file():
    text = memory_reference.read_text(encoding="utf-8")
    for term in ["Pre-task recall", "Post-task learning", "Promotion", "Cross-device synchronization"]:
        if term not in text:
            fail(f"persistent-decision-layer.md missing section: {term}")

if evaluation_reference.is_file():
    text = evaluation_reference.read_text(encoding="utf-8")
    for term in ["Local-only evaluation", "Record recall without storing the query", "Scorecard", "Retrieval benchmark", "Experimental modes"]:
        if term not in text:
            fail(f"memory-evaluation.md missing section: {term}")

for template_name, kind in [("case.md", "case"), ("candidate.md", "candidate"), ("rule.md", "rule")]:
    path = MEMORY / "templates" / template_name
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8")
    for term in ["schema_version: 1", f'kind: "{kind}"', "evidence: []"]:
        if term not in text:
            fail(f"{path.relative_to(ROOT)} missing template term: {term}")

memory_python_files = [MEMORY_SCRIPT, *sorted(MEMORY_PACKAGE.glob("*.py"))]
source_guard_script = PLUGIN / "scripts" / "routecraft_source_guard.py"
python_files = [*memory_python_files, source_guard_script, EVALUATION_SCRIPT, OBSERVATORY_SCRIPT]
for path in python_files:
    if not path.is_file():
        continue
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        fail(f"{path.relative_to(ROOT)} compilation failed: {exc}")

implementation = "\n".join(path.read_text(encoding="utf-8") for path in memory_python_files if path.is_file())
for term in [
    "def recall_records",
    "def create_learning_record",
    "def cmd_promote",
    "def sync_store",
    "ensure_dedicated_git_root",
    "check_sensitive_text",
    "configure_private_git_identity",
    "ROUTECRAFT_GIT_EMAIL",
    "configure_utf8_stdin",
    "utf-8-sig",
]:
    if term not in implementation:
        fail(f"memory CLI package missing implementation term: {term}")

if EVALUATION_SCRIPT.is_file():
    evaluation_text = EVALUATION_SCRIPT.read_text(encoding="utf-8")
    for term in [
        "VALID_MODES = (\"off\", \"recall\", \"full\")",
        "observed_precision",
        "cross_project_useful",
        "cross_device_useful",
        "privacy_integrity",
        "benchmark-last.json",
        "insufficient-data",
        "round-robin",
    ]:
        if term not in evaluation_text:
            fail(f"Memory evaluator missing required contract text: {term}")

if OBSERVATORY_SCRIPT.is_file():
    observatory_text = OBSERVATORY_SCRIPT.read_text(encoding="utf-8")
    for term in ["evaluation_status", "summary", "--compact", '"evaluation":evaluation']:
        if term not in observatory_text:
            fail(f"Observatory missing evaluation telemetry contract text: {term}")

hooks_path = PLUGIN / "hooks" / "hooks.json"
hooks = load_json(hooks_path) if hooks_path.is_file() else {}
hook_events = hooks.get("hooks", {}) if isinstance(hooks, dict) else {}
for event in ("SessionStart", "Stop"):
    if event not in hook_events:
        fail(f"hooks.json missing Source Guard event: {event}")
hook_text = json.dumps(hooks, ensure_ascii=False)
for term in ["routecraft_source_guard.py", "CLAUDE_PLUGIN_ROOT", "commandWindows"]:
    if term not in hook_text:
        fail(f"hooks.json missing Source Guard contract text: {term}")
if source_guard_script.is_file():
    guard_text = source_guard_script.read_text(encoding="utf-8")
    for term in [
        "GITHUB SOURCE-OF-TRUTH POLICY",
        "default_visibility",
        "raw Codex transcripts",
        "allow_force_push",
        "decision\": \"block",
    ]:
        if term not in guard_text:
            fail(f"Source Guard missing required contract text: {term}")

for path in ROOT.rglob("*"):
    if path.is_file() and path.suffix.lower() in {".md", ".json", ".toml", ".py", ".sh", ".ps1", ".yml", ".yaml"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        if ("[" + "TODO:") in text or ("TODO" + "_PLACEHOLDER") in text:
            fail(f"unfinished placeholder in {path.relative_to(ROOT)}")

if errors:
    print("RouteCraft verification FAILED")
    for error in errors:
        print(f"- {error}")
    sys.exit(1)

print("RouteCraft verification OK")
print(f"- manifest: {manifest.get('name')} {manifest.get('version')}")
print(f"- marketplace: {market.get('name')}")
print(f"- agents: {len(EXPECTED_AGENTS)}")
print("- orchestration contract: present")
print("- persistent decision layer: present")
print("- memory evaluation: present")
print(f"- Python tools: compile ({len(python_files)} modules)")
