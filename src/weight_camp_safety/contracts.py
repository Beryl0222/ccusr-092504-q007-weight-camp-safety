"""领域事件交换契约的基础校验。

校验器只负责交换层（必填、类型、时间、版本、登记值），不在此处做
业务判定；风险分层、停训门槛、写保护等规则见 ``rules`` 模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


# 已登记的事件类型。新增业务事实必须在此登记，避免跨系统出现私有口径。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "CONSENT_GRANTED",
        "CONSENT_WITHDRAWN",
        "ASSESSMENT_APPROVED",
        "MEDICAL_REVIEW_RECORDED",
        "RISK_STRATIFIED",
        "CONTRAINDICATION_ISSUED",
        "REFERRAL_REQUESTED",
        "REFERRAL_FEEDBACK_RECORDED",
        "PROFESSIONAL_REVIEW_OPENED",
        "PROFESSIONAL_REVIEW_CLOSED",
        "COACH_CERTIFICATION_RECORDED",
        "COACH_CERTIFICATION_REVOKED",
        "PRESCRIPTION_ISSUED",
        "PRESCRIPTION_SUPERSEDED",
        "PLAN_ACTIVATED",
        "PLAN_REVISED",
        "SESSION_RECORDED",
        "VITALS_RECORDED",
        "SYMPTOM_REPORTED",
        "TRAINING_STOPPED",
        "TRAINING_RESUMED",
        "SAFETY_INCIDENT_OPENED",
        "EMERGENCY_RESPONDED",
        "SAFETY_INCIDENT_CLOSED",
        "FOLLOWUP_CLOSED",
        "COMMITMENT_RECORDED",
        "SUSPENSION_RECORD_FILED",
        "REFUND_SETTLED",
        "NOTIFICATION_QUEUED",
        "NOTIFICATION_DELIVERED",
        "NOTIFICATION_FAILED",
        "INGESTION_DEDUPED",
        "DATA_REVIEW_OPENED",
        "DATA_REVIEW_RESOLVED",
    }
)

AGGREGATE_TYPES: frozenset[str] = frozenset(
    {
        "participant",
        "consent",
        "medical_assessment",
        "risk_assessment",
        "contraindication_opinion",
        "referral",
        "professional_review",
        "coach_certification",
        "prescription",
        "training_plan",
        "session_execution",
        "vital_sign",
        "safety_incident",
        "commercial_commitment",
        "refund_settlement",
        "emergency_notification",
        "ingestion_record",
    }
)

ACTOR_ROLES: frozenset[str] = frozenset(
    {
        "participant",
        "coach",
        "site_staff",
        "sales",
        "medical_reviewer",
        "medical_director",
        "system",
        "regulator",
        "administrator",
    }
)

# 医疗事实一旦写入只能追加新版本，业绩/销售角色不得改写。
IMMUTABLE_FACTS: frozenset[str] = frozenset(
    {
        "medical_assessment",
        "contraindication_opinion",
        "vital_sign",
        "safety_incident",
        "session_execution",
    }
)


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


def _has_timezone(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _issue(issues: list[ContractIssue], field: str, code: str, message: str) -> None:
    issues.append(ContractIssue(field, code, message))


def validate_event(payload: Any, schema: Mapping[str, Any] | None = None) -> list[ContractIssue]:
    """返回稳定排序的问题列表，不在交换层改写输入。

    无 schema 入参时使用本模块登记的常量集合，便于服务侧直接调用；
    传入 schema 时以 schema 的 enum 为准，便于契约测试对照 JSON 文件。
    """
    if not isinstance(payload, Mapping):
        return [ContractIssue("$", "object_required", "事件必须是 JSON 对象")]

    issues: list[ContractIssue] = []
    required = (schema or {}).get("required", [
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "version",
        "summary",
    ])
    for field in required:
        if field not in payload:
            _issue(issues, str(field), "required", "缺少必填字段")

    for field in ("event_id", "aggregate_type", "aggregate_id", "summary"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            _issue(issues, field, "non_empty_string", "字段必须是非空字符串")

    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        _issue(issues, "version", "positive_integer", "版本必须是正整数")

    for field in ("occurred_at", "effective_at"):
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not _has_timezone(value)):
            _issue(issues, field, "timezone_required", "时间必须包含时区")

    properties = (schema or {}).get("properties", {})

    def _allowed(field: str, fallback: frozenset[str]) -> set[str]:
        return set(properties.get(field, {}).get("enum", [])) or set(fallback)

    for field, registered in (
        ("event_type", EVENT_TYPES),
        ("aggregate_type", AGGREGATE_TYPES),
        ("actor_role", ACTOR_ROLES),
    ):
        value = payload.get(field)
        if isinstance(value, str) and value not in _allowed(field, registered):
            _issue(issues, field, "unsupported_value", "字段值未在契约中登记")

    # 幂等键必须成对出现：只有来源标识没有哈希无法判定"同标识异内容"。
    has_source = "source_record_id" in payload
    has_hash = "content_hash" in payload
    if has_source != has_hash:
        _issue(
            issues,
            "source_record_id",
            "idempotency_key_pair_required",
            "source_record_id 与 content_hash 必须同时提供",
        )
    for field in ("source_record_id", "content_hash", "participant_id", "site_id", "actor_id", "review_id"):
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not value.strip()):
            _issue(issues, field, "non_empty_string", "字段必须是非空字符串")

    basis_refs = payload.get("basis_refs")
    if basis_refs is not None:
        if not isinstance(basis_refs, list) or not basis_refs:
            _issue(issues, "basis_refs", "non_empty_array", "依据引用必须是非空数组")
        elif any(not isinstance(ref, str) or not ref.strip() for ref in basis_refs) or len(set(basis_refs)) != len(basis_refs):
            _issue(issues, "basis_refs", "unique_non_empty_strings", "依据引用必须是去重的非空字符串")

    return sorted(issues, key=lambda issue: (issue.field, issue.code))
