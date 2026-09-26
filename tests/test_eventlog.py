from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.eventlog import (
    EventLog,
    IngestOutcome,
    IngestionGateway,
    VersionConflict,
    canonical_hash,
)


def _event(event_id: str, version: int, **extra: object) -> dict[str, object]:
    payload = {
        "event_id": event_id,
        "event_type": "VITALS_RECORDED",
        "aggregate_type": "vital_sign",
        "aggregate_id": "V-1",
        "occurred_at": "2026-09-25T06:35:10+08:00",
        "version": version,
        "summary": "体征记录",
    }
    payload.update(extra)
    return payload


class AppendOnlyLogTests(unittest.TestCase):
    def test_versions_must_be_strictly_sequential(self) -> None:
        log = EventLog()
        log.append(_event("E1", 1))
        with self.assertRaises(VersionConflict):
            log.append(_event("E2", 3))
        log.append(_event("E2", 2))
        self.assertEqual([1, 2], [e["version"] for e in log.stream("vital_sign", "V-1")])

    def test_event_id_is_unique(self) -> None:
        log = EventLog()
        log.append(_event("E1", 1))
        other = _event("E1", 1, aggregate_id="V-2")
        with self.assertRaises(ValueError):
            log.append(other)

    def test_immutable_fact_types_are_registered(self) -> None:
        log = EventLog()
        self.assertIn("safety_incident", log.immutable_aggregates())
        self.assertIn("vital_sign", log.immutable_aggregates())


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = EventLog()
        self.gw = IngestionGateway(self.log)

    def _wearable(self, event_id: str, source: str, body: dict[str, object], when: str) -> dict[str, object]:
        return _event(
            event_id, 1,
            participant_id="P-1", site_id="S-1", actor_id="wearable", actor_role="system",
            occurred_at=when, summary="穿戴上报",
            source_record_id=source, content_hash=canonical_hash(body), data=body,
        )

    def test_first_arrival_is_admitted_once(self) -> None:
        body = {"metric": "heart_rate", "value": 118}
        r1 = self.gw.admit(self._wearable("W1", "WR-1", body, "2026-09-25T06:35:10+08:00"))
        self.assertIs(IngestOutcome.ADMITTED, r1.outcome)

        r2 = self.gw.admit(self._wearable("W1-DUP", "WR-1", body, "2026-09-25T09:00:00+08:00"))
        self.assertIs(IngestOutcome.DEDUPED, r2.outcome)

        vitals = self.log.stream("vital_sign", "V-1")
        self.assertEqual(1, len(vitals), "重复记录必须保持一次事实")
        dedup_events = [e for e in self.log.all_events() if e["event_type"] == "INGESTION_DEDUPED"]
        self.assertEqual(1, len(dedup_events))
        self.assertEqual(["W1"], dedup_events[0]["basis_refs"])

    def test_same_id_different_content_opens_review_without_overwriting(self) -> None:
        body_a = {"metric": "heart_rate", "value": 118}
        body_b = {"metric": "heart_rate", "value": 151}
        self.gw.admit(self._wearable("W1", "WR-1", body_a, "2026-09-25T06:35:10+08:00"))
        conflict = self.gw.admit(self._wearable("W1-B", "WR-1", body_b, "2026-09-25T07:06:00+08:00"))
        self.assertIs(IngestOutcome.REVIEW_OPENED, conflict.outcome)

        # 原始记录未被覆盖，冲突载荷未成为体征事实
        vitals = self.log.stream("vital_sign", "V-1")
        self.assertEqual(1, len(vitals))
        self.assertEqual(118, vitals[0]["data"]["value"])

        review = [e for e in self.log.all_events() if e["event_type"] == "DATA_REVIEW_OPENED"][0]
        self.assertEqual(151, review["conflicting_payload"]["data"]["value"])

        resolved = self.gw.resolve_review("WR-1", event_id="R1", resolution="维持首传", actor_id="DR-1")
        self.assertIs(IngestOutcome.ADMITTED, resolved.outcome)
        self.assertEqual(1, len(self.log.stream("vital_sign", "V-1")))

    def test_late_offline_upload_does_not_rewrite_sequence(self) -> None:
        body = {"metric": "spo2", "value": 97}
        r1 = self.gw.admit(self._wearable("W1", "WR-1", body, "2026-09-25T06:35:10+08:00"))
        r2 = self.gw.admit(self._wearable(
            "W2", "WR-2", {"metric": "spo2", "value": 98}, "2026-09-25T06:40:00+08:00"))
        # 离线迟到：发生时间更早，但入库排在后面，版本仍按入库顺序
        self.assertIs(IngestOutcome.ADMITTED, r2.outcome)
        order = [(e["event_id"], e["version"]) for e in self.log.stream("vital_sign", "V-1")]
        self.assertEqual([("W1", 1), ("W2", 2)], order)

    def test_invalid_payload_is_rejected_not_admitted(self) -> None:
        bad = _event("BAD", 1, source_record_id="WR-X")  # 缺 content_hash
        result = self.gw.admit(bad)
        self.assertIs(IngestOutcome.REJECTED, result.outcome)
        self.assertTrue(result.issues)


if __name__ == "__main__":
    unittest.main()
