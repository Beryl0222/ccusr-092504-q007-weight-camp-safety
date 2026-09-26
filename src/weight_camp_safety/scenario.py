"""端到端事故场景构造器：联调样例与测试共用同一套事实。

时间线（2026-09，华东某门店）：
入营问卷已注明心血管疾病 -> HIGH 分层与禁忌/转诊 -> 资质有效的教练按
低强度处方带训 -> 穿戴体征重复上报（幂等丢弃）、同标识异内容（开复核单）
-> 学员胸闷头晕，本地即时停训与应急处置 -> 120 通知首呼失败、重连补送成功
-> 无新复核时恢复被门槛拒绝 -> 医学主管新复核后恢复 -> 计划仅未来变更
-> 销售改写原始体重/事故被拒、退款结算不得改动医疗事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .emergency import EmergencyOutbox
from .eventlog import EventLog, IngestOutcome, IngestionGateway, canonical_hash
from .rules import (
    Action,
    RiskTier,
    RULE_BOOK_VERSION,
    check_action,
    evaluate_stop_signals,
    parse_ts,
    stratify_risk,
)

PARTICIPANT = "P-0925-007"
SITE = "SITE-HD-01"
INCIDENT = "INC-2026-0925-001"


class _FlakyTransmitter:
    """断网时全部呼叫失败；恢复在线后成功并逐次给回执。"""

    def __init__(self, online: bool = False) -> None:
        self.online = online
        self.calls = 0
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> str:
        self.calls += 1
        if not self.online:
            raise RuntimeError("应急通道暂时不可用")
        receipt = f"120-RECEIPT-{self.calls:04d}"
        self.sent.append({"payload": payload, "receipt": receipt})
        return receipt


@dataclass
class ScenarioHandles:
    log: EventLog
    gateway: IngestionGateway
    outbox: EmergencyOutbox
    transmitter: _FlakyTransmitter
    ids: dict[str, str] = field(default_factory=dict)
    gate_results: dict[str, list[str]] = field(default_factory=dict)


def _put(log: EventLog, event: dict[str, Any]) -> dict[str, Any]:
    event.setdefault("version", log.next_version(event["aggregate_type"], event["aggregate_id"]))
    return log.append(event)


def build_scenario() -> ScenarioHandles:
    log = EventLog()
    gateway = IngestionGateway(log)
    transmitter = _FlakyTransmitter()
    outbox = EmergencyOutbox(log, transmitter=None)  # 首呼在断网状态进行
    ids: dict[str, str] = {}
    gates: dict[str, list[str]] = {}

    def put(event: dict[str, Any]) -> dict[str, Any]:
        return _put(log, event)

    # 1. 学员同意 -------------------------------------------------------
    put({
        "event_id": "EVT-CONSENT-001",
        "event_type": "CONSENT_GRANTED",
        "aggregate_type": "consent",
        "aggregate_id": f"CONSENT-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": PARTICIPANT,
        "actor_role": "participant",
        "occurred_at": "2026-09-20T09:00:00+08:00",
        "summary": "学员签署知情同意：评估、训练监测与应急处置授权",
        "data": {"scope": ["assessment", "training_monitoring", "emergency"], "version_text": "CONSENT-v3"},
    })

    # 2. 入营医学/运动评估（问卷已注明基础疾病） -------------------------
    assessment = put({
        "event_id": "EVT-ASSESS-001",
        "event_type": "ASSESSMENT_APPROVED",
        "aggregate_type": "medical_assessment",
        "aggregate_id": f"MA-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "STAFF-014",
        "actor_role": "site_staff",
        "occurred_at": "2026-09-20T09:30:00+08:00",
        "summary": "入营问卷与体检：自述心血管疾病，静息血压偏高",
        "data": {
            "conditions": ["CVD", "CONTROLLED_HTN"],
            "bmi": 36.4,
            "baseline_weight_kg": 98.6,
            "resting_bp": "152/96",
            "meds": ["降压药"],
        },
    })
    ids["assessment"] = assessment["event_id"]

    # 3. 系统按已批准规则分层（不替代诊断） ------------------------------
    risk = stratify_risk(
        {"event_id": assessment["event_id"], "conditions": ["CVD", "CONTROLLED_HTN"], "bmi": 36.4}
    )
    assert risk.tier is RiskTier.HIGH
    put({
        "event_id": "EVT-RISK-001",
        "event_type": "RISK_STRATIFIED",
        "aggregate_type": "risk_assessment",
        "aggregate_id": f"RA-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_role": "system",
        "actor_id": "rule-engine",
        "occurred_at": "2026-09-20T09:31:00+08:00",
        "summary": f"规则分层 {risk.tier.value}：高强度计划禁用，需转诊与医学处方",
        "basis_refs": [assessment["event_id"]],
        "data": {
            "tier": risk.tier.value,
            "rule_book_version": risk.rule_book_version,
            "triggered": [{"rule_id": t.rule_id, "detail": t.detail, "basis": t.basis} for t in risk.triggered],
            "intensity_cap": sorted(risk.intensity_cap),
            "disclaimer": risk.disclaimer,
        },
    })

    # 4. 禁忌意见与转诊 --------------------------------------------------
    contra = put({
        "event_id": "EVT-CONTRA-001",
        "event_type": "CONTRAINDICATION_ISSUED",
        "aggregate_type": "contraindication_opinion",
        "aggregate_id": f"CO-{PARTICIPANT}-1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-20T10:00:00+08:00",
        "summary": "禁忌意见：禁止高强度间歇与负重屏气，转诊心内科评估",
        "basis_refs": [assessment["event_id"], "EVT-RISK-001"],
        "data": {"forbidden": ["HIIT", "heavy_load_valsalva"], "status": "OPEN"},
    })
    put({
        "event_id": "EVT-REF-001",
        "event_type": "REFERRAL_REQUESTED",
        "aggregate_type": "referral",
        "aggregate_id": f"REF-{PARTICIPANT}-1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-20T10:05:00+08:00",
        "summary": "已开具心内科转诊单",
        "basis_refs": [contra["event_id"]],
    })
    put({
        "event_id": "EVT-REF-002",
        "event_type": "REFERRAL_FEEDBACK_RECORDED",
        "aggregate_type": "referral",
        "aggregate_id": f"REF-{PARTICIPANT}-1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-22T15:00:00+08:00",
        "summary": "心内科反馈：可在监测下进行低强度有氧，随身携带自备药",
        "basis_refs": ["EVT-REF-001"],
        "data": {"clearance": "LOW_INTENSITY_MONITORED"},
    })

    # 5. 教练资质：有效教练入库；另一名过期教练被门禁拒绝 ---------------
    put({
        "event_id": "EVT-CERT-LI-001",
        "event_type": "COACH_CERTIFICATION_RECORDED",
        "aggregate_type": "coach_certification",
        "aggregate_id": "CERT-COACH-LI",
        "site_id": SITE,
        "actor_id": "HR-SYS",
        "actor_role": "administrator",
        "occurred_at": "2026-09-01T08:00:00+08:00",
        "summary": "李教练心肺复苏与健身指导资质登记",
        "data": {
            "coach_id": "COACH-LI",
            "valid_from": "2025-09-01T00:00:00+08:00",
            "valid_until": "2027-08-31T23:59:59+08:00",
        },
    })
    expired_coach = {
        "coach_id": "COACH-ZHAO",
        "valid_from": "2023-01-01T00:00:00+08:00",
        "valid_until": "2025-12-31T23:59:59+08:00",
    }
    expired_violations = check_action(
        Action.START_SESSION,
        actor_role="coach",
        consent_active=True,
        risk=risk,
        prescription_intensity="low",
        referral_cleared=True,
        coach=expired_coach,
        at=parse_ts("2026-09-25T07:00:00+08:00"),
    )
    assert [v.rule_id for v in expired_violations] == ["R-GATE-004"]
    gates["expired_coach_start_blocked"] = [v.reason for v in expired_violations]

    # 6. 商业承诺（"不瘦退款"）独立留存 ----------------------------------
    commitment = put({
        "event_id": "EVT-COMMIT-001",
        "event_type": "COMMITMENT_RECORDED",
        "aggregate_type": "commercial_commitment",
        "aggregate_id": f"CC-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "SALES-07",
        "actor_role": "sales",
        "occurred_at": "2026-09-20T11:00:00+08:00",
        "summary": "销售承诺：未达约定减重比例可申请退款（不构成医疗承诺）",
        "data": {"clause": "不瘦退款", "target_weight_loss_pct": 8, "medical_facts_independent": True},
    })
    ids["commitment"] = commitment["event_id"]

    # 7. 医学处方与计划激活 ----------------------------------------------
    prescription = put({
        "event_id": "EVT-RX-001",
        "event_type": "PRESCRIPTION_ISSUED",
        "aggregate_type": "prescription",
        "aggregate_id": f"RX-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-22T15:20:00+08:00",
        "effective_at": "2026-09-23T06:00:00+08:00",
        "summary": "低强度有氧 + 限能量平衡膳食，心率上限 120",
        "basis_refs": ["EVT-REF-002", contra["event_id"], "EVT-RISK-001"],
        "data": {
            "intensity": "low",
            "daily_kcal_deficit": 500,
            "heart_rate_cap": 120,
            "sodium_g_day": 5,
        },
    })
    plan = put({
        "event_id": "EVT-PLAN-001",
        "event_type": "PLAN_ACTIVATED",
        "aggregate_type": "training_plan",
        "aggregate_id": f"TP-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "COACH-LI",
        "actor_role": "coach",
        "occurred_at": "2026-09-22T18:00:00+08:00",
        "effective_at": "2026-09-23T06:00:00+08:00",
        "summary": "门店计划 v1：每日晨练低强度有氧 40 分钟",
        "basis_refs": [prescription["event_id"]],
        "data": {"plan_version": 1, "intensity": "low"},
    })

    # 高危分层未闭环转诊时，激活/开课必须被拒绝（反向留证）
    early_violations = check_action(
        Action.ACTIVATE_PLAN,
        actor_role="coach",
        consent_active=True,
        risk=risk,
        prescription_intensity="low",
        referral_cleared=False,
    )
    assert any(v.rule_id == "R-GATE-007" for v in early_violations)
    gates["activation_before_referral_blocked"] = [v.reason for v in early_violations]

    # 8. 每日执行 --------------------------------------------------------
    session = put({
        "event_id": "EVT-SESSION-001",
        "event_type": "SESSION_RECORDED",
        "aggregate_type": "session_execution",
        "aggregate_id": f"SE-{PARTICIPANT}-20260925",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "COACH-LI",
        "actor_role": "coach",
        "occurred_at": "2026-09-25T06:40:00+08:00",
        "summary": "晨间快走完成 35 分钟，无不适",
        "basis_refs": [plan["event_id"], prescription["event_id"]],
        "data": {"plan_version": 1, "prescription_version": 1, "duration_min": 35},
    })

    # 9. 穿戴体征：首次入库、重复幂等、同标识异内容复核 -----------------
    wearable_body = {"metric": "heart_rate", "value": 118, "measured_at": "2026-09-25T06:35:00+08:00"}
    h1 = canonical_hash(wearable_body)
    first = gateway.admit({
        "event_id": "EVT-VITALS-W1",
        "event_type": "VITALS_RECORDED",
        "aggregate_type": "vital_sign",
        "aggregate_id": f"VS-{PARTICIPANT}-W1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_role": "system",
        "actor_id": "wearable-A",
        "occurred_at": "2026-09-25T06:35:10+08:00",
        "summary": "穿戴心率上报 118",
        "source_record_id": "WR-20260925-0001",
        "content_hash": h1,
        "data": wearable_body,
    })
    assert first.outcome is IngestOutcome.ADMITTED
    dup = gateway.admit({
        "event_id": "EVT-VITALS-W1-DUP",
        "event_type": "VITALS_RECORDED",
        "aggregate_type": "vital_sign",
        "aggregate_id": f"VS-{PARTICIPANT}-W1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_role": "system",
        "actor_id": "wearable-A",
        "occurred_at": "2026-09-25T07:05:00+08:00",  # 离线补传，时间迟到
        "summary": "穿戴心率重复上报 118",
        "source_record_id": "WR-20260925-0001",
        "content_hash": h1,
        "data": wearable_body,
    })
    assert dup.outcome is IngestOutcome.DEDUPED
    conflict_body = dict(wearable_body, value=151)
    conflict = gateway.admit({
        "event_id": "EVT-VITALS-W1-CONFLICT",
        "event_type": "VITALS_RECORDED",
        "aggregate_type": "vital_sign",
        "aggregate_id": f"VS-{PARTICIPANT}-W1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_role": "system",
        "actor_id": "wearable-B",
        "occurred_at": "2026-09-25T07:06:00+08:00",
        "summary": "同标识心率 151（与首传 118 冲突）",
        "source_record_id": "WR-20260925-0001",
        "content_hash": canonical_hash(conflict_body),
        "data": conflict_body,
    })
    assert conflict.outcome is IngestOutcome.REVIEW_OPENED
    gateway.resolve_review(
        "WR-20260925-0001",
        event_id="EVT-REVIEW-DATA-001",
        resolution="复核：首传设备校准有效，151 为二次佩戴错位，维持首传记录",
        actor_id="DR-WANG",
    )

    # 10. 红旗症状 -> 规则即时停训 -> 本地落库（不依赖网络） -------------
    symptom = put({
        "event_id": "EVT-SYMPTOM-001",
        "event_type": "SYMPTOM_REPORTED",
        "aggregate_type": "vital_sign",
        "aggregate_id": f"SYM-{PARTICIPANT}-20260925",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": PARTICIPANT,
        "actor_role": "participant",
        "occurred_at": "2026-09-25T07:10:00+08:00",
        "summary": "学员主诉胸闷、心悸、头晕",
        "data": {"codes": ["CHEST_TIGHTNESS", "PALPITATIONS", "DIZZINESS"]},
    })
    decision = evaluate_stop_signals(
        symptoms=[{"record_id": symptom["event_id"], "code": c}
                  for c in symptom["data"]["codes"]],
    )
    assert decision.must_stop and decision.escalate
    put({
        "event_id": "EVT-INCIDENT-OPEN-001",
        "event_type": "SAFETY_INCIDENT_OPENED",
        "aggregate_type": "safety_incident",
        "aggregate_id": INCIDENT,
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "COACH-LI",
        "actor_role": "coach",
        "occurred_at": "2026-09-25T07:11:00+08:00",
        "summary": "学员训练中出现胸闷心悸头晕，开立事故单",
        "basis_refs": [symptom["event_id"], session["event_id"]],
    })
    stop = outbox.stop_training_locally(
        event_id="EVT-STOP-001",
        participant_id=PARTICIPANT,
        site_id=SITE,
        actor_id="COACH-LI",
        occurred_at="2026-09-25T07:11:30+08:00",
        reasons=[{"rule_id": t.rule_id, "detail": t.detail, "basis": t.basis} for t in decision.triggered],
        summary="即时停训：胸闷/心悸/头晕，等待专业复核",
        incident_id=INCIDENT,
        rule_book_version=RULE_BOOK_VERSION,
    )
    ids["stop"] = stop["event_id"]
    outbox.record_emergency_response(
        event_id="EVT-EMERGENCY-001",
        incident_id=INCIDENT,
        participant_id=PARTICIPANT,
        site_id=SITE,
        actor_id="COACH-LI",
        occurred_at="2026-09-25T07:12:00+08:00",
        actions=["就地平卧", "监测脉搏", "通知值班医生", "拨打120"],
        summary="现场应急处置：平卧监测并启动急救呼叫",
        basis_refs=[stop["event_id"], "EVT-INCIDENT-OPEN-001"],
    )

    # 11. 通知外发箱：首呼失败不阻塞，重连只补未确认通知 -----------------
    outbox.queue_notification(
        notification_id="NOTIF-120-001",
        channel="emergency_120",
        target="120",
        subject="学员训练中晕厥前兆",
        body={"incident_id": INCIDENT, "symptoms": symptom["data"]["codes"]},
        basis_refs=[stop["event_id"], "EVT-EMERGENCY-001"],
        participant_id=PARTICIPANT,
        site_id=SITE,
        occurred_at="2026-09-25T07:12:10+08:00",
    )
    outbox.queue_notification(
        notification_id="NOTIF-DOCTOR-001",
        channel="oncall_doctor",
        target="DR-WANG",
        subject="训练红旗症状待复核",
        body={"incident_id": INCIDENT},
        basis_refs=[stop["event_id"]],
        participant_id=PARTICIPANT,
        site_id=SITE,
        occurred_at="2026-09-25T07:12:11+08:00",
    )
    # 断网期间尝试：两条均失败，本地停训事件早已独立成立
    failed_attempt = outbox.attempt_pending(transmitter)
    assert {r["status"] for r in failed_attempt} == {"FAILED"}
    # 重连：只补送仍未确认的两条；同标识重复入队不会产生第二条通知
    outbox.queue_notification(
        notification_id="NOTIF-120-001",
        channel="emergency_120",
        target="120",
        subject="学员训练中晕厥前兆",
        body={"incident_id": INCIDENT, "symptoms": symptom["data"]["codes"]},
        basis_refs=[stop["event_id"], "EVT-EMERGENCY-001"],
        participant_id=PARTICIPANT,
        site_id=SITE,
    )
    transmitter.online = True
    reconnect_attempt = outbox.flush_on_reconnect(transmitter)
    assert len(reconnect_attempt) == 2
    assert {r["status"] for r in reconnect_attempt} == {"DELIVERED"}
    assert outbox.unconfirmed == []
    assert len(transmitter.sent) == 2  # 失败通知只补发一次

    # 12. 无新专业复核，恢复训练必须被拒绝 -------------------------------
    premature = check_action(
        Action.RESUME_TRAINING,
        actor_role="coach",
        stop_event={"event_id": stop["event_id"], "occurred_at": stop["occurred_at"]},
        reviews=[],
    )
    assert [v.rule_id for v in premature] == ["R-GATE-006"]
    gates["resume_without_review_blocked"] = [v.reason for v in premature]

    put({
        "event_id": "EVT-REVIEW-OPEN-001",
        "event_type": "PROFESSIONAL_REVIEW_OPENED",
        "aggregate_type": "professional_review",
        "aggregate_id": f"PR-{PARTICIPANT}-20260925",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-25T08:00:00+08:00",
        "summary": "120 评估后留观，开立停训后专业复核单",
        "basis_refs": [stop["event_id"], "EVT-EMERGENCY-001"],
    })
    review_closed = put({
        "event_id": "EVT-REVIEW-CLOSE-001",
        "event_type": "PROFESSIONAL_REVIEW_CLOSED",
        "aggregate_type": "professional_review",
        "aggregate_id": f"PR-{PARTICIPANT}-20260925",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-CHEN",
        "actor_role": "medical_director",
        "occurred_at": "2026-09-26T10:00:00+08:00",
        "summary": "医学主管复核：低血糖诱发症状，纠正后可低强度恢复",
        "basis_refs": ["EVT-REVIEW-OPEN-001"],
        "data": {"status": "CLOSED", "finding": "低血糖反应", "conditions": ["运动前补糖", "心率上限110"]},
    })
    now_resume = check_action(
        Action.RESUME_TRAINING,
        actor_role="medical_director",
        stop_event={"event_id": stop["event_id"], "occurred_at": stop["occurred_at"]},
        reviews=[{"status": "CLOSED", "actor_role": "medical_director", "closed_at": review_closed["occurred_at"]}],
    )
    assert now_resume == []
    put({
        "event_id": "EVT-RESUME-001",
        "event_type": "TRAINING_RESUMED",
        "aggregate_type": "participant",
        "aggregate_id": PARTICIPANT,
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-CHEN",
        "actor_role": "medical_director",
        "occurred_at": "2026-09-26T10:05:00+08:00",
        "summary": "凭新专业复核恢复低强度训练",
        "basis_refs": [review_closed["event_id"], stop["event_id"]],
        "review_id": f"PR-{PARTICIPANT}-20260925",
    })

    # 13. 计划变更只作用未来 ---------------------------------------------
    past_revision = check_action(
        Action.REVISE_PLAN,
        actor_role="medical_director",
        revision_effective_at=parse_ts("2026-09-25T00:00:00+08:00"),
        now=parse_ts("2026-09-26T10:06:00+08:00"),
    )
    assert [v.rule_id for v in past_revision] == ["R-PLAN-001"]
    gates["backdated_plan_revision_blocked"] = [v.reason for v in past_revision]
    rx_v2 = put({
        "event_id": "EVT-RX-002",
        "event_type": "PRESCRIPTION_SUPERSEDED",
        "aggregate_type": "prescription",
        "aggregate_id": f"RX-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-26T10:08:00+08:00",
        "effective_at": "2026-09-27T06:00:00+08:00",
        "summary": "处方 v2：餐后训练、运动前补糖、心率上限 110；v1 仅对已完成安排有效",
        "basis_refs": [review_closed["event_id"], "EVT-RX-001"],
        "data": {"intensity": "low", "supersedes": 1, "heart_rate_cap": 110, "pre_exercise_carbs": True},
    })
    put({
        "event_id": "EVT-PLAN-002",
        "event_type": "PLAN_REVISED",
        "aggregate_type": "training_plan",
        "aggregate_id": f"TP-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-WANG",
        "actor_role": "medical_reviewer",
        "occurred_at": "2026-09-26T10:10:00+08:00",
        "effective_at": "2026-09-27T06:00:00+08:00",
        "summary": "计划 v2：晨练改为餐后进行并降低强度增量，历史安排不回填",
        "basis_refs": [review_closed["event_id"], rx_v2["event_id"]],
        "data": {"plan_version": 2, "supersedes": 1},
    })

    # 14. 业绩角色写保护：销售不得改体重/事故；退款不改医疗事实 ---------
    sales_write = check_action(
        Action.WRITE_MEDICAL_FACT,
        actor_role="sales",
        medical_fact_event_type="SAFETY_INCIDENT_OPENED",
    )
    assert {v.rule_id for v in sales_write} == {"R-ACCESS-001", "R-ACCESS-002", "R-ACCESS-003"}
    gates["sales_medical_write_blocked"] = [v.reason for v in sales_write]

    # 门店如实留存员工曾隐瞒停训记录的合规事实（不是改写，是追加）
    put({
        "event_id": "EVT-SUSPENSION-FILE-001",
        "event_type": "SUSPENSION_RECORD_FILED",
        "aggregate_type": "commercial_commitment",
        "aggregate_id": f"CC-{PARTICIPANT}",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "AUDIT-01",
        "actor_role": "administrator",
        "occurred_at": "2026-09-26T11:00:00+08:00",
        "summary": "合规调查：门店员工曾因退款承诺压力漏报停训，已追加纠正记录",
        "basis_refs": [stop["event_id"], "EVT-INCIDENT-OPEN-001"],
    })

    refund_violations = check_action(
        Action.SETTLE_REFUND,
        actor_role="administrator",
        refund_payload={"incident_patch": {"hidden": True}},
    )
    assert [v.rule_id for v in refund_violations] == ["R-COMM-001"]
    gates["refund_altering_medical_facts_blocked"] = [v.reason for v in refund_violations]

    put({
        "event_id": "EVT-REFUND-001",
        "event_type": "REFUND_SETTLED",
        "aggregate_type": "refund_settlement",
        "aggregate_id": f"RF-{PARTICIPANT}-1",
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "FIN-SYS",
        "actor_role": "administrator",
        "occurred_at": "2026-09-26T14:00:00+08:00",
        "summary": "按承诺条款核算退款；医疗事实保持不变",
        "basis_refs": [commitment["event_id"], "EVT-SUSPENSION-FILE-001"],
        "data": {"refund_amount_cny": 4200, "medical_facts_modified": False},
    })
    put({
        "event_id": "EVT-INCIDENT-CLOSE-001",
        "event_type": "SAFETY_INCIDENT_CLOSED",
        "aggregate_type": "safety_incident",
        "aggregate_id": INCIDENT,
        "participant_id": PARTICIPANT,
        "site_id": SITE,
        "actor_id": "DR-CHEN",
        "actor_role": "medical_director",
        "occurred_at": "2026-09-26T15:00:00+08:00",
        "summary": "事故关闭：处置闭环、复核恢复、结算与纠偏均已留痕",
        "basis_refs": ["EVT-REFUND-001", "EVT-RESUME-001", "EVT-REVIEW-CLOSE-001"],
    })

    return ScenarioHandles(
        log=log,
        gateway=gateway,
        outbox=outbox,
        transmitter=transmitter,
        ids=ids,
        gate_results=gates,
    )
