from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "plugins" / "codex-routecraft" / "skills" / "orchestration"


class McpCapabilityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (ORCHESTRATION / "SKILL.md").read_text(encoding="utf-8")
        cls.roles = (ORCHESTRATION / "references" / "role-contracts.md").read_text(encoding="utf-8")
        cls.policy = (ORCHESTRATION / "references" / "mcp-capability-policy.md").read_text(encoding="utf-8")

    def test_skill_runs_capability_gate_before_worker_ownership(self) -> None:
        self.assertIn("`references/mcp-capability-policy.md`", self.skill)
        gate = self.skill.index("## Gate MCP capabilities before delegation")
        ownership = self.skill.index("## Worker ownership")
        self.assertLess(gate, ownership)
        self.assertIn("MCP CAPABILITIES", self.skill[gate:ownership])

    def test_fixed_route_declaration_is_not_extended_with_mcp_fields(self) -> None:
        match = re.search(r"```text\nROUTECRAFT PLAN\n(?P<body>.*?)\n```", self.skill, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertEqual(
            [line.split(":", 1)[0] for line in body.splitlines()],
            ["execution", "lane", "review", "parallelism", "risk", "reason"],
        )

    def test_worker_and_reviewer_packets_require_capability_blocks(self) -> None:
        self.assertGreaterEqual(self.roles.count("MCP CAPABILITIES"), 3)
        self.assertIn("exact tool names", self.roles)
        self.assertIn("A child must not discover or use additional MCP tools", self.roles)
        self.assertIn("mutation authorization", self.roles)

    def test_policy_covers_five_servers_and_least_privilege_defaults(self) -> None:
        for server in ("GitHub MCP", "Filesystem MCP", "n8n MCP", "Playwright MCP", "Discord API MCP"):
            with self.subTest(server=server):
                self.assertIn(server, self.policy)
        self.assertIn("Use `none` by default", self.policy)
        self.assertIn("Prefer a single GitHub surface per task", self.policy)
        self.assertIn("Default state: disabled", self.policy)
        self.assertIn("isolated/headless", self.policy)
        self.assertIn("sending is an external mutation", self.policy)

    def test_external_mutation_stays_parent_owned_and_explicit(self) -> None:
        mutation = self.policy[self.policy.index("### `external-mutation`") :]
        self.assertIn("Requires explicit user authorization", mutation)
        self.assertIn("Remains parent-owned", mutation)
        self.assertIn("stop and obtain new authorization", mutation)


if __name__ == "__main__":
    unittest.main()
