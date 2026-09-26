"""减重训练安全干预台的领域对象与共享常量。"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """系统内的操作角色，用于档案写入权限判定。"""

    PARTICIPANT = "participant"
    COACH = "coach"
    MEDICAL_REVIEWER = "medical_reviewer"
    PERFORMANCE_STAFF = "performance_staff"
    REGULATOR = "regulator"
    SYSTEM = "system"


class RecordKind(str, Enum):
    """需要分别留存版本的档案类别。"""

    CONSENT = "consent"
    MEDICAL_ASSESSMENT = "medical_assessment"
    EXERCISE_ASSESSMENT = "exercise_assessment"
    CONTRAINDICATION = "contraindication"
    REFERRAL = "referral"
    COACH_CREDENTIAL = "coach_credential"
    PRESCRIPTION = "prescription"
    DAILY_EXECUTION = "daily_execution"
    VITAL_SIGN = "vital_sign"
    EMERGENCY_RESPONSE = "emergency_response"
    COMMERCIAL_PROMISE = "commercial_promise"


class RiskTier(str, Enum):
    """规则集输出的风险分层。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CONTRAINDICATED = "contraindicated"


#: 触发即时停训与升级的红旗症状。
RED_FLAG_SYMPTOMS: frozenset[str] = frozenset({"胸闷", "心悸", "头晕", "明显乏力"})

#: 系统结论的固定声明：分层与门槛不替代医生诊断。
NOT_A_DIAGNOSIS = "系统依据已批准规则给出风险分层与动作门槛，仅供训练安排参考，不能替代医生诊断。"
