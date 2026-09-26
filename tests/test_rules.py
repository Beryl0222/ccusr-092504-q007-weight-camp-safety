from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.rules import (
    Action,
    RiskTier,
    check_action,
    evaluate_stop_signals,
    parse_ts,
    stratify_risk,
)

AT = "2026-09-25T07:00:00+08:00"
VALID_COACH = {
    "coach_id": "COACH-LI",
    "valid_from": "2025-09-01T00:00:00+08:00",
    "valid_until": "2027-08-31T23:59:59+08:00",
}
EXPIRED_COACH = {
    "coach_id": "COACH-ZHAO",
    "valid_from": "2023-01-01T00:00:00+08:00",
    "valid_until": "2025-12-31T23:59:59+08:00",
}


class StopSignalTests(unittest.TestCase):
    def test_red_flag_symptoms_stop_and_escalate(self) -> None:
        for code in ("CHEST_TIGHTNESS", "PALPITATIONS", "DIZZINESS", "MARKED_FATIGUE", "SYNCOPE"):
            decision = evaluate_stop_signals(symptoms=[{"record_id": "S1", "code": code}])
            self.assertTrue(decision.must_stop, code)
            self.assertTrue(decision.escalate, code)
            self.assertEqual("R-STOP-001", decision.triggered[0].rule_id)

    def test_non_red_flag_symptom_does_not_stop(self) -> None:
        decision = evaluate_stop_signals(symptoms=[{"record_id": "S1", "code": "MILD_SORENESS"}])
        self.assertFalse(decision.must_stop)

    def test_vitals_outside_threshold_stop(self) -> None:
        decision = evaluate_stop_signals(vitals=[
            {"record_id": "V1", "metric": "systolic_bp", "value": 185},
            {"record_id": "V2", "metric": "spo2", "value": 90},
            {"record_id": "V3", "metric": "heart_rate", "value": 96},
        ])
        self.assertTrue(decision.must_stop)
        self.assertEqual({"V1", "V2"}, {t.basis for t in decision.triggered})

    def test_all_triggered_signals_are_kept(self) -> None:
        decision = evaluate_stop_signals(
            symptoms=[{"record_id": "S1", "code": "CHEST_TIGHTNESS"}],
            vitals=[{"record_id": "V1", "metric": "heart_rate", "value": 160}],
        )
        self.assertEqual(2, len(decision.triggered))


class RiskStratificationTests(unittest.TestCase):
    def test_known_cardiovascular_disease_is_high(self) -> None:
        d = stratify_risk({"event_id": "A1", "conditions": ["CVD"]})
        self.assertIs(RiskTier.HIGH, d.tier)
        self.assertTrue(d.requires_referral)
        self.assertTrue(d.requires_medical_prescription)
        self.assertNotIn("high", d.intensity_cap)

    def test_moderate_condition_caps_high_intensity(self) -> None:
        d = stratify_risk({"event_id": "A1", "conditions": ["DIABETES"]})
        self.assertIs(RiskTier.MODERATE, d.tier)
        self.assertFalse(d.requires_referral)
        self.assertEqual(frozenset({"low", "moderate"}), d.intensity_cap)

    def test_severe_bmi_alone_is_moderate(self) -> None:
        d = stratify_risk({"event_id": "A1", "conditions": [], "bmi": 42.0})
        self.assertIs(RiskTier.MODERATE, d.tier)

    def test_open_contraindication_forces_high(self) -> None:
        d = stratify_risk({"event_id": "A1", "conditions": [], "contraindication_open": True})
        self.assertIs(RiskTier.HIGH, d.tier)

    def test_decision_carries_disclaimer_and_basis(self) -> None:
        d = stratify_risk({"event_id": "A1", "conditions": ["CVD"]})
        self.assertIn("不替代医生诊断", d.disclaimer)
        self.assertEqual("A1", d.triggered[0].basis)
        self.assertEqual("2026.09-approved", d.rule_book_version)


class GateTests(unittest.TestCase):
    def _high_risk(self) -> object:
        return stratify_risk({"event_id": "A1", "conditions": ["CVD"]})

    def test_expired_certification_blocks_start(self) -> None:
        violations = check_action(
            Action.START_SESSION,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            prescription_intensity="low", referral_cleared=True,
            coach=EXPIRED_COACH, at=parse_ts(AT),
        )
        self.assertEqual(["R-GATE-004"], [v.rule_id for v in violations])

    def test_revoked_certification_blocks_start(self) -> None:
        coach = dict(VALID_COACH, revoked=True)
        violations = check_action(
            Action.START_SESSION,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            prescription_intensity="low", referral_cleared=True,
            coach=coach, at=parse_ts(AT),
        )
        self.assertIn("撤销", violations[0].reason)

    def test_valid_coach_passes_when_referral_cleared(self) -> None:
        violations = check_action(
            Action.START_SESSION,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            prescription_intensity="low", referral_cleared=True,
            coach=VALID_COACH, at=parse_ts(AT),
        )
        self.assertEqual([], violations)

    def test_high_risk_requires_referral_before_start(self) -> None:
        violations = check_action(
            Action.START_SESSION,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            prescription_intensity="low", referral_cleared=False, coach=VALID_COACH,
            at=parse_ts(AT),
        )
        self.assertIn("R-GATE-007", [v.rule_id for v in violations])

    def test_consent_required(self) -> None:
        violations = check_action(
            Action.ACTIVATE_PLAN, actor_role="coach", consent_active=False,
            risk=self._high_risk(), referral_cleared=True,
        )
        self.assertIn("R-GATE-001", [v.rule_id for v in violations])

    def test_intensity_cap_enforced_on_plan(self) -> None:
        violations = check_action(
            Action.ACTIVATE_PLAN,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            prescription_intensity="high", referral_cleared=True,
        )
        self.assertIn("R-GATE-003", [v.rule_id for v in violations])

    def test_open_contraindication_blocks(self) -> None:
        violations = check_action(
            Action.ACTIVATE_PLAN,
            actor_role="coach", consent_active=True, risk=self._high_risk(),
            referral_cleared=True, contraindication_open=True,
        )
        self.assertIn("R-GATE-002", [v.rule_id for v in violations])

    def test_prescription_requires_professional_role(self) -> None:
        violations = check_action(
            Action.ISSUE_PRESCRIPTION, actor_role="coach",
            risk=stratify_risk({"event_id": "A1", "conditions": []}),
            prescription_intensity="low",
        )
        self.assertIn("R-GATE-005", [v.rule_id for v in violations])

    def test_resume_requires_new_review_closed_after_stop(self) -> None:
        stop = {"event_id": "STOP-1", "occurred_at": "2026-09-25T07:11:30+08:00"}
        # 旧复核（关闭于停训之前）不算"新的专业复核"
        violations = check_action(
            Action.RESUME_TRAINING, actor_role="medical_director", stop_event=stop,
            reviews=[{"status": "CLOSED", "actor_role": "medical_director",
                      "closed_at": "2026-09-24T10:00:00+08:00"}],
        )
        self.assertEqual(["R-GATE-006"], [v.rule_id for v in violations])

        ok = check_action(
            Action.RESUME_TRAINING, actor_role="medical_director", stop_event=stop,
            reviews=[{"status": "CLOSED", "actor_role": "medical_director",
                      "closed_at": "2026-09-26T10:00:00+08:00"}],
        )
        self.assertEqual([], ok)

        # 教练不能自行宣布恢复
        coach_resume = check_action(
            Action.RESUME_TRAINING, actor_role="coach", stop_event=stop,
            reviews=[{"status": "CLOSED", "actor_role": "medical_director",
                      "closed_at": "2026-09-26T10:00:00+08:00"}],
        )
        self.assertIn("R-GATE-006", [v.rule_id for v in coach_resume])

    def test_plan_revision_must_be_future_only(self) -> None:
        violations = check_action(
            Action.REVISE_PLAN, actor_role="medical_director",
            revision_effective_at=parse_ts("2026-09-25T00:00:00+08:00"),
            now=parse_ts("2026-09-26T09:00:00+08:00"),
        )
        self.assertEqual(["R-PLAN-001"], [v.rule_id for v in violations])

        ok = check_action(
            Action.REVISE_PLAN, actor_role="medical_director",
            revision_effective_at=parse_ts("2026-09-27T06:00:00+08:00"),
            now=parse_ts("2026-09-26T09:00:00+08:00"),
        )
        self.assertEqual([], ok)

    def test_sales_cannot_touch_medical_facts(self) -> None:
        violations = check_action(Action.WRITE_MEDICAL_FACT, actor_role="sales")
        rule_ids = {v.rule_id for v in violations}
        self.assertTrue({"R-ACCESS-001", "R-ACCESS-002"} <= rule_ids)

    def test_refund_cannot_override_medical_facts(self) -> None:
        violations = check_action(
            Action.SETTLE_REFUND, actor_role="administrator",
            refund_payload={"weight_patch": {"kg": 80}},
        )
        self.assertEqual(["R-COMM-001"], [v.rule_id for v in violations])
        self.assertEqual(
            [], check_action(Action.SETTLE_REFUND, actor_role="administrator",
                             refund_payload={"refund_amount_cny": 1000}),
        )


if __name__ == "__main__":
    unittest.main()
