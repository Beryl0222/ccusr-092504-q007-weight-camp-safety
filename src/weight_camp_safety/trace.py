"""只读视图：监管事故追溯链路与学员核对视图。

视图不产生新事实，只对追加日志中的事件做串联：
- 监管链路：事故 → 入营评估/同意 → 当班人员资质 → 处方版本 →
  计划版本与每日执行 → 体征/症状 → 每次停训与应急处置 → 复核恢复 → 商业结算；
- 学员视图：我为什么被停训（依据到规则与原始记录）、后续安排、退款进度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .eventlog import EventLog
from .rules import parse_ts


# 追溯链路各环节登记的事件类型
SECTION_ORDER: list[tuple[str, frozenset[str]]] = [
    ("consent", frozenset({"CONSENT_GRANTED", "CONSENT_WITHDRAWN"})),
    ("intake_assessment", frozenset({"ASSESSMENT_APPROVED", "MEDICAL_REVIEW_RECORDED"})),
    ("risk_stratification", frozenset({"RISK_STRATIFIED"})),
    ("contraindication_and_referral", frozenset(
        {"CONTRAINDICATION_ISSUED", "REFERRAL_REQUESTED", "REFERRAL_FEEDBACK_RECORDED"}
    )),
    ("coach_on_duty", frozenset({"COACH_CERTIFICATION_RECORDED", "COACH_CERTIFICATION_REVOKED"})),
    ("prescription_versions", frozenset({"PRESCRIPTION_ISSUED", "PRESCRIPTION_SUPERSEDED"})),
    ("plan_versions", frozenset({"PLAN_ACTIVATED", "PLAN_REVISED"})),
    ("daily_execution", frozenset({"SESSION_RECORDED"})),
    ("vitals_and_symptoms", frozenset({"VITALS_RECORDED", "SYMPTOM_REPORTED"})),
    ("stops_and_dispositions", frozenset(
        {"TRAINING_STOPPED", "EMERGENCY_RESPONDED", "SAFETY_INCIDENT_OPENED",
         "SAFETY_INCIDENT_CLOSED", "FOLLOWUP_CLOSED"}
    )),
    ("professional_reviews", frozenset({"PROFESSIONAL_REVIEW_OPENED", "PROFESSIONAL_REVIEW_CLOSED"})),
    ("commercial", frozenset({"COMMITMENT_RECORDED", "SUSPENSION_RECORD_FILED", "REFUND_SETTLED"})),
    ("notifications", frozenset({"NOTIFICATION_QUEUED", "NOTIFICATION_DELIVERED", "NOTIFICATION_FAILED"})),
    ("data_ingestion", frozenset({"INGESTION_DEDUPED", "DATA_REVIEW_OPENED", "DATA_REVIEW_RESOLVED"})),
]

_EVENT_SECTION: dict[str, str] = {
    etype: section for section, types in SECTION_ORDER for etype in types
}


def _sorted_by_time(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda e: (parse_ts(e["occurred_at"]), e["event_id"]))


@dataclass
class BasisClosure:
    missing: list[tuple[str, str]] = field(default_factory=list)
    dangling: list[tuple[str, str]] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return not self.missing and not self.dangling


def check_basis_closure(log: EventLog, events: list[dict[str, Any]]) -> BasisClosure:
    """每个 basis_refs 引用必须指向已存在事件；事故链路尤其不能断引。"""
    closure = BasisClosure()
    for event in events:
        for ref in event.get("basis_refs", ()):
            target = log.get(ref)
            if target is None:
                closure.missing.append((event["event_id"], ref))
            elif target.get("participant_id") and event.get("participant_id") and target["participant_id"] != event["participant_id"]:
                closure.dangling.append((event["event_id"], ref))
    return closure


def build_incident_trace(log: EventLog, incident_id: str) -> dict[str, Any]:
    """从一个 safety_incident 出发，重建全链路监管证据包。"""
    incident_events = log.stream("safety_incident", incident_id)
    if not incident_events:
        raise KeyError(f"事故不存在: {incident_id}")

    participant_ids = {e.get("participant_id") for e in incident_events if e.get("participant_id")}
    site_ids = {e.get("site_id") for e in incident_events if e.get("site_id")}
    if not participant_ids:
        raise ValueError(f"事故 {incident_id} 缺少 participant_id，无法建立追溯链路")
    participant_id = next(iter(participant_ids))

    sections: dict[str, list[dict[str, Any]]] = {section: [] for section, _ in SECTION_ORDER}
    sections["incident"] = _sorted_by_time(incident_events)

    participant_events = [
        e for e in log.all_events() if e.get("participant_id") == participant_id
    ]
    # 教练资质按门店入网（不带 participant_id），用事故门店关联当班人员证据
    coach_events = [
        e
        for e in log.all_events()
        if e["aggregate_type"] == "coach_certification" and (not site_ids or e.get("site_id") in site_ids)
    ]

    for event in _sorted_by_time(participant_events) + _sorted_by_time(coach_events):
        section = _EVENT_SECTION.get(event["event_type"])
        if section is not None:
            sections[section].append(event)

    # 每日执行里只保留实际带训教练证据（其余已由 participant_id 过滤）
    closure = check_basis_closure(log, participant_events + coach_events + incident_events)

    on_duty = []
    for session in sections["daily_execution"]:
        if session.get("actor_role") == "coach":
            on_duty.append(
                {
                    "session_event_id": session["event_id"],
                    "coach_id": session.get("actor_id"),
                    "site_id": session.get("site_id"),
                    "at": session["occurred_at"],
                    "plan_version": session.get("data", {}).get("plan_version"),
                    "prescription_version": session.get("data", {}).get("prescription_version"),
                }
            )

    return {
        "incident_id": incident_id,
        "participant_id": participant_id,
        "site_ids": sorted(site_ids),
        "sections": sections,
        "on_duty_coaches": on_duty,
        "basis_closure": closure,
    }


@dataclass
class ParticipantView:
    participant_id: str
    consent_active: bool
    current_risk_tier: str | None
    stopped: bool
    stop_basis: list[dict[str, Any]]
    next_arrangements: list[dict[str, str]]
    refund_progress: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "consent_active": self.consent_active,
            "current_risk_tier": self.current_risk_tier,
            "stopped": self.stopped,
            "stop_basis": self.stop_basis,
            "next_arrangements": self.next_arrangements,
            "refund_progress": self.refund_progress,
        }


def build_participant_view(log: EventLog, participant_id: str) -> ParticipantView:
    events = _sorted_by_time(
        [e for e in log.all_events() if e.get("participant_id") == participant_id]
    )

    consent_active = False
    risk_tier: str | None = None
    stop_events: list[dict[str, Any]] = []
    stopped = False
    arrangements: list[dict[str, str]] = []

    commitments: list[dict[str, Any]] = []
    suspensions: list[dict[str, Any]] = []
    refunds: list[dict[str, Any]] = []

    for event in events:
        etype = event["event_type"]
        if etype == "CONSENT_GRANTED":
            consent_active = True
        elif etype == "CONSENT_WITHDRAWN":
            consent_active = False
        elif etype == "RISK_STRATIFIED":
            risk_tier = str(event.get("data", {}).get("tier"))
        elif etype == "TRAINING_STOPPED":
            stop_events.append(event)
            stopped = True
            arrangements.append({
                "type": "STOP",
                "at": event["occurred_at"],
                "detail": event["summary"],
                "event_id": event["event_id"],
            })
        elif etype == "PROFESSIONAL_REVIEW_OPENED":
            arrangements.append({
                "type": "REVIEW_PENDING",
                "at": event["occurred_at"],
                "detail": event["summary"],
                "event_id": event["event_id"],
            })
        elif etype == "PROFESSIONAL_REVIEW_CLOSED":
            arrangements.append({
                "type": "REVIEW_CLOSED",
                "at": event["occurred_at"],
                "detail": event["summary"],
                "event_id": event["event_id"],
            })
        elif etype == "TRAINING_RESUMED":
            stopped = False
            arrangements.append({
                "type": "RESUMED",
                "at": event["occurred_at"],
                "detail": event["summary"],
                "event_id": event["event_id"],
            })
        elif etype in ("REFERRAL_REQUESTED", "REFERRAL_FEEDBACK_RECORDED", "PLAN_REVISED"):
            arrangements.append({
                "type": etype,
                "at": event["occurred_at"],
                "detail": event["summary"],
                "event_id": event["event_id"],
            })
        elif etype == "COMMITMENT_RECORDED":
            commitments.append(event)
        elif etype == "SUSPENSION_RECORD_FILED":
            suspensions.append(event)
        elif etype == "REFUND_SETTLED":
            refunds.append(event)

    latest_stop = stop_events[-1] if stop_events else None

    stop_basis: list[dict[str, Any]] = []
    if latest_stop is not None:
        data = latest_stop.get("data", {})
        stop_basis.append(
            {
                "stop_event_id": latest_stop["event_id"],
                "at": latest_stop["occurred_at"],
                "summary": latest_stop["summary"],
                "resume_policy": data.get("resume_policy"),
                "reasons": data.get("reasons", []),
                "source_record_ids": list(latest_stop.get("basis_refs", [])),
                "rule_book_version": data.get("rule_book_version"),
            }
        )

    refund_progress = {
        "commitments": [
            {"event_id": e["event_id"], "terms": e.get("data", {}), "at": e["occurred_at"]}
            for e in commitments
        ],
        "suspension_records": [
            {"event_id": e["event_id"], "summary": e["summary"], "at": e["occurred_at"]}
            for e in suspensions
        ],
        "latest_settlement": (
            {
                "event_id": refunds[-1]["event_id"],
                "at": refunds[-1]["occurred_at"],
                "detail": refunds[-1].get("data", {}),
            }
            if refunds
            else None
        ),
    }

    return ParticipantView(
        participant_id=participant_id,
        consent_active=consent_active,
        current_risk_tier=risk_tier,
        stopped=stopped,
        stop_basis=stop_basis,
        next_arrangements=arrangements,
        refund_progress=refund_progress,
    )
