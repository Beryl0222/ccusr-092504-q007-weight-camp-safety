from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.archive import ArchiveError, VersionedArchive
from weight_camp_safety.contracts import validate_event
from weight_camp_safety.ingest import IngestStatus
from weight_camp_safety.models import NOT_A_DIAGNOSIS, RecordKind, RiskTier, Role
from weight_camp_safety.notify import EmergencyNotifier, NotificationStatus
from weight_camp_safety.rules import ApprovedRuleSet, MovementThreshold
from weight_camp_safety.service import (
    ActiveStopError,
    CoachCredentialExpired,
    InterventionService,
    MovementNotAllowed,
    ResumeWithoutReview,
    RetroactivePlanChange,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 9, 0, tzinfo=TZ)

MEDICAL = {
    "participant_id": "p-001",
    "original_weight_kg": 96.5,
    "height_cm": 172,
    "conditions": ["冠心病"],
    "doctor_contraindicated": False,
}


def make_rules() -> ApprovedRuleSet:
    return ApprovedRuleSet(
        rule_set_id="rules-2026q3",
        version=3,
        approved_by="医疗安全主管",
        approved_at=T0,
        thresholds=(
            MovementThreshold(RiskTier.LOW, 150, 90, ()),
            MovementThreshold(RiskTier.MEDIUM, 130, 60, ("波比跳",)),
            MovementThreshold(RiskTier.HIGH, 110, 40, ("波比跳", "负重深蹲")),
            MovementThreshold(RiskTier.CONTRAINDICATED, 0, 0, ("*",)),
        ),
        high_risk_conditions=frozenset({"冠心病", "高血压3级"}),
        medium_risk_conditions=frozenset({"高血压1级", "膝关节炎"}),
    )


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.holder = {"now": T0}
        self.archive = VersionedArchive()
        self.sent: list[tuple[str, str]] = []
        self.transport_error: Exception | None = None

        def transport(channel: str, message: str) -> None:
            if self.transport_error is not None:
                raise self.transport_error
            self.sent.append((channel, message))

        self.notifier = EmergencyNotifier(transport)
        self.service = InterventionService(
            self.archive, make_rules(), self.notifier, clock=lambda: self.holder["now"]
        )
        self.service.register_coach_credential(
            "coach-1", {"coach_id": "coach-1", "valid_until": "2027-06-01T00:00:00+08:00"}
        )

    def enroll(self, participant_id: str = "p-001", medical: dict | None = None):
        return self.service.enroll_participant(
            participant_id,
            consent={"agreed": True, "scope": "训练风险知情同意"},
            medical_assessment=medical or dict(MEDICAL, participant_id=participant_id),
            exercise_assessment={"participant_id": participant_id, "vo2max": 32},
        )


class StratificationTests(ServiceFixture):
    def test_stratification_uses_approved_rules_and_carries_disclaimer(self) -> None:
        result = self.enroll()
        self.assertEqual(RiskTier.HIGH, result.tier)
        self.assertEqual(110, result.threshold.max_heart_rate)
        self.assertEqual("rules-2026q3", result.rule_set_id)
        self.assertEqual(NOT_A_DIAGNOSIS, result.disclaimer)

    def test_tiers_follow_conditions_and_bmi(self) -> None:
        base = {"height_cm": 170, "original_weight_kg": 70.0, "conditions": []}
        self.assertEqual(RiskTier.LOW, self.enroll("p-low", dict(base, participant_id="p-low")).tier)
        medium = dict(base, participant_id="p-mid", conditions=["膝关节炎"])
        self.assertEqual(RiskTier.MEDIUM, self.enroll("p-mid", medium).tier)
        contraindicated = dict(base, participant_id="p-ct", doctor_contraindicated=True)
        self.assertEqual(RiskTier.CONTRAINDICATED, self.enroll("p-ct", contraindicated).tier)

    def test_prohibited_movement_is_rejected_for_tier(self) -> None:
        self.enroll()
        with self.assertRaises(MovementNotAllowed):
            self.service.execute_day("p-001", "coach-1", {"date": "2026-09-26", "movements": ["波比跳"]})


class VersionRetentionTests(ServiceFixture):
    def test_every_record_kind_keeps_separate_versions(self) -> None:
        now = self.holder["now"]
        for kind in RecordKind:
            if kind is RecordKind.COMMERCIAL_PROMISE:
                continue
            record_id = f"rk-{kind.value}"
            payload = {"participant_id": "p-x", "incident_id": record_id, "symptoms": ["头晕"]}
            self.archive.append(kind, record_id, dict(payload, seq=1), Role.MEDICAL_REVIEWER, now)
            self.archive.append(kind, record_id, dict(payload, seq=2), Role.MEDICAL_REVIEWER, now)
            history = self.archive.history(kind, record_id)
            self.assertEqual([1, 2], [v.version for v in history], kind.value)
            self.assertEqual(1, history[0].payload["seq"], kind.value)

    def test_prescription_history_is_retained_and_future_only(self) -> None:
        self.enroll()
        self.service.prescribe("p-001", {"calories": 1800}, effective_from=T0)
        self.service.prescribe("p-001", {"calories": 1600}, effective_from=T0 + timedelta(days=1))
        history = self.archive.history(RecordKind.PRESCRIPTION, "p-001")
        self.assertEqual([1800, 1600], [v.payload["calories"] for v in history])
        with self.assertRaises(RetroactivePlanChange):
            self.service.prescribe("p-001", {"calories": 1500}, effective_from=T0 - timedelta(days=1))

    def test_performance_staff_cannot_modify_medical_facts(self) -> None:
        self.enroll()
        with self.assertRaises(ArchiveError):
            self.archive.append(
                RecordKind.MEDICAL_ASSESSMENT, "p-001", dict(MEDICAL), Role.PERFORMANCE_STAFF, T0
            )
        with self.assertRaises(ArchiveError):
            self.archive.append(
                RecordKind.EMERGENCY_RESPONSE, "stop-x", {"incident_id": "stop-x"}, Role.PERFORMANCE_STAFF, T0
            )

    def test_original_weight_is_immutable_across_versions(self) -> None:
        self.enroll()
        with self.assertRaises(ArchiveError):
            self.archive.append(
                RecordKind.MEDICAL_ASSESSMENT,
                "p-001",
                dict(MEDICAL, original_weight_kg=90.0),
                Role.MEDICAL_REVIEWER,
                T0,
            )


class StopAndResumeTests(ServiceFixture):
    def test_red_flag_symptoms_trigger_immediate_stop_and_escalation(self) -> None:
        self.enroll()
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["胸闷", "头晕"]}, "coach-1")
        self.assertIsNotNone(stop)
        self.assertTrue(stop.active)
        self.assertEqual(("头晕", "胸闷"), stop.symptoms)
        types = [e["event_type"] for e in self.service.events]
        self.assertIn("TRAINING_STOPPED", types)
        self.assertIn("ESCALATION_RAISED", types)
        dispositions = self.archive.history(RecordKind.EMERGENCY_RESPONSE, stop.stop_id)
        self.assertEqual("即时停训并升级", dispositions[0].payload["disposition"])
        with self.assertRaises(ActiveStopError):
            self.service.execute_day("p-001", "coach-1", {"date": "2026-09-27", "movements": []})

    def test_emergency_call_failure_does_not_block_local_stop(self) -> None:
        self.enroll()
        self.transport_error = ConnectionError("外呼网关不可用")
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["心悸"]}, "coach-1")
        self.assertTrue(stop.active)
        (notification,) = self.notifier.notifications_for(stop.stop_id)
        self.assertEqual(NotificationStatus.FAILED, notification.status)

        # 服务恢复：已确认的不补送，未确认的补送。
        self.transport_error = None
        self.notifier.acknowledge(notification.notification_id)
        _, stop2 = self.service.record_vitals("p-001", {"symptoms": ["明显乏力"]}, "coach-1")
        resent = self.notifier.resend_unacknowledged()
        self.assertEqual([n.incident_id for n in resent], [stop2.stop_id])
        self.assertEqual(2, len(self.sent))  # 第二次告警首次送达 + 恢复后补送

    def test_only_new_professional_review_can_resume(self) -> None:
        self.enroll()
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["胸闷"]}, "coach-1")
        with self.assertRaises(ResumeWithoutReview):
            self.service.resume_training(stop.stop_id)
        self.service.submit_review(stop.stop_id, "王医生", "maintain", "建议观察 48 小时")
        with self.assertRaises(ResumeWithoutReview):
            self.service.resume_training(stop.stop_id)
        self.holder["now"] = T0 + timedelta(days=2)
        self.service.submit_review(stop.stop_id, "王医生", "resume", "复查指标正常")
        resumed = self.service.resume_training(stop.stop_id)
        self.assertFalse(resumed.active)
        self.assertIn("TRAINING_RESUMED", [e["event_type"] for e in self.service.events])
        self.service.execute_day("p-001", "coach-1", {"date": "2026-09-28", "movements": ["快走"]})


class CoachCredentialTests(ServiceFixture):
    def test_expired_coach_credential_blocks_execution(self) -> None:
        self.service.register_coach_credential(
            "coach-2", {"coach_id": "coach-2", "valid_until": "2026-01-01T00:00:00+08:00"}
        )
        self.enroll()
        with self.assertRaises(CoachCredentialExpired):
            self.service.execute_day("p-001", "coach-2", {"date": "2026-09-26", "movements": []})
        with self.assertRaises(CoachCredentialExpired):
            self.service.execute_day("p-001", "coach-ghost", {"date": "2026-09-26", "movements": []})


class IngestTests(ServiceFixture):
    def test_duplicate_wearable_payload_is_recorded_once(self) -> None:
        self.enroll()
        content = {"heart_rate": 102, "symptoms": []}
        first, _ = self.service.ingest_wearable("p-001", "band", "w-1", content, "coach-1")
        second, _ = self.service.ingest_wearable("p-001", "band", "w-1", content, "coach-1")
        self.assertEqual(IngestStatus.STORED, first.status)
        self.assertEqual(IngestStatus.DUPLICATE, second.status)
        self.assertEqual(1, len(self.archive.history(RecordKind.VITAL_SIGN, "p-001")))

    def test_same_id_with_different_content_goes_to_review(self) -> None:
        self.enroll()
        self.service.ingest_wearable("p-001", "band", "w-2", {"heart_rate": 100}, "coach-1")
        result, _ = self.service.ingest_wearable("p-001", "band", "w-2", {"heart_rate": 130}, "coach-1")
        self.assertEqual(IngestStatus.CONFLICT, result.status)
        self.assertEqual(1, len(self.service.ingest.conflicts))
        self.assertEqual(1, len(self.archive.history(RecordKind.VITAL_SIGN, "p-001")))


class RefundIsolationTests(ServiceFixture):
    def test_refund_settlement_never_alters_medical_facts(self) -> None:
        self.enroll()
        self.service.register_commercial_promise(
            "promise-1", {"participant_id": "p-001", "terms": "不瘦退款"}, Role.PERFORMANCE_STAFF
        )
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["头晕"]}, "coach-1")
        before = {
            kind: list(self.archive.history(kind, "p-001"))
            for kind in (RecordKind.MEDICAL_ASSESSMENT, RecordKind.VITAL_SIGN)
        }
        before_dispositions = list(self.archive.history(RecordKind.EMERGENCY_RESPONSE, stop.stop_id))

        settlement = self.service.settle_refund("p-001", "promise-1", 12800.0, Role.PERFORMANCE_STAFF)
        self.service.mark_refund_paid(settlement.settlement_id)

        for kind, versions in before.items():
            self.assertEqual(versions, self.archive.history(kind, "p-001"))
        self.assertEqual(before_dispositions, self.archive.history(RecordKind.EMERGENCY_RESPONSE, stop.stop_id))
        self.assertEqual("paid", self.service.settlements[settlement.settlement_id].status)


class TransparencyTests(ServiceFixture):
    def test_participant_view_shows_stop_basis_followups_and_refund_progress(self) -> None:
        self.enroll()
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["胸闷"]}, "coach-1")
        self.service.add_follow_up(stop.stop_id, "48 小时后复查心电图")
        settlement = self.service.settle_refund("p-001", "promise-1", 12800.0, Role.PERFORMANCE_STAFF)

        view = self.service.participant_view("p-001")
        self.assertEqual("high", view["risk_tier"])
        (item,) = view["stops"]
        self.assertEqual("出现红旗症状，即时停训并升级", item["reason"])
        self.assertEqual(["胸闷"], item["symptoms"])
        self.assertEqual(["48 小时后复查心电图"], item["follow_ups"])
        self.assertEqual([{"settlement_id": settlement.settlement_id, "amount": 12800.0, "status": "pending"}], view["refunds"])

    def test_regulator_traces_incident_back_to_origin(self) -> None:
        self.enroll()
        self.service.record_contraindication("p-001", {"note": "避免憋气发力动作"})
        self.service.record_referral("p-001", {"note": "建议心内科随访"})
        self.service.prescribe("p-001", {"calories": 1800}, effective_from=T0)
        self.service.prescribe("p-001", {"calories": 1600}, effective_from=T0 + timedelta(days=1))
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["心悸"]}, "coach-1")
        self.service.submit_review(stop.stop_id, "王医生", "resume")
        self.service.resume_training(stop.stop_id)

        trace = self.service.trace_incident(stop.stop_id)
        self.assertEqual("coach-1", trace.on_duty_coach)
        self.assertEqual("coach-1", trace.coach_credential.payload["coach_id"])
        self.assertEqual(1, len(trace.enrollment["medical_assessment"]))
        self.assertEqual(1, len(trace.enrollment["contraindication"]))
        self.assertEqual([1, 2], [v.version for v in trace.prescription_versions])
        self.assertEqual(
            ["即时停训并升级", "专业复核后恢复训练"],
            [d.payload["disposition"] for d in trace.dispositions],
        )
        self.assertEqual(1, len(trace.notifications))


class EventContractTests(ServiceFixture):
    def test_emitted_events_satisfy_exchange_contract(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        self.enroll()
        self.service.prescribe("p-001", {"calories": 1800}, effective_from=T0)
        self.service.execute_day("p-001", "coach-1", {"date": "2026-09-26", "movements": ["快走"]})
        _, stop = self.service.record_vitals("p-001", {"symptoms": ["胸闷"]}, "coach-1")
        self.service.submit_review(stop.stop_id, "王医生", "resume")
        self.service.resume_training(stop.stop_id)
        self.service.settle_refund("p-001", "promise-1", 12800.0, Role.PERFORMANCE_STAFF)

        self.assertEqual(7, len(self.service.events))
        for event in self.service.events:
            self.assertEqual([], validate_event(event, schema), event["event_type"])


if __name__ == "__main__":
    unittest.main()
