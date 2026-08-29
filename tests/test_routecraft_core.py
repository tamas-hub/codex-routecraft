from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
SCRIPT = SCRIPTS / "routecraft-core.py"
sys.path.insert(0, str(SCRIPTS))
from routecraft_core import HostCapabilityRegistry, RouteCraftCore, RoutingMode, RoutingRequest, plan_route, resolve_routecraft_version


REGISTRY = {
    "schema_version": "1",
    "providers": [{"provider": "provider_a", "capabilities": {"available": True}, "hosts": [
        {"host": "host_a", "capabilities": {"native_routing": True}, "models": [
            {"model": "model_a", "capabilities": {"available": True, "native_routing": True}}
        ]}
    ]}],
}


class FakeMemory:
    def __init__(self) -> None: self.outcomes = []
    def recall(self, request): return [{"id": "remembered"}]
    def notify_outcome(self, outcome): self.outcomes.append(outcome)
    def notify_experience(self, experience): self.outcomes.append(experience)


class FakeEvents:
    def __init__(self) -> None: self.events = []
    def emit(self, event): self.events.append(event)


class RetryingHost:
    def __init__(self) -> None: self.calls = 0
    def dispatch(self, request, decision, executor=None):
        self.calls += 1
        return {"succeeded": self.calls > 1, "status": "succeeded" if self.calls > 1 else "rejected", "raw_output": "never exposed"}


class RouteCraftCoreTests(unittest.TestCase):
    def test_capability_registry_keeps_unknown_as_none(self) -> None:
        registry = HostCapabilityRegistry.from_mapping(REGISTRY)
        self.assertTrue(registry.capability("native_routing", provider="provider_a", host="host_a", model="model_a"))
        self.assertIsNone(registry.capability("native_routing", provider="future", host="future", model="future"))
        self.assertEqual(REGISTRY, registry.to_dict())

    def test_capability_registry_never_infers_from_unselected_children(self) -> None:
        value = {
            "schema_version": "1",
            "providers": [{"provider": "provider_a", "capabilities": {}, "hosts": [
                {"host": "host_a", "capabilities": {}, "models": [
                    {"model": "model_a", "capabilities": {"native_routing": True}},
                ]},
                {"host": "host_b", "capabilities": {"native_routing": True}, "models": []},
            ]}],
        }
        registry = HostCapabilityRegistry.from_mapping(value)
        self.assertIsNone(registry.capability("native_routing", provider="provider_a"))
        self.assertIsNone(registry.capability("native_routing", provider="provider_a", host="host_a"))
        self.assertTrue(registry.capability("native_routing", provider="provider_a", host="host_a", model="model_a"))
        decision = plan_route(RoutingRequest(task="work", mode="native", provider="provider_a", host="host_a"), registry)
        self.assertEqual("fallback", decision.status)
        self.assertFalse(decision.dispatch)

    def test_modes_preserve_legacy_and_native_falls_back(self) -> None:
        registry = HostCapabilityRegistry.from_mapping(REGISTRY)
        legacy = plan_route(RoutingRequest(task="work"), registry)
        self.assertEqual(RoutingMode.LEGACY, legacy.mode); self.assertEqual("legacy", legacy.authority); self.assertFalse(legacy.dispatch)
        advisory = plan_route(RoutingRequest(task="work", mode="advisory"), registry)
        self.assertFalse(advisory.dispatch); self.assertEqual("advisory_hints_only", advisory.reason)
        native = plan_route(RoutingRequest(task="work", mode="native", provider="provider_a", host="host_a", model="model_a"), registry)
        self.assertTrue(native.dispatch)
        fallback = plan_route(RoutingRequest(task="work", mode="native"), HostCapabilityRegistry())
        self.assertEqual(RoutingMode.ADVISORY, fallback.mode); self.assertFalse(fallback.dispatch)
        routecraft = plan_route(RoutingRequest(task="work", mode="routecraft", config={"risk_level": "high"}), registry)
        self.assertEqual("terra_high", routecraft.lane); self.assertIsNone(routecraft.model)

    def test_legacy_authority_precedes_registry_model_prechecks(self) -> None:
        legacy = plan_route(RoutingRequest(task="work", mode="legacy", provider="provider_a", host="host_a", model="future"), REGISTRY)
        self.assertEqual("legacy", legacy.authority)
        self.assertEqual("ok", legacy.status)
        # A malformed later registry must not alter the pre-registry legacy path.
        malformed_registry = {"schema_version": "not-supported", "providers": []}
        malformed = plan_route(RoutingRequest(task="work", mode="legacy", model="future"), malformed_registry)
        self.assertEqual("legacy", malformed.authority)
        self.assertEqual("ok", malformed.status)

    def test_requested_unavailable_or_unknown_model_fails_safely(self) -> None:
        registry = HostCapabilityRegistry.from_mapping(REGISTRY)
        unknown = plan_route(RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="future"), registry)
        self.assertEqual("invalid_configuration", unknown.status); self.assertFalse(unknown.dispatch)
        unavailable = json.loads(json.dumps(REGISTRY))
        unavailable["providers"][0]["hosts"][0]["models"][0]["capabilities"]["available"] = False
        decision = plan_route(RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a"), HostCapabilityRegistry.from_mapping(unavailable))
        self.assertEqual("requested_model_is_unavailable", decision.reason)
        provider_unavailable = json.loads(json.dumps(REGISTRY))
        provider_unavailable["providers"][0]["capabilities"]["available"] = False
        decision = plan_route(RoutingRequest(task="work", mode="routecraft", provider="provider_a"), HostCapabilityRegistry.from_mapping(provider_unavailable))
        self.assertEqual("requested_provider_is_unavailable", decision.reason)
        self.assertFalse(decision.dispatch)
        decision = plan_route(RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a"), HostCapabilityRegistry.from_mapping(provider_unavailable))
        self.assertEqual("requested_model_is_unavailable", decision.reason)
        self.assertFalse(decision.dispatch)

    def test_execute_uses_only_host_dispatch_and_memory_event_failures_are_best_effort(self) -> None:
        host, memory, events = RetryingHost(), FakeMemory(), FakeEvents()
        core = RouteCraftCore(registry=REGISTRY, host=host, memory=memory, events=events, max_retries=1)
        result = core.execute(RoutingRequest(task="work", mode="routecraft", task_id="task_1"), executor=object())
        self.assertTrue(result.succeeded); self.assertEqual(2, host.calls); self.assertEqual(2, result.attempts)
        self.assertEqual(1, result.evidence["memory_recalled_count"])
        self.assertNotIn("raw_output", json.dumps(result.to_dict()))
        self.assertTrue(result.evidence["payload_redacted"])
        self.assertEqual(2, result.evidence["events_emitted"])
        self.assertEqual(2, len(events.events)); self.assertEqual(2, len(memory.outcomes))

    def test_invalid_decision_is_not_dispatched_or_reported_as_not_dispatched(self) -> None:
        host, memory, events = RetryingHost(), FakeMemory(), FakeEvents()
        result = RouteCraftCore(registry=REGISTRY, host=host, memory=memory, events=events).execute(
            RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="unknown")
        )
        self.assertEqual("invalid_configuration", result.status)
        self.assertFalse(result.succeeded)
        self.assertEqual(0, host.calls)
        self.assertEqual(0, result.evidence["events_emitted"])
        self.assertEqual([], events.events)

    def test_memory_and_event_errors_do_not_prevent_host_execution(self) -> None:
        class BrokenMemory:
            def recall(self, request): raise RuntimeError("private path must not surface")
            def notify_outcome(self, outcome): raise RuntimeError("no")
            def notify_experience(self, experience): raise RuntimeError("no")
        class BrokenEvents:
            def emit(self, event): raise RuntimeError("no")
        class WorkingHost:
            def dispatch(self, request, decision, executor=None): return {"succeeded": True, "status": "succeeded"}
        result = RouteCraftCore(host=WorkingHost(), memory=BrokenMemory(), events=BrokenEvents()).execute(
            RoutingRequest(task="work", mode="routecraft")
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(0, result.evidence["memory_recalled_count"])

    def test_events_use_observed_host_facts_and_null_when_actual_model_is_absent(self) -> None:
        class ObservedHost:
            def dispatch(self, request, decision, executor=None):
                return {
                    "succeeded": True, "status": "succeeded", "actual_model": "model_b",
                    "actual_reasoning_effort": "high", "input_tokens": 10, "cached_input_tokens": 2,
                    "output_tokens": 4, "reasoning_tokens": 1, "total_tokens": 14,
                    "duration_ms": 12, "model_calls": 1, "tool_calls": 3, "file_reads": 2,
                    "benchmark": {"schema_version": "1", "mode": "on", "test_result": "passed", "final_success": True},
                    "raw_output": "never exposed", "task": "never exposed",
                }
        events = FakeEvents()
        result = RouteCraftCore(registry=REGISTRY, host=ObservedHost(), events=events).execute(
            RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a")
        )
        self.assertTrue(result.succeeded)
        completed = events.events[-1]["metadata"]["routecraft_telemetry"]
        self.assertEqual("routecraft", completed["decision_source"])
        self.assertTrue(completed["route_changed"])
        self.assertEqual(14, completed["total_tokens"])
        self.assertEqual(1, completed["model_calls"])
        self.assertEqual("passed", completed["benchmark"]["test_result"])
        self.assertNotIn("raw_output", json.dumps(events.events))
        self.assertNotIn("never exposed", json.dumps(events.events))

        class ModelAbsentHost:
            def dispatch(self, request, decision, executor=None): return {"succeeded": True, "status": "succeeded"}
        absent_events = FakeEvents()
        RouteCraftCore(registry=REGISTRY, host=ModelAbsentHost(), events=absent_events).execute(
            RoutingRequest(task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a")
        )
        absent = absent_events.events[-1]["metadata"]["routecraft_telemetry"]
        self.assertIsNone(absent["actual_model"])
        self.assertIsNone(absent["route_changed"])

    def test_native_host_observation_is_not_attributed_to_routecraft(self) -> None:
        class NativeHost:
            def dispatch(self, request, decision, executor=None): return {"succeeded": True, "status": "succeeded", "actual_model": "model_b"}
        events = FakeEvents()
        RouteCraftCore(registry=REGISTRY, host=NativeHost(), events=events).execute(
            RoutingRequest(task="work", mode="native", provider="provider_a", host="host_a", model="model_a")
        )
        telemetry = events.events[-1]["metadata"]["routecraft_telemetry"]
        self.assertEqual("codex", telemetry["decision_source"])
        self.assertTrue(telemetry["route_changed"])

    def test_route_changed_uses_requested_reasoning_only_when_models_match(self) -> None:
        class ReasoningHost:
            def dispatch(self, request, decision, executor=None):
                return {"succeeded": True, "status": "succeeded", "actual_model": "model_a", "actual_reasoning": "high"}
        events = FakeEvents()
        RouteCraftCore(registry=REGISTRY, host=ReasoningHost(), events=events).execute(RoutingRequest(
            task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a", requested_reasoning="medium",
        ))
        self.assertTrue(events.events[-1]["metadata"]["routecraft_telemetry"]["route_changed"])

        missing_events = FakeEvents()
        RouteCraftCore(registry=REGISTRY, host=ReasoningHost(), events=missing_events).execute(RoutingRequest(
            task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a",
        ))
        self.assertIsNone(missing_events.events[-1]["metadata"]["routecraft_telemetry"]["route_changed"])

    def test_routecraft_lane_does_not_masquerade_as_an_exact_selected_model(self) -> None:
        class Host:
            def dispatch(self, request, decision, executor=None): return {"succeeded": True, "status": "succeeded"}
        events = FakeEvents()
        RouteCraftCore(registry=REGISTRY, host=Host(), events=events).execute(RoutingRequest(
            task="work", mode="routecraft", provider="provider_a", host="host_a", model="model_a",
        ))
        telemetry = events.events[-1]["metadata"]["routecraft_telemetry"]
        self.assertEqual("model_a", telemetry["requested_model"])
        self.assertIsNone(telemetry["selected_model"])
        self.assertIsNone(telemetry["route_decision_model"])
        self.assertEqual("medium", telemetry["selected_reasoning"])

    def test_host_benchmark_v2_preserves_comparison_identifiers(self) -> None:
        class BenchmarkHost:
            def dispatch(self, request, decision, executor=None):
                return {"succeeded": True, "status": "succeeded", "benchmark": {
                    "schema_version": "2", "mode": "on", "pair_id": "pair_001", "scope_id": "scope_001",
                    "test_result": "passed", "final_success": True,
                }}
        events = FakeEvents()
        RouteCraftCore(host=BenchmarkHost(), events=events).execute(RoutingRequest(task="work", mode="routecraft"))
        benchmark = events.events[-1]["metadata"]["routecraft_telemetry"]["benchmark"]
        self.assertEqual("2", benchmark["schema_version"])
        self.assertEqual("pair_001", benchmark["pair_id"])
        self.assertEqual("scope_001", benchmark["scope_id"])

    def test_core_version_is_resolved_from_plugin_manifest(self) -> None:
        manifest = json.loads((SCRIPTS.parent / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], resolve_routecraft_version())

    def test_cli_is_utf8_json_and_does_not_dispatch(self) -> None:
        completed = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), "plan", "--task", "日本語の計画", "--mode", "advisory"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(payload["ok"]); self.assertFalse(payload["data"]["dispatch"])


if __name__ == "__main__":
    unittest.main()
