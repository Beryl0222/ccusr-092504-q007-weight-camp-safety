from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.emergency import EmergencyOutbox
from weight_camp_safety.eventlog import EventLog

P = "P-1"
S = "S-1"
REASONS = [
    {"rule_id": "R-STOP-001", "detail": "红旗症状：胸闷", "basis": "SYM-1"},
]


class _Channel:
    def __init__(self, online: bool) -> None:
        self.online = online
        self.sent: list[dict[str, object]] = []

    def send(self, payload: dict[str, object]) -> str:
        if not self.online:
            raise RuntimeError("offline")
        self.sent.append(payload)
        return f"R-{len(self.sent):03d}"


class EmergencyOutboxTests(unittest.TestCase):
    def _stop(self, outbox: EmergencyOutbox) -> dict[str, object]:
        return outbox.stop_training_locally(
            event_id="STOP-1", participant_id=P, site_id=S, actor_id="COACH-1",
            occurred_at="2026-09-25T07:11:30+08:00", reasons=REASONS,
            summary="即时停训", incident_id="INC-1", rule_book_version="2026.09-approved",
        )

    def test_local_stop_succeeds_without_any_transmitter(self) -> None:
        log = EventLog()
        outbox = EmergencyOutbox(log)
        stop = self._stop(outbox)
        self.assertEqual("TRAINING_STOPPED", stop["event_type"])
        self.assertEqual("IMMEDIATE", stop["data"]["scope"])
        self.assertEqual(["SYM-1"], stop["basis_refs"])

    def test_call_failure_does_not_block_stop_and_is_recorded(self) -> None:
        log = EventLog()
        outbox = EmergencyOutbox(log)
        self._stop(outbox)
        outbox.queue_notification(
            notification_id="N1", channel="120", target="120", subject="s",
            body={"x": 1}, basis_refs=["STOP-1"], participant_id=P, site_id=S,
        )
        results = outbox.attempt_pending(_Channel(online=False))
        self.assertEqual("FAILED", results[0]["status"])
        # 本地停训依然成立；通知仍在外发箱中
        self.assertEqual("TRAINING_STOPPED", log.get("STOP-1")["event_type"])
        self.assertEqual(["N1"], outbox.unconfirmed)
        failed_events = [e for e in log.all_events() if e["event_type"] == "NOTIFICATION_FAILED"]
        self.assertEqual(1, len(failed_events))

    def test_reconnect_resends_only_unconfirmed_and_marks_receipts(self) -> None:
        log = EventLog()
        outbox = EmergencyOutbox(log)
        self._stop(outbox)
        outbox.queue_notification(
            notification_id="N1", channel="120", target="120", subject="s1",
            body={}, basis_refs=["STOP-1"], participant_id=P, site_id=S,
        )
        outbox.queue_notification(
            notification_id="N2", channel="doctor", target="DR-1", subject="s2",
            body={}, basis_refs=["STOP-1"], participant_id=P, site_id=S,
        )
        outbox.attempt_pending(_Channel(online=False))

        online = _Channel(online=True)
        results = outbox.flush_on_reconnect(online)
        self.assertEqual({"DELIVERED"}, {r["status"] for r in results})
        self.assertEqual(2, len(online.sent))
        self.assertEqual([], outbox.unconfirmed)

        # 再次 flush 不会重复发送任何通知
        self.assertEqual([], outbox.flush_on_reconnect(online))
        self.assertEqual(2, len(online.sent))

        delivered = [e for e in log.all_events() if e["event_type"] == "NOTIFICATION_DELIVERED"]
        self.assertEqual(2, len(delivered))
        self.assertTrue(all(e["data"]["receipt"] for e in delivered))

    def test_duplicate_queue_is_ignored(self) -> None:
        log = EventLog()
        outbox = EmergencyOutbox(log)
        kwargs = dict(channel="120", target="120", subject="s", body={},
                      basis_refs=["STOP-1"], participant_id=P, site_id=S)
        outbox.queue_notification(notification_id="N1", **kwargs)
        outbox.queue_notification(notification_id="N1", **kwargs)
        queued = [e for e in log.all_events() if e["event_type"] == "NOTIFICATION_QUEUED"]
        self.assertEqual(1, len(queued))


if __name__ == "__main__":
    unittest.main()
