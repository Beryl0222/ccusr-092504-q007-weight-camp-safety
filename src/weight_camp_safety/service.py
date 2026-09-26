"""训练风险干预服务：在领域契约之上编排安全流程。

核心不变量：
- 红旗症状触发即时停训与升级，应急外呼失败不阻塞本地停训；
- 只有停训之后新的专业复核可以恢复训练；
- 计划变更只作用未来安排，历史执行与医疗事实不可回溯改写；
- 退款结算只写结算记录，不反向改变医疗事实。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .archive import RecordVersion, VersionedArchive
from .ingest import IngestGateway, IngestResult, IngestStatus
from .models import RecordKind, RiskTier, Role
from .notify import EmergencyNotifier, Notification
from .rules import ApprovedRuleSet, RiskStratification


class InterventionError(Exception):
    """干预流程被安全约束拒绝。"""


class CoachCredentialExpired(InterventionError):
    """教练资质缺失或已过期。"""


class RetroactivePlanChange(InterventionError):
    """计划变更只允许作用于未来安排。"""


class ResumeWithoutReview(InterventionError):
    """只有新的专业复核可以恢复训练。"""


class ActiveStopError(InterventionError):
    """停训期间禁止安排训练执行。"""


class MovementNotAllowed(InterventionError):
    """动作超出当前风险分层的门槛。"""


@dataclass(frozen=True)
class TrainingStop:
    """一次即时停训及其后续安排。"""

    stop_id: str
    participant_id: str
    coach_id: str
    reason: str
    symptoms: tuple[str, ...]
    triggered_at: datetime
    prescription_version: int
    follow_ups: tuple[str, ...] = ()
    resumed_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.resumed_at is None


@dataclass(frozen=True)
class ProfessionalReview:
    """停训后的专业复核结论。"""

    review_id: str
    stop_id: str
    reviewer: str
    reviewed_at: datetime
    decision: str  # "resume" | "maintain"
    note: str = ""


@dataclass(frozen=True)
class RefundSettlement:
    """退款结算记录，与医疗事实隔离。"""

    settlement_id: str
    participant_id: str
    promise_id: str
    amount: float
    status: str  # "pending" | "paid"
    settled_by: Role
    settled_at: datetime


@dataclass(frozen=True)
class IncidentTrace:
    """监管追溯视图：从事故回到入营评估、当班人员、处方版本与每次处置。"""

    stop: TrainingStop
    enrollment: dict[str, list[RecordVersion]]
    on_duty_coach: str
    coach_credential: RecordVersion | None
    prescription_versions: list[RecordVersion]
    dispositions: list[RecordVersion]
    notifications: list[Notification]


class InterventionService:
    """训练风险干预服务。"""

    def __init__(
        self,
        archive: VersionedArchive,
        rules: ApprovedRuleSet,
        notifier: EmergencyNotifier,
        ingest: IngestGateway | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.archive = archive
        self.rules = rules
        self.notifier = notifier
        self.ingest = ingest or IngestGateway()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events: list[dict[str, Any]] = []
        self.stratifications: dict[str, RiskStratification] = {}
        self.stops: dict[str, TrainingStop] = {}
        self.reviews: list[ProfessionalReview] = []
        self.settlements: dict[str, RefundSettlement] = {}
        self._stop_seq = 0
        self._review_seq = 0
        self._settlement_seq = 0

    # ------------------------------------------------------------------
    # 入营：同意、评估、禁忌与转诊分别留存版本
    # ------------------------------------------------------------------
    def enroll_participant(
        self,
        participant_id: str,
        consent: Mapping[str, Any],
        medical_assessment: Mapping[str, Any],
        exercise_assessment: Mapping[str, Any],
        actor: Role = Role.MEDICAL_REVIEWER,
    ) -> RiskStratification:
        now = self.clock()
        self.archive.append(RecordKind.CONSENT, participant_id, consent, actor, now)
        self.archive.append(RecordKind.MEDICAL_ASSESSMENT, participant_id, medical_assessment, actor, now)
        self.archive.append(RecordKind.EXERCISE_ASSESSMENT, participant_id, exercise_assessment, actor, now)
        stratification = self.rules.stratify(medical_assessment)
        self.stratifications[participant_id] = stratification
        self._emit(
            "ASSESSMENT_APPROVED",
            "risk_assessment",
            participant_id,
            f"入营评估完成，风险分层 {stratification.tier.value}（{self.rules.rule_set_id} v{self.rules.version}）",
        )
        return stratification

    def record_contraindication(
        self, participant_id: str, payload: Mapping[str, Any], actor: Role = Role.MEDICAL_REVIEWER
    ) -> RecordVersion:
        return self.archive.append(RecordKind.CONTRAINDICATION, participant_id, payload, actor, self.clock())

    def record_referral(
        self, participant_id: str, payload: Mapping[str, Any], actor: Role = Role.MEDICAL_REVIEWER
    ) -> RecordVersion:
        return self.archive.append(RecordKind.REFERRAL, participant_id, payload, actor, self.clock())

    def register_coach_credential(
        self, coach_id: str, payload: Mapping[str, Any], actor: Role = Role.SYSTEM
    ) -> RecordVersion:
        return self.archive.append(RecordKind.COACH_CREDENTIAL, coach_id, payload, actor, self.clock())

    def register_commercial_promise(
        self, promise_id: str, payload: Mapping[str, Any], actor: Role
    ) -> RecordVersion:
        return self.archive.append(RecordKind.COMMERCIAL_PROMISE, promise_id, payload, actor, self.clock())

    # ------------------------------------------------------------------
    # 处方与每日执行：变更只作用未来，执行受分层门槛约束
    # ------------------------------------------------------------------
    def prescribe(
        self,
        participant_id: str,
        payload: Mapping[str, Any],
        effective_from: datetime,
        actor: Role = Role.MEDICAL_REVIEWER,
    ) -> RecordVersion:
        if effective_from < self.clock():
            raise RetroactivePlanChange("计划变更只作用未来安排，不允许回溯生效")
        body = dict(payload, effective_from=effective_from.isoformat())
        version = self.archive.append(RecordKind.PRESCRIPTION, participant_id, body, actor, self.clock())
        self._emit(
            "PRESCRIPTION_UPDATED",
            "training_plan",
            participant_id,
            f"处方第 {version.version} 版自 {body['effective_from']} 起生效",
        )
        return version

    def execute_day(
        self,
        participant_id: str,
        coach_id: str,
        payload: Mapping[str, Any],
        actor: Role = Role.COACH,
    ) -> RecordVersion:
        self._require_no_active_stop(participant_id)
        self._require_valid_coach(coach_id)
        tier = self._tier_of(participant_id)
        for movement in payload.get("movements", ()):
            if not self.rules.movement_allowed(tier, movement):
                raise MovementNotAllowed(f"动作 {movement} 超出分层 {tier.value} 的门槛")
        body = dict(payload, participant_id=participant_id, coach_id=coach_id)
        version = self.archive.append(RecordKind.DAILY_EXECUTION, participant_id, body, actor, self.clock())
        self._emit("SESSION_RECORDED", "participant", participant_id, f"记录第 {version.version} 次每日执行")
        return version

    # ------------------------------------------------------------------
    # 体征症状与即时停训
    # ------------------------------------------------------------------
    def record_vitals(
        self,
        participant_id: str,
        payload: Mapping[str, Any],
        coach_id: str,
        actor: Role = Role.COACH,
    ) -> tuple[RecordVersion, TrainingStop | None]:
        body = dict(payload, participant_id=participant_id, coach_id=coach_id)
        version = self.archive.append(RecordKind.VITAL_SIGN, participant_id, body, actor, self.clock())
        red_flags = tuple(sorted(set(payload.get("symptoms", ())) & self.rules.red_flag_symptoms))
        if red_flags:
            stop = self._trigger_stop(participant_id, coach_id, red_flags)
            return version, stop
        return version, None

    def ingest_wearable(
        self,
        participant_id: str,
        source: str,
        external_id: str,
        content: Mapping[str, Any],
        coach_id: str,
    ) -> tuple[IngestResult, TrainingStop | None]:
        """穿戴数据与离线补传的统一入口：重复到达保持一次记录。"""
        result = self.ingest.receive(source, external_id, content)
        if result.status is IngestStatus.STORED:
            _, stop = self.record_vitals(participant_id, content, coach_id, actor=Role.SYSTEM)
            return result, stop
        return result, None

    def _trigger_stop(self, participant_id: str, coach_id: str, symptoms: tuple[str, ...]) -> TrainingStop:
        self._stop_seq += 1
        now = self.clock()
        latest_rx = self.archive.latest(RecordKind.PRESCRIPTION, participant_id)
        stop = TrainingStop(
            stop_id=f"stop-{self._stop_seq:04d}",
            participant_id=participant_id,
            coach_id=coach_id,
            reason="出现红旗症状，即时停训并升级",
            symptoms=symptoms,
            triggered_at=now,
            prescription_version=latest_rx.version if latest_rx else 0,
        )
        self.stops[stop.stop_id] = stop
        self.archive.append(
            RecordKind.EMERGENCY_RESPONSE,
            stop.stop_id,
            {
                "incident_id": stop.stop_id,
                "participant_id": participant_id,
                "symptoms": list(symptoms),
                "disposition": "即时停训并升级",
            },
            Role.SYSTEM,
            now,
        )
        self._emit("TRAINING_STOPPED", "safety_incident", stop.stop_id, f"学员 {participant_id} 出现 {'、'.join(symptoms)}，即时停训")
        self._emit("ESCALATION_RAISED", "safety_incident", stop.stop_id, "已升级至医疗安全主管")
        # 应急外呼失败不阻塞本地停训；恢复后由 resend_unacknowledged 补送。
        self.notifier.alert(stop.stop_id, "emergency_call", f"学员 {participant_id} 停训：{'、'.join(symptoms)}")
        return stop

    # ------------------------------------------------------------------
    # 复核与恢复
    # ------------------------------------------------------------------
    def submit_review(self, stop_id: str, reviewer: str, decision: str, note: str = "") -> ProfessionalReview:
        if decision not in ("resume", "maintain"):
            raise ValueError("复核结论只能是 resume 或 maintain")
        stop = self.stops[stop_id]
        self._review_seq += 1
        review = ProfessionalReview(
            review_id=f"review-{self._review_seq:04d}",
            stop_id=stop_id,
            reviewer=reviewer,
            reviewed_at=self.clock(),
            decision=decision,
            note=note,
        )
        self.reviews.append(review)
        if decision == "maintain":
            self.archive.append(
                RecordKind.EMERGENCY_RESPONSE,
                stop.stop_id,
                {
                    "incident_id": stop.stop_id,
                    "participant_id": stop.participant_id,
                    "symptoms": list(stop.symptoms),
                    "disposition": "专业复核维持停训",
                    "review_id": review.review_id,
                },
                Role.MEDICAL_REVIEWER,
                review.reviewed_at,
            )
        return review

    def resume_training(self, stop_id: str) -> TrainingStop:
        stop = self.stops[stop_id]
        if not stop.active:
            return stop
        approved = any(
            r.stop_id == stop_id and r.decision == "resume" and r.reviewed_at >= stop.triggered_at
            for r in self.reviews
        )
        if not approved:
            raise ResumeWithoutReview("只有停训之后新的专业复核可以恢复训练")
        stop = replace(stop, resumed_at=self.clock())
        self.stops[stop_id] = stop
        self.archive.append(
            RecordKind.EMERGENCY_RESPONSE,
            stop.stop_id,
            {
                "incident_id": stop.stop_id,
                "participant_id": stop.participant_id,
                "symptoms": list(stop.symptoms),
                "disposition": "专业复核后恢复训练",
            },
            Role.MEDICAL_REVIEWER,
            stop.resumed_at,
        )
        self._emit("TRAINING_RESUMED", "safety_incident", stop.stop_id, "专业复核通过，恢复训练")
        return stop

    def add_follow_up(self, stop_id: str, arrangement: str) -> TrainingStop:
        stop = self.stops[stop_id]
        stop = replace(stop, follow_ups=stop.follow_ups + (arrangement,))
        self.stops[stop_id] = stop
        return stop

    # ------------------------------------------------------------------
    # 退款结算：只写结算记录，不反向改变医疗事实
    # ------------------------------------------------------------------
    def settle_refund(
        self,
        participant_id: str,
        promise_id: str,
        amount: float,
        actor: Role,
        status: str = "pending",
    ) -> RefundSettlement:
        self._settlement_seq += 1
        settlement = RefundSettlement(
            settlement_id=f"refund-{self._settlement_seq:04d}",
            participant_id=participant_id,
            promise_id=promise_id,
            amount=amount,
            status=status,
            settled_by=actor,
            settled_at=self.clock(),
        )
        self.settlements[settlement.settlement_id] = settlement
        self._emit(
            "REFUND_SETTLED",
            "refund_settlement",
            settlement.settlement_id,
            f"学员 {participant_id} 退款 {amount:.2f} 元登记为 {status}",
        )
        return settlement

    def mark_refund_paid(self, settlement_id: str) -> RefundSettlement:
        settlement = replace(self.settlements[settlement_id], status="paid")
        self.settlements[settlement_id] = settlement
        return settlement

    # ------------------------------------------------------------------
    # 学员视图与监管追溯
    # ------------------------------------------------------------------
    def participant_view(self, participant_id: str) -> dict[str, Any]:
        """学员可核对停训依据、后续安排与退款进度。"""
        stratification = self.stratifications.get(participant_id)
        return {
            "participant_id": participant_id,
            "risk_tier": stratification.tier.value if stratification else None,
            "stops": [
                {
                    "stop_id": s.stop_id,
                    "reason": s.reason,
                    "symptoms": list(s.symptoms),
                    "triggered_at": s.triggered_at.isoformat(),
                    "active": s.active,
                    "follow_ups": list(s.follow_ups),
                }
                for s in self.stops.values()
                if s.participant_id == participant_id
            ],
            "refunds": [
                {
                    "settlement_id": r.settlement_id,
                    "amount": r.amount,
                    "status": r.status,
                }
                for r in self.settlements.values()
                if r.participant_id == participant_id
            ],
        }

    def trace_incident(self, stop_id: str) -> IncidentTrace:
        """监管查询：从事故追到入营评估、当班人员、处方版本和每次处置。"""
        stop = self.stops[stop_id]
        participant_id = stop.participant_id
        enrollment = {
            kind.value: self.archive.history(kind, participant_id)
            for kind in (
                RecordKind.CONSENT,
                RecordKind.MEDICAL_ASSESSMENT,
                RecordKind.EXERCISE_ASSESSMENT,
                RecordKind.CONTRAINDICATION,
                RecordKind.REFERRAL,
            )
        }
        return IncidentTrace(
            stop=stop,
            enrollment=enrollment,
            on_duty_coach=stop.coach_id,
            coach_credential=self.archive.latest(RecordKind.COACH_CREDENTIAL, stop.coach_id),
            prescription_versions=self.archive.history(RecordKind.PRESCRIPTION, participant_id),
            dispositions=self.archive.history(RecordKind.EMERGENCY_RESPONSE, stop_id),
            notifications=self.notifier.notifications_for(stop_id),
        )

    # ------------------------------------------------------------------
    # 内部约束
    # ------------------------------------------------------------------
    def _tier_of(self, participant_id: str) -> RiskTier:
        stratification = self.stratifications.get(participant_id)
        if stratification is None:
            raise InterventionError(f"学员 {participant_id} 缺少入营评估，不能安排训练")
        return stratification.tier

    def _require_no_active_stop(self, participant_id: str) -> None:
        for stop in self.stops.values():
            if stop.participant_id == participant_id and stop.active:
                raise ActiveStopError(f"学员 {participant_id} 处于停训状态（{stop.stop_id}）")

    def _require_valid_coach(self, coach_id: str) -> None:
        credential = self.archive.latest(RecordKind.COACH_CREDENTIAL, coach_id)
        if credential is None:
            raise CoachCredentialExpired(f"教练 {coach_id} 资质缺失")
        valid_until = datetime.fromisoformat(str(credential.payload["valid_until"]).replace("Z", "+00:00"))
        if valid_until < self.clock():
            raise CoachCredentialExpired(f"教练 {coach_id} 资质已于 {valid_until.isoformat()} 过期")

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, summary: str) -> None:
        self.events.append(
            {
                "event_id": f"evt-{len(self.events) + 1:04d}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": self.clock().isoformat(),
                "version": 1,
                "summary": summary,
            }
        )
