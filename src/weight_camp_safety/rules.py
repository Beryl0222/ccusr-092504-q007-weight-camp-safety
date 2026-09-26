"""已批准规则库：风险分层、动作门槛与即时停训信号。

规则只产出**分层结论与动作门槛**并回引规则版本与依据事件，
不替代医生诊断；禁忌意见与复核结论必须由 medical_reviewer /
medical_director 角色的专业人员作出。

规则集合以版本号冻结（``approved_rule_set``），规则调整必须走
新版本审批，历史结论因此可按当时生效的规则复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping, Sequence


RULE_BOOK_VERSION = "2026.09-approved"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RiskTier(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class Action(str, Enum):
    ACTIVATE_PLAN = "ACTIVATE_PLAN"
    START_SESSION = "START_SESSION"
    RESUME_TRAINING = "RESUME_TRAINING"
    ISSUE_PRESCRIPTION = "ISSUE_PRESCRIPTION"
    SETTLE_REFUND = "SETTLE_REFUND"
    WRITE_MEDICAL_FACT = "WRITE_MEDICAL_FACT"
    REVISE_PLAN = "REVISE_PLAN"


# 即时停训症状信号（场景硬性要求：胸闷、心悸、头晕、明显乏力）。
RED_FLAG_SYMPTOMS: dict[str, str] = {
    "CHEST_TIGHTNESS": "胸闷",
    "PALPITATIONS": "心悸",
    "DIZZINESS": "头晕",
    "MARKED_FATIGUE": "明显乏力",
    "SYNCOPE": "晕厥",
}

# 入营问卷/医学评估中的高危基础疾病代码。
HIGH_RISK_CONDITIONS: dict[str, str] = {
    "CVD": "心血管疾病",
    "UNCONTROLLED_HTN": "未控制的高血压",
    "ARRHYTHMIA": "心律失常",
    "SYNCOPE_HISTORY": "不明原因晕厥史",
    "UNSTABLE_METABOLIC": "未稳定的代谢急症",
}
MODERATE_RISK_CONDITIONS: dict[str, str] = {
    "CONTROLLED_HTN": "控制稳定的高血压",
    "DIABETES": "糖尿病",
    "SEVERE_OBESITY": "重度肥胖(BMI>=40)",
    "PREGNANCY": "妊娠期",
}

# 体征门槛（经批准的固定阈值，超出即停训）。单位随字段约定。
VITAL_THRESHOLDS: dict[str, tuple[float, float]] = {
    # 静息/运动中收缩压下限、上限（mmHg）
    "systolic_bp": (90.0, 180.0),
    "diastolic_bp": (50.0, 110.0),
    # 心率（次/分）
    "heart_rate": (40.0, 150.0),
    # 血氧 SpO2（%），仅有下限
    "spo2": (92.0, 100.0),
}

# 业绩相关角色不得触碰医疗事实。
PERFORMANCE_ROLES: frozenset[str] = frozenset({"sales"})
MEDICAL_WRITER_ROLES: frozenset[str] = frozenset(
    {"medical_reviewer", "medical_director", "system", "coach", "participant"}
)
PROFESSIONAL_ROLES: frozenset[str] = frozenset({"medical_reviewer", "medical_director"})

RISK_TIER_INTENSITY_CAP: dict[RiskTier, frozenset[str]] = {
    RiskTier.LOW: frozenset({"low", "moderate", "high"}),
    RiskTier.MODERATE: frozenset({"low", "moderate"}),
    RiskTier.HIGH: frozenset({"low"}),
}


@dataclass(frozen=True)
class TriggeredRule:
    rule_id: str
    detail: str
    basis: str  # 触发该规则的输入标识（症状记录/体征记录/评估事件 id）


@dataclass(frozen=True)
class StopDecision:
    must_stop: bool
    escalate: bool
    triggered: tuple[TriggeredRule, ...]
    rule_book_version: str = RULE_BOOK_VERSION


@dataclass(frozen=True)
class RiskDecision:
    tier: RiskTier
    triggered: tuple[TriggeredRule, ...]
    intensity_cap: frozenset[str]
    requires_referral: bool
    requires_medical_prescription: bool
    rule_book_version: str = RULE_BOOK_VERSION
    disclaimer: str = "系统分层依据已批准规则生成，不替代医生诊断；禁忌与转诊以专业意见为准。"


@dataclass(frozen=True)
class GateViolation:
    rule_id: str
    action: Action
    reason: str


@dataclass(frozen=True)
class ApprovedRuleSet:
    version: str
    red_flag_symptoms: Mapping[str, str] = field(default_factory=lambda: dict(RED_FLAG_SYMPTOMS))
    high_risk_conditions: Mapping[str, str] = field(default_factory=lambda: dict(HIGH_RISK_CONDITIONS))
    moderate_risk_conditions: Mapping[str, str] = field(default_factory=lambda: dict(MODERATE_RISK_CONDITIONS))
    vital_thresholds: Mapping[str, tuple[float, float]] = field(default_factory=lambda: dict(VITAL_THRESHOLDS))


def approved_rule_set() -> ApprovedRuleSet:
    """当前唯一批准生效的规则集合（冻结）。"""
    return ApprovedRuleSet(version=RULE_BOOK_VERSION)


# ---------------------------------------------------------------------------
# 即时停训信号
# ---------------------------------------------------------------------------

def evaluate_stop_signals(
    symptoms: Iterable[Mapping[str, str]] = (),
    vitals: Iterable[Mapping[str, object]] = (),
    rules: ApprovedRuleSet | None = None,
) -> StopDecision:
    """症状与体征任一命中即"即时停训 + 升级"，多条信号全部保留作为依据。

    symptoms: [{"event_id"/"record_id": ..., "code": "CHEST_TIGHTNESS"}]
    vitals:  [{"record_id": ..., "metric": "systolic_bp", "value": 185}]
    """
    rules = rules or approved_rule_set()
    triggered: list[TriggeredRule] = []

    for item in symptoms:
        code = item.get("code")
        label = rules.red_flag_symptoms.get(code)  # type: ignore[arg-type]
        if label is not None:
            triggered.append(
                TriggeredRule(
                    "R-STOP-001",
                    f"红旗症状：{label}（{code}），立即停训并升级处置",
                    str(item.get("record_id") or item.get("event_id") or code),
                )
            )

    for item in vitals:
        metric = str(item.get("metric"))
        bounds = rules.vital_thresholds.get(metric)
        if bounds is None:
            continue
        try:
            value = float(item["value"])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        low, high = bounds
        if value < low or value > high:
            triggered.append(
                TriggeredRule(
                    "R-STOP-002",
                    f"体征 {metric}={value:g} 超出批准门槛 [{low:g}, {high:g}]，立即停训并升级",
                    str(item.get("record_id") or item.get("event_id") or metric),
                )
            )

    return StopDecision(must_stop=bool(triggered), escalate=bool(triggered), triggered=tuple(triggered))


# ---------------------------------------------------------------------------
# 风险分层
# ---------------------------------------------------------------------------

def stratify_risk(
    assessment: Mapping[str, object],
    rules: ApprovedRuleSet | None = None,
) -> RiskDecision:
    """根据已批准规则对入营医学/运动评估分层。

    assessment: {"event_id": ..., "conditions": ["CVD", ...], "bmi": 42.1,
                 "contraindication_open": bool}
    任一高危条件命中即 HIGH；中危条件命中即 MODERATE；否则 LOW。
    """
    rules = rules or approved_rule_set()
    basis = str(assessment.get("event_id") or "assessment")
    conditions = set(assessment.get("conditions") or ())  # type: ignore[arg-type]

    triggered: list[TriggeredRule] = []
    for code in conditions:
        if code in rules.high_risk_conditions:
            triggered.append(
                TriggeredRule("R-RISK-001", f"高危基础疾病：{rules.high_risk_conditions[code]}", basis)
            )
        elif code in rules.moderate_risk_conditions:
            triggered.append(
                TriggeredRule("R-RISK-002", f"中危基础情况：{rules.moderate_risk_conditions[code]}", basis)
            )

    bmi = assessment.get("bmi")
    if isinstance(bmi, (int, float)) and not isinstance(bmi, bool) and bmi >= 40 and "SEVERE_OBESITY" not in conditions:
        triggered.append(TriggeredRule("R-RISK-002", f"BMI={bmi:g} 达重度肥胖", basis))

    if assessment.get("contraindication_open"):
        triggered.append(TriggeredRule("R-RISK-003", "存在尚未关闭的禁忌意见", basis))

    if any(t.rule_id in ("R-RISK-001", "R-RISK-003") for t in triggered):
        tier = RiskTier.HIGH
    elif triggered:
        tier = RiskTier.MODERATE
    else:
        tier = RiskTier.LOW

    return RiskDecision(
        tier=tier,
        triggered=tuple(triggered),
        intensity_cap=RISK_TIER_INTENSITY_CAP[tier],
        requires_referral=tier is RiskTier.HIGH,
        requires_medical_prescription=tier in (RiskTier.MODERATE, RiskTier.HIGH),
    )


# ---------------------------------------------------------------------------
# 动作门槛
# ---------------------------------------------------------------------------

def _violation(action: Action, rule_id: str, reason: str) -> GateViolation:
    return GateViolation(rule_id, action, reason)


def check_action(
    action: Action,
    *,
    rules: ApprovedRuleSet | None = None,
    actor_role: str | None = None,
    consent_active: bool | None = None,
    risk: RiskDecision | None = None,
    prescription_intensity: str | None = None,
    contraindication_open: bool = False,
    referral_cleared: bool = False,
    coach: Mapping[str, object] | None = None,
    at: datetime | None = None,
    stop_event: Mapping[str, object] | None = None,
    reviews: Sequence[Mapping[str, object]] = (),
    revision_effective_at: datetime | None = None,
    now: datetime | None = None,
    medical_fact_event_type: str | None = None,
    refund_payload: Mapping[str, object] | None = None,
) -> list[GateViolation]:
    """对一个拟执行动作检查已批准门槛，返回全部违例（空列表=放行）。

    只做判定不执行副作用；调用方负责把违例与依据写回事件流。
    """
    rules = rules or approved_rule_set()
    out: list[GateViolation] = []

    # 通用写保护：业绩角色不得写医疗事实；原始体重/不良事件不可被业绩人员修改。
    if actor_role in PERFORMANCE_ROLES and action in (
        Action.WRITE_MEDICAL_FACT,
        Action.ISSUE_PRESCRIPTION,
        Action.REVISE_PLAN,
    ):
        out.append(_violation(action, "R-ACCESS-001", "销售/业绩角色不得写入或变更医疗事实与处方"))

    if action is Action.WRITE_MEDICAL_FACT:
        if actor_role not in MEDICAL_WRITER_ROLES:
            out.append(_violation(action, "R-ACCESS-002", "该角色无权留存医疗事实"))
        if medical_fact_event_type in (
            "WEIGHT_UPDATED",
            "SAFETY_INCIDENT_OPENED",
            "EMERGENCY_RESPONDED",
            "VITALS_RECORDED",
        ) and actor_role in PERFORMANCE_ROLES:
            out.append(_violation(action, "R-ACCESS-003", "原始体重与不良事件追加只读，业绩人员不可修改"))
        return out

    if action in (Action.ACTIVATE_PLAN, Action.START_SESSION):
        if consent_active is False:
            out.append(_violation(action, "R-GATE-001", "缺少有效学员同意，不得开始评估后训练安排"))
        if risk is not None:
            if risk.requires_referral and not referral_cleared:
                out.append(_violation(action, "R-GATE-007", "高危分层须完成转诊并取得反馈后才能开始"))
        if contraindication_open:
            out.append(_violation(action, "R-GATE-002", "存在未关闭禁忌意见，计划不得激活/开课"))
        if risk is not None and prescription_intensity is not None:
            if prescription_intensity not in risk.intensity_cap:
                out.append(
                    _violation(
                        action,
                        "R-GATE-003",
                        f"{risk.tier.value} 风险分层不允许 {prescription_intensity} 强度",
                    )
                )
        if action is Action.START_SESSION:
            cert_issue = _coach_cert_issue(coach, at or now)
            if cert_issue:
                out.append(_violation(Action.START_SESSION, "R-GATE-004", cert_issue))

    if action is Action.ISSUE_PRESCRIPTION:
        if actor_role not in PROFESSIONAL_ROLES and actor_role != "system":
            out.append(_violation(action, "R-GATE-005", "饮食/训练处方须由专业复核角色签发或系统按批准模板生成"))
        if risk is not None and prescription_intensity is not None and prescription_intensity not in risk.intensity_cap:
            out.append(_violation(action, "R-GATE-003", f"{risk.tier.value} 风险分层不允许 {prescription_intensity} 强度"))

    if action is Action.RESUME_TRAINING:
        issue = _resume_review_issue(stop_event, reviews, actor_role)
        if issue:
            out.append(_violation(Action.RESUME_TRAINING, "R-GATE-006", issue))

    if action is Action.REVISE_PLAN:
        if revision_effective_at is None:
            out.append(_violation(action, "R-PLAN-001", "计划变更必须指定生效时间"))
        elif now is not None and revision_effective_at <= now:
            out.append(_violation(action, "R-PLAN-001", "计划变更只作用于未来安排，生效时间必须晚于当前时间"))

    if action is Action.SETTLE_REFUND:
        if refund_payload:
            for forbidden in ("medical_fact_overrides", "incident_patch", "vitals_patch", "weight_patch", "stop_basis_patch"):
                if forbidden in refund_payload:
                    out.append(
                        _violation(
                            action,
                            "R-COMM-001",
                            "退款结算不得反向改写医疗事实（停训依据/不良事件/体征/原始体重）",
                        )
                    )
                    break

    return out


def _coach_cert_issue(coach: Mapping[str, object] | None, at: datetime | None) -> str | None:
    if not coach:
        return "开课必须登记当班教练"
    if coach.get("revoked"):
        return f"教练 {coach.get('coach_id')} 资质已被撤销"
    valid_from = coach.get("valid_from")
    valid_until = coach.get("valid_until")
    if not valid_from or not valid_until:
        return "教练资质缺少有效期"
    moment = at or datetime.now().astimezone()
    start = valid_from if isinstance(valid_from, datetime) else parse_ts(str(valid_from))
    end = valid_until if isinstance(valid_until, datetime) else parse_ts(str(valid_until))
    if start.tzinfo is None or end.tzinfo is None:
        return "教练资质有效期必须含时区"
    if not (start <= moment <= end):
        return f"教练 {coach.get('coach_id')} 资质已过期或尚未生效"
    return None


def _resume_review_issue(
    stop_event: Mapping[str, object] | None,
    reviews: Sequence[Mapping[str, object]],
    actor_role: str | None,
) -> str | None:
    """红旗停训后只有"新的"专业复核可以恢复：复核关闭时间必须晚于停训时间。"""
    if not stop_event:
        return "恢复训练必须指回原停训事件"
    stopped_at_raw = stop_event.get("occurred_at")
    if not stopped_at_raw:
        return "停训事件缺少发生时间"
    stopped_at = stopped_at_raw if isinstance(stopped_at_raw, datetime) else parse_ts(str(stopped_at_raw))

    closed_after = [
        r
        for r in reviews
        if r.get("status") == "CLOSED"
        and r.get("actor_role") in PROFESSIONAL_ROLES
        and _parse_optional_ts(r.get("closed_at")) is not None
        and _parse_optional_ts(r.get("closed_at")) > stopped_at  # type: ignore[operator]
    ]
    if not closed_after:
        return "红旗停训后必须由专业人员完成新的复核（关闭时间晚于停训时间）方可恢复"
    if actor_role not in PROFESSIONAL_ROLES and actor_role != "system":
        return "只有专业复核角色可确认恢复训练"
    return None


def _parse_optional_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return parse_ts(value)
    return None
