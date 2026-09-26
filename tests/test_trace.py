from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.scenario import INCIDENT, PARTICIPANT, SITE, build_scenario
from weight_camp_safety.trace import build_incident_trace, build_participant_view


class ScenarioFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = build_scenario()

    def test_all_basis_refs_resolve_within_trace(self) -> None:
        trace = build_incident_trace(self.h.log, INCIDENT)
        self.assertTrue(
            trace["basis_closure"].closed,
            {"missing": trace["basis_closure"].missing, "dangling": trace["basis_closure"].dangling},
        )

    def test_incident_trace_covers_full_chain(self) -> None:
        trace = build_incident_trace(self.h.log, INCIDENT)
        sections = trace["sections"]

        self.assertEqual(PARTICIPANT, trace["participant_id"])
        self.assertIn(SITE, trace["site_ids"])

        # 事故 -> 入营评估
        self.assertTrue(sections["intake_assessment"])
        self.assertEqual("CVD", sections["intake_assessment"][0]["data"]["conditions"][0])

        # -> 风险分层（批准规则版本）与禁忌转诊
        self.assertEqual("HIGH", sections["risk_stratification"][0]["data"]["tier"])
        self.assertTrue(sections["contraindication_and_referral"])

        # -> 当班人员（带训教练来自每日执行，资质事件按门店入网）
        duty = trace["on_duty_coaches"]
        self.assertEqual("COACH-LI", duty[0]["coach_id"])
        self.assertTrue(
            any(e["aggregate_id"] == "CERT-COACH-LI" for e in sections["coach_on_duty"])
        )

        # -> 处方版本、计划版本与每次执行
        rx = sections["prescription_versions"]
        self.assertEqual(
            [("PRESCRIPTION_ISSUED", 1), ("PRESCRIPTION_SUPERSEDED", 2)],
            [(e["event_type"], e["version"]) for e in rx],
        )
        plans = sections["plan_versions"]
        self.assertEqual(["PLAN_ACTIVATED", "PLAN_REVISED"], [e["event_type"] for e in plans])
        # 计划 v2 必须回引到处方 v2
        self.assertIn("EVT-RX-002", plans[1]["basis_refs"])
        self.assertTrue(sections["daily_execution"])

        # -> 体征症状、每次停训与应急处置
        self.assertTrue(sections["vitals_and_symptoms"])
        stop_types = [e["event_type"] for e in sections["stops_and_dispositions"]]
        self.assertIn("TRAINING_STOPPED", stop_types)
        self.assertIn("EMERGENCY_RESPONDED", stop_types)

        # -> 专业复核恢复与商业结算
        self.assertTrue(sections["professional_reviews"])
        commercial = sections["commercial"]
        self.assertEqual(
            ["COMMITMENT_RECORDED", "SUSPENSION_RECORD_FILED", "REFUND_SETTLED"],
            [e["event_type"] for e in commercial],
        )

    def test_trace_unknown_incident_raises(self) -> None:
        with self.assertRaises(KeyError):
            build_incident_trace(self.h.log, "INC-NOPE")

    def test_participant_view_lists_stop_basis_arrangements_and_refund(self) -> None:
        view = build_participant_view(self.h.log, PARTICIPANT).as_dict()
        self.assertTrue(view["consent_active"])
        self.assertEqual("HIGH", view["current_risk_tier"])
        # 复核后已恢复
        self.assertFalse(view["stopped"])

        basis = view["stop_basis"][0]
        self.assertEqual("EVT-STOP-001", basis["stop_event_id"])
        self.assertEqual("REQUIRES_NEW_PROFESSIONAL_REVIEW", basis["resume_policy"])
        self.assertEqual("2026.09-approved", basis["rule_book_version"])
        rule_ids = {r["rule_id"] for r in basis["reasons"]}
        self.assertEqual({"R-STOP-001"}, rule_ids)
        self.assertIn("EVT-SYMPTOM-001", basis["source_record_ids"])

        kinds = [a["type"] for a in view["next_arrangements"]]
        self.assertEqual(
            ["REFERRAL_REQUESTED", "REFERRAL_FEEDBACK_RECORDED", "STOP",
             "REVIEW_PENDING", "REVIEW_CLOSED", "RESUMED", "PLAN_REVISED"],
            kinds,
        )

        refund = view["refund_progress"]
        self.assertEqual("不瘦退款", refund["commitments"][0]["terms"]["clause"])
        self.assertIsNotNone(refund["latest_settlement"])
        self.assertEqual(4200, refund["latest_settlement"]["detail"]["refund_amount_cny"])
        self.assertFalse(refund["latest_settlement"]["detail"]["medical_facts_modified"])

    def test_view_while_stopped_before_review(self) -> None:
        # 用只截止到停训时刻的日志重建视图：应显示 stopped 且无恢复安排
        from weight_camp_safety.eventlog import EventLog
        from weight_camp_safety.trace import build_participant_view as build_view

        cutoff = "2026-09-25T07:12:00+08:00"
        sub = EventLog()
        for e in self.h.log.all_events():
            if e["occurred_at"] <= cutoff:
                sub.append({k: v for k, v in e.items()})
        view = build_view(sub, PARTICIPANT).as_dict()
        self.assertTrue(view["stopped"])
        self.assertIsNone(view["refund_progress"]["latest_settlement"])

    def test_medical_facts_remain_unchanged_after_refund(self) -> None:
        # 退款后原始体重、事故、停训依据必须与首次写入完全一致
        assessment = self.h.log.get("EVT-ASSESS-001")
        self.assertEqual(98.6, assessment["data"]["baseline_weight_kg"])
        incident_open = self.h.log.get("EVT-INCIDENT-OPEN-001")
        self.assertEqual("COACH-LI", incident_open["actor_id"])
        stop = self.h.log.get("EVT-STOP-001")
        self.assertEqual(["EVT-SYMPTOM-001"], stop["basis_refs"])

    def test_gate_decisions_in_scenario_are_evidence(self) -> None:
        expected = {
            "expired_coach_start_blocked",
            "activation_before_referral_blocked",
            "resume_without_review_blocked",
            "backdated_plan_revision_blocked",
            "sales_medical_write_blocked",
            "refund_altering_medical_facts_blocked",
        }
        self.assertEqual(expected, set(self.h.gate_results))


if __name__ == "__main__":
    unittest.main()
