from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "codex-routecraft" / "scripts"))
from routecraft_protocols import (
    EVENT_SCHEMA_VERSION, ROUTECRAFT_TELEMETRY_KEYS, ROUTECRAFT_TELEMETRY_SCHEMA_VERSION,
    EventValidationError, TelemetryValidationError, new_event, new_routecraft_telemetry,
    validate_event, validate_routecraft_telemetry,
)
from routecraft_protocols.privacy import contains_absolute_path, contains_secret_like


def _secret_fixtures() -> tuple[str, ...]:
    """Construct detector fixtures at runtime so repository scans remain clean."""
    return (
        "sk-" + "a" * 20,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "eyJ" + "a" * 9 + "." + "b" * 9 + "." + "c" * 9,
        "gh" + "p_" + "a" * 20,
        "gh" + "o_" + "a" * 20,
        "gh" + "p_" + "a" * 10 + "_" + "a" * 10,
        "gh" + "u_" + "a" * 20,
        "gh" + "s_" + "a" * 20,
        "gh" + "r_" + "a" * 20,
        "github" + "_pat_" + "a" * 20,
        "-----" + "BEGIN" + " PRIVATE KEY" + "-----",
        "Bearer" + " " + "a" * 12,
    )


def _decorated_identifier_secrets() -> tuple[str, ...]:
    return tuple("prefix_" + secret + "_suffix" for secret in _secret_fixtures()[:-2])


def _credential_text_fixtures() -> tuple[str, ...]:
    return (
        "authorization" + "=" + "a" * 12,
        "api" + "_key=" + "a" * 6,
        "MY_" + "TOKEN=" + "a" * 6,
        "access" + "_key=\"" + "a" * 8 + "\"",
        "secret" + "_token:\"" + "a" * 8 + "\"",
    )


class EventProtocolTests(unittest.TestCase):
    def test_new_event_is_canonical_utc_and_preserves_unknown_as_null(self) -> None:
        event = new_event("routing.planned", "routecraft_core", task_id="task_1", metadata={"mode": "advisory"})
        self.assertEqual(EVENT_SCHEMA_VERSION, event["schema_version"])
        self.assertEqual(13, len(event))
        self.assertIsNone(event["provider"])
        self.assertTrue(event["timestamp"].endswith("Z"))
        self.assertEqual(event, validate_event(event))

    def test_schema_rejects_unknown_keys_families_and_privacy_leaks(self) -> None:
        event = new_event("task.created", "routecraft_core")
        bad = dict(event)
        bad["extra"] = True
        with self.assertRaises(EventValidationError):
            validate_event(bad)
        with self.assertRaises(EventValidationError):
            new_event("graph.started", "routecraft_core")
        metadata_cases = [{"api_key": "x"}, {"raw_output": "x"}, {"artifact": "C:/Users/name/file"}, {"artifact": "see C:\\Users\\name\\file"}, {"artifact": "see /home/name/file"}, {"artifact": "/srv/private/secret.txt"}, {"artifact": "/usr/local/private.txt"}, {"artifact": "/private/var/private.txt"}]
        metadata_cases.append({"note": _secret_fixtures()[0]})
        for metadata in metadata_cases:
            with self.subTest(metadata=metadata), self.assertRaises(EventValidationError):
                new_event("task.created", "routecraft_core", metadata=metadata)
        measured = new_event("usage.measured", "routecraft_core", metadata={"input_tokens": 4, "output_tokens": 6})
        self.assertEqual(10, measured["metadata"]["input_tokens"] + measured["metadata"]["output_tokens"])
        with self.assertRaises(EventValidationError):
            new_event("usage.measured", "routecraft_core", metadata={"token_count": "secret-like-string"})

    def test_schema_rejects_secret_like_top_level_identifiers_without_echoing_them(self) -> None:
        # Build the inert fixture at runtime so repository dogfood does not
        # mistake the source file itself for a committed credential.
        secret = _secret_fixtures()[0]
        for field in ("event_id", "provider", "agent", "model", "project", "task_id", "status"):
            fields = {field: secret}
            with self.subTest(field=field), self.assertRaises(EventValidationError) as raised:
                new_event("task.created", "routecraft_core", **fields)
            self.assertNotIn(secret, str(raised.exception))
        with self.assertRaises(EventValidationError) as raised:
            new_event("task.created", secret)
        self.assertNotIn(secret, str(raised.exception))
        with self.assertRaises(EventValidationError):
            new_event("task." + secret, "routecraft_core")

    def test_sample_is_valid_and_json_bounded(self) -> None:
        sample = json.loads((ROOT / "samples" / "praxis-event-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(sample, validate_event(sample))

    def test_optional_telemetry_round_trips_without_changing_common_event_v1_shape(self) -> None:
        telemetry = new_routecraft_telemetry(
            run_id="run_1", requested_model="model_a", selected_model="model_a", actual_model="model_b",
            decision_source="routecraft", decision_reason="routecraft_lane_hint", route_changed=True,
            input_tokens=10, cached_input_tokens=2, output_tokens=4, reasoning_tokens=1, total_tokens=14, model_calls=1,
            benchmark={"schema_version": "1", "mode": "on", "test_result": "passed", "final_success": True},
        )
        self.assertEqual(ROUTECRAFT_TELEMETRY_SCHEMA_VERSION, telemetry["schema_version"])
        self.assertEqual(set(ROUTECRAFT_TELEMETRY_KEYS), set(telemetry))
        self.assertEqual(1, telemetry["model_calls"])
        event = new_event("execution.completed", "routecraft_core", metadata={"routecraft_telemetry": telemetry})
        self.assertEqual(13, len(event))
        self.assertEqual(telemetry, event["metadata"]["routecraft_telemetry"])
        self.assertEqual(event, validate_event(event))
        # Arbitrary older privacy-safe metadata remains valid without the optional envelope.
        self.assertEqual({"future_hint": "opaque_value"}, new_event("task.created", "routecraft_core", metadata={"future_hint": "opaque_value"})["metadata"])

    def test_telemetry_rejects_bad_attribution_privacy_and_incomplete_route_change(self) -> None:
        with self.assertRaises(TelemetryValidationError):
            new_routecraft_telemetry(decision_source="host")
        with self.assertRaises(TelemetryValidationError):
            new_routecraft_telemetry(actual_model="model_b", route_changed=True)
        with self.assertRaises(TelemetryValidationError):
            new_routecraft_telemetry(session_id=_secret_fixtures()[0])
        with self.assertRaises(TelemetryValidationError):
            validate_routecraft_telemetry({"schema_version": "1"})

    def test_route_changed_compares_requested_to_actual_model_and_reasoning(self) -> None:
        reasoning_changed = new_routecraft_telemetry(
            requested_model="model_a", actual_model="model_a", requested_reasoning="medium",
            actual_reasoning="high", route_changed=True,
        )
        self.assertTrue(reasoning_changed["route_changed"])
        unchanged = new_routecraft_telemetry(
            requested_model="model_a", actual_model="model_a", requested_reasoning="medium",
            actual_reasoning="medium", route_changed=False,
        )
        self.assertFalse(unchanged["route_changed"])
        with self.assertRaises(TelemetryValidationError):
            new_routecraft_telemetry(requested_model="model_a", actual_model="model_a", route_changed=False)

    def test_benchmark_v1_round_trips_and_v2_adds_bounded_pair_scope(self) -> None:
        v1 = {"schema_version": "1", "mode": "off", "test_result": None, "final_success": None}
        self.assertEqual(v1, new_routecraft_telemetry(benchmark=v1)["benchmark"])
        v2 = {
            "schema_version": "2", "mode": "on", "pair_id": "pair_001", "scope_id": "scope_001",
            "test_result": "passed", "final_success": True,
        }
        self.assertEqual(v2, new_routecraft_telemetry(benchmark=v2)["benchmark"])
        with self.assertRaises(TelemetryValidationError) as raised:
            new_routecraft_telemetry(benchmark={**v2, "pair_id": _secret_fixtures()[0]})
        self.assertNotIn(_secret_fixtures()[0], str(raised.exception))
        with self.assertRaises(TelemetryValidationError):
            new_routecraft_telemetry(benchmark={**v2, "scope_id": "C:/private/scope"})

    def test_shared_privacy_rejects_all_secret_families_at_top_level_telemetry_and_benchmark_v2(self) -> None:
        baseline = {"schema_version": "2", "mode": "on", "pair_id": "pair_001", "scope_id": "scope_001", "test_result": "passed", "final_success": True}
        for secret in _secret_fixtures():
            with self.subTest(secret_family="runtime"):
                self.assertTrue(contains_secret_like(secret))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", secret)
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", "routecraft_core", provider=secret)
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(run_id=secret)
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(requested_model=secret)
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(memory_case_ids=[secret])
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(rules_applied=[secret])
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(benchmark={**baseline, "pair_id": secret})
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(benchmark={**baseline, "scope_id": secret})
                self.assertNotIn(secret, str(raised.exception))

    def test_shared_privacy_rejects_embedded_identifier_secrets_and_credential_text(self) -> None:
        baseline = {"schema_version": "2", "mode": "on", "pair_id": "pair_001", "scope_id": "scope_001", "test_result": "passed", "final_success": True}
        for secret in _decorated_identifier_secrets():
            with self.subTest(secret_family="embedded-runtime"):
                self.assertTrue(contains_secret_like(secret))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", secret, provider=secret)
                self.assertNotIn(secret, str(raised.exception))
                with self.assertRaises(TelemetryValidationError) as raised:
                    new_routecraft_telemetry(run_id=secret, requested_model=secret, memory_case_ids=[secret], rules_applied=[secret])
                self.assertNotIn(secret, str(raised.exception))
                for field in ("pair_id", "scope_id"):
                    with self.subTest(field=field), self.assertRaises(TelemetryValidationError) as raised:
                        new_routecraft_telemetry(benchmark={**baseline, field: secret})
                    self.assertNotIn(secret, str(raised.exception))
        for text in _secret_fixtures()[8:]:
            with self.subTest(text_family="runtime-text"):
                self.assertTrue(contains_secret_like("prefix " + text + " suffix"))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", "routecraft_core", metadata={"note": "prefix " + text + " suffix"})
                self.assertNotIn(text, str(raised.exception))
        for text in _credential_text_fixtures():
            with self.subTest(text_family="credential-runtime"):
                self.assertTrue(contains_secret_like(text))
                self.assertTrue(contains_secret_like("prefix " + text + " suffix"))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", "routecraft_core", metadata={"note": "prefix " + text + " suffix"})
                self.assertNotIn(text, str(raised.exception))
        self.assertFalse(contains_secret_like("opaque_id-2026.08"))

    def test_shared_privacy_detects_decorated_absolute_paths_without_rejecting_https_or_safe_labels(self) -> None:
        paths = (
            "prefix_" + "C:" + "/Users/name/file",
            "prefix-" + "\\\\" + "server" + "\\share\\file",
            "prefix-" + "/srv/private/file",
        )
        for path in paths:
            with self.subTest(path_kind="runtime"):
                self.assertTrue(contains_absolute_path(path))
                with self.assertRaises(EventValidationError) as raised:
                    new_event("task.created", "routecraft_core", metadata={"artifact": path})
                self.assertNotIn(path, str(raised.exception))
        self.assertFalse(contains_absolute_path("https://example.invalid/path"))
        self.assertEqual("model-a_b", new_routecraft_telemetry(requested_model="model-a_b")["requested_model"])

    def test_nullable_label_lists_reject_null_or_non_string_elements(self) -> None:
        self.assertIsNone(new_routecraft_telemetry(memory_case_ids=None)["memory_case_ids"])
        self.assertIsNone(new_routecraft_telemetry(rules_applied=None)["rules_applied"])
        for field, value in (
            ("memory_case_ids", [None]), ("memory_case_ids", ["case_1", None]),
            ("rules_applied", [None]), ("rules_applied", ["rule_1", 1]),
        ):
            with self.subTest(field=field, value_type=type(value[-1]).__name__), self.assertRaises(TelemetryValidationError):
                new_routecraft_telemetry(**{field: value})

    def test_unknown_key_diagnostics_do_not_reflect_secret_or_path_names(self) -> None:
        event = new_event("task.created", "routecraft_core")
        unknown_names = (
            "unknown_" + _secret_fixtures()[0],
            "unknown_" + "C:" + "/private/key",
        )
        for unknown in unknown_names:
            with self.subTest(boundary="validate_event", key_kind="runtime"), self.assertRaises(EventValidationError) as raised:
                validate_event({**event, unknown: True})
            self.assertNotIn(unknown, str(raised.exception))
            with self.subTest(boundary="new_event", key_kind="runtime"), self.assertRaises(EventValidationError) as raised:
                new_event("task.created", "routecraft_core", **{unknown: True})
            self.assertNotIn(unknown, str(raised.exception))
            with self.subTest(boundary="new_routecraft_telemetry", key_kind="runtime"), self.assertRaises(TelemetryValidationError) as raised:
                new_routecraft_telemetry(**{unknown: True})
            self.assertNotIn(unknown, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
