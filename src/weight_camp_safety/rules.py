"""已批准规则集：风险分层与动作门槛。

规则集经医学负责人批准后生效，系统只按规则给出分层与门槛，
结论固定附带"不替代医生诊断"的声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .models import NOT_A_DIAGNOSIS, RED_FLAG_SYMPTOMS, RiskTier


@dataclass(frozen=True)
class MovementThreshold:
    """某一风险分层对应的动作门槛。"""

    tier: RiskTier
    max_heart_rate: int
    max_session_minutes: int
    prohibited_movements: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskStratification:
    """一次分层的结论，始终附带非诊断声明。"""

    tier: RiskTier
    threshold: MovementThreshold
    rule_set_id: str
    rule_version: int
    disclaimer: str = NOT_A_DIAGNOSIS


@dataclass(frozen=True)
class ApprovedRuleSet:
    """经批准生效的规则集，系统不得越权诊断。"""

    rule_set_id: str
    version: int
    approved_by: str
    approved_at: datetime
    thresholds: tuple[MovementThreshold, ...]
    high_risk_conditions: frozenset[str] = frozenset()
    medium_risk_conditions: frozenset[str] = frozenset()
    high_bmi: float = 32.0
    medium_bmi: float = 28.0
    red_flag_symptoms: frozenset[str] = RED_FLAG_SYMPTOMS

    def threshold_for(self, tier: RiskTier) -> MovementThreshold:
        for threshold in self.thresholds:
            if threshold.tier is tier:
                return threshold
        raise KeyError(f"规则集缺少分层 {tier.value} 的动作门槛")

    def stratify(self, assessment: Mapping[str, Any]) -> RiskStratification:
        """依据医学评估给出分层；仅供训练安排参考，不构成诊断。"""
        tier = self._tier_of(assessment)
        return RiskStratification(
            tier=tier,
            threshold=self.threshold_for(tier),
            rule_set_id=self.rule_set_id,
            rule_version=self.version,
        )

    def movement_allowed(self, tier: RiskTier, movement: str) -> bool:
        prohibited = self.threshold_for(tier).prohibited_movements
        return "*" not in prohibited and movement not in prohibited

    def _tier_of(self, assessment: Mapping[str, Any]) -> RiskTier:
        if assessment.get("doctor_contraindicated"):
            return RiskTier.CONTRAINDICATED
        conditions = set(assessment.get("conditions", ()))
        bmi = _bmi(assessment)
        if conditions & self.high_risk_conditions or bmi >= self.high_bmi:
            return RiskTier.HIGH
        if conditions & self.medium_risk_conditions or bmi >= self.medium_bmi:
            return RiskTier.MEDIUM
        return RiskTier.LOW


def _bmi(assessment: Mapping[str, Any]) -> float:
    weight = float(assessment.get("original_weight_kg", 0.0) or 0.0)
    height_cm = float(assessment.get("height_cm", 0.0) or 0.0)
    if weight <= 0 or height_cm <= 0:
        return 0.0
    height_m = height_cm / 100.0
    return weight / (height_m * height_m)
