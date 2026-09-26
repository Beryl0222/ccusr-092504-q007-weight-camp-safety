"""应急处置与通知外发箱（outbox）。

关键安全语义：
- 红旗信号触发的本地停训/应急处置**先落本地事件**，不等待外部服务可用；
  应急呼叫（120/值班医生/总部）失败只记录 NOTIFICATION_FAILED，不阻塞停训；
- 外发箱对每条通知保证 at-least-once 发送，按通知标识去重，
  恢复服务后只补送仍处于 QUEUED/FAILED 且未收到送达确认的通知；
- NOTIFICATION_DELIVERED 必须带送达凭证（对端回执标识），防止误标已达。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol

from .eventlog import EventLog


class Transmitter(Protocol):
    def send(self, payload: Mapping[str, object]) -> str:
        """发送成功返回收执标识；失败抛出异常。"""
        ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EmergencyOutbox:
    log: EventLog
    transmitter: Transmitter | None = None
    # notification_id -> 待发通知载荷（含目标、内容、依据事件）
    _pending: dict[str, dict[str, object]] = field(default_factory=dict)
    _delivered: set[str] = field(default_factory=set)
    # 呼叫失败计数，仅用于"失败不阻塞"的可观测性，事实仍以事件日志为准
    failure_log: list[Mapping[str, object]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 本地侧：停训与应急处置永远先成立
    # ------------------------------------------------------------------

    def stop_training_locally(
        self,
        *,
        event_id: str,
        participant_id: str,
        site_id: str,
        actor_id: str,
        occurred_at: str,
        reasons: list[Mapping[str, object]],
        summary: str,
        incident_id: str,
        rule_book_version: str | None = None,
    ) -> dict[str, object]:
        """红旗信号触发的即时停训。纯本地落库，不尝试任何网络动作。

        reasons 为规则引擎触发项快照（rule_id/detail/basis），原样写入
        data，供学员核对停训依据与监管追溯。
        """
        basis: list[str] = []
        for r in reasons:
            ref = str(r.get("basis")) if r.get("basis") else ""
            if ref and ref not in basis:
                basis.append(ref)
        event = {
            "event_id": event_id,
            "event_type": "TRAINING_STOPPED",
            "aggregate_type": "participant",
            "aggregate_id": participant_id,
            "participant_id": participant_id,
            "site_id": site_id,
            "actor_id": actor_id,
            "actor_role": "coach",
            "occurred_at": occurred_at,
            "version": self.log.next_version("participant", participant_id),
            "summary": summary,
            "basis_refs": basis,
            "data": {
                "scope": "IMMEDIATE",
                "resume_policy": "REQUIRES_NEW_PROFESSIONAL_REVIEW",
                "reasons": list(reasons),
                "incident_id": incident_id,
                "rule_book_version": rule_book_version,
            },
        }
        return self.log.append(event)

    def record_emergency_response(
        self,
        *,
        event_id: str,
        incident_id: str,
        participant_id: str,
        site_id: str,
        actor_id: str,
        occurred_at: str,
        actions: list[str],
        summary: str,
        basis_refs: list[str],
    ) -> dict[str, object]:
        """现场应急处置（平卧、补糖、吸氧、呼叫120等）独立留痕。"""
        event = {
            "event_id": event_id,
            "event_type": "EMERGENCY_RESPONDED",
            "aggregate_type": "safety_incident",
            "aggregate_id": incident_id,
            "participant_id": participant_id,
            "site_id": site_id,
            "actor_id": actor_id,
            "actor_role": "coach",
            "occurred_at": occurred_at,
            "version": self.log.next_version("safety_incident", incident_id),
            "summary": summary,
            "basis_refs": basis_refs,
            "data": {"actions": list(actions)},
        }
        return self.log.append(event)

    # ------------------------------------------------------------------
    # 外发箱：排队、尝试、确认、重连补送
    # ------------------------------------------------------------------

    def queue_notification(
        self,
        *,
        notification_id: str,
        channel: str,
        target: str,
        subject: str,
        body: Mapping[str, object],
        basis_refs: list[str],
        participant_id: str,
        site_id: str,
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """通知进入外发箱；重复入队（同标识）直接忽略，保持一次通知。"""
        if notification_id in self._pending or notification_id in self._delivered:
            existing = self.log.get(f"notif-queued:{notification_id}")
            return existing or {}

        event = {
            "event_id": f"notif-queued:{notification_id}",
            "event_type": "NOTIFICATION_QUEUED",
            "aggregate_type": "emergency_notification",
            "aggregate_id": notification_id,
            "participant_id": participant_id,
            "site_id": site_id,
            "actor_role": "system",
            "occurred_at": occurred_at or _now_iso(),
            "version": self.log.next_version("emergency_notification", notification_id),
            "summary": f"应急通知已排队：{subject}",
            "basis_refs": basis_refs,
            "data": {"channel": channel, "target": target, "subject": subject, "body": dict(body)},
        }
        stored = self.log.append(event)
        self._pending[notification_id] = stored
        return stored

    def attempt_pending(self, transmitter: Transmitter | None = None) -> list[Mapping[str, object]]:
        """尝试发送全部未确认通知；任何一条失败都不影响其余通知与本地处置。

        返回本次尝试的结果快照列表。已送达的通知永不再发。
        """
        sender = transmitter or self.transmitter
        results: list[Mapping[str, object]] = []
        for notification_id in list(self._pending):
            queued = self._pending[notification_id]
            try:
                if sender is None:
                    raise RuntimeError("未配置发送通道")
                receipt = sender.send(queued.get("data", {}))
                if not isinstance(receipt, str) or not receipt.strip():
                    raise RuntimeError("发送通道未返回收执标识")
            except Exception as exc:  # 通道失败：记录，保留在外发箱，稍后补送
                self._record_failure(notification_id, queued, str(exc))
                self.failure_log.append({"notification_id": notification_id, "reason": str(exc)})
                results.append({"notification_id": notification_id, "status": "FAILED", "reason": str(exc)})
                continue

            self._mark_delivered(notification_id, queued, receipt)
            results.append({"notification_id": notification_id, "status": "DELIVERED", "receipt": receipt})
        return results

    def flush_on_reconnect(self, transmitter: Transmitter) -> list[Mapping[str, object]]:
        """恢复服务后调用：只补送仍未确认（QUEUED/FAILED 无 DELIVERED）的通知。"""
        # _pending 中天然只剩未确认项；已确认项在送达时已移除。
        return self.attempt_pending(transmitter)

    @property
    def unconfirmed(self) -> list[str]:
        return list(self._pending)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _record_failure(self, notification_id: str, queued: Mapping[str, object], reason: str) -> None:
        version = self.log.next_version("emergency_notification", notification_id)
        event = {
            "event_id": f"notif-failed:{notification_id}:{version}",
            "event_type": "NOTIFICATION_FAILED",
            "aggregate_type": "emergency_notification",
            "aggregate_id": notification_id,
            "actor_role": "system",
            "occurred_at": _now_iso(),
            "version": version,
            "summary": f"通知发送失败（不阻塞本地停训）：{reason}",
            "basis_refs": [str(queued["event_id"])],
            "data": {"reason": reason},
        }
        for optional in ("participant_id", "site_id"):
            if queued.get(optional):
                event[optional] = queued[optional]
        self.log.append(event)

    def _mark_delivered(self, notification_id: str, queued: Mapping[str, object], receipt: str) -> None:
        version = self.log.next_version("emergency_notification", notification_id)
        event = {
            "event_id": f"notif-delivered:{notification_id}:{version}",
            "event_type": "NOTIFICATION_DELIVERED",
            "aggregate_type": "emergency_notification",
            "aggregate_id": notification_id,
            "actor_role": "system",
            "occurred_at": _now_iso(),
            "version": version,
            "summary": f"通知已送达，回执 {receipt}",
            "basis_refs": [str(queued["event_id"])],
            "data": {"receipt": receipt},
        }
        for optional in ("participant_id", "site_id"):
            if queued.get(optional):
                event[optional] = queued[optional]
        self.log.append(event)
        self._pending.pop(notification_id, None)
        self._delivered.add(notification_id)
