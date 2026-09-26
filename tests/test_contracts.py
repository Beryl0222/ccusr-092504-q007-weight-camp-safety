from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_sample_trace_events_are_all_valid(self) -> None:
        trace = json.loads((ROOT / "data" / "sample_trace.json").read_text(encoding="utf-8"))
        for event in trace["events"]:
            self.assertEqual([], validate_event(event, self.schema), event["event_id"])

    def test_schema_and_validator_register_same_types(self) -> None:
        from weight_camp_safety.contracts import AGGREGATE_TYPES, EVENT_TYPES

        self.assertEqual(set(self.schema["properties"]["event_type"]["enum"]), set(EVENT_TYPES))
        self.assertEqual(set(self.schema["properties"]["aggregate_type"]["enum"]), set(AGGREGATE_TYPES))

    def test_missing_fields_are_reported_in_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_naive_time_and_zero_version_are_rejected(self) -> None:
        payload = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(payload, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_unknown_event_type_is_rejected(self) -> None:
        payload = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(payload, self.schema)
        self.assertEqual([("event_type", "unsupported_value")], [(item.field, item.code) for item in issues])

    def test_idempotency_key_must_come_as_pair(self) -> None:
        payload = dict(self.sample, source_record_id="WR-1")
        codes = {(i.field, i.code) for i in validate_event(payload, self.schema)}
        self.assertIn(("source_record_id", "idempotency_key_pair_required"), codes)

    def test_basis_refs_must_be_unique_non_empty(self) -> None:
        payload = dict(self.sample, basis_refs=["a", "a"])
        codes = {(i.field, i.code) for i in validate_event(payload, self.schema)}
        self.assertIn(("basis_refs", "unique_non_empty_strings"), codes)
        self.assertEqual([], validate_event(dict(self.sample, basis_refs=["a"]), self.schema))

    def test_unknown_actor_role_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, actor_role="marketing"), self.schema)
        self.assertIn(("actor_role", "unsupported_value"), {(i.field, i.code) for i in issues})


if __name__ == "__main__":
    unittest.main()
