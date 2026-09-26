"""应急通知：呼叫失败不阻塞本地停训，恢复后只补送未确认通知。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class NotificationStatus(str, Enum):
    SENT = "sent"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"


@dataclass
class Notification:
    notification_id: str
    incident_id: str
    channel: str
    message: str
    status: NotificationStatus
    attempts: int = 0


#: 外呼通道，失败时抛异常；通知器负责兜底，绝不上抛。
Transport = Callable[[str, str], None]


class EmergencyNotifier:
    """应急外呼的可靠包装。"""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._notifications: dict[str, Notification] = {}
        self._sequence = 0

    def alert(self, incident_id: str, channel: str, message: str) -> Notification:
        """尽力呼叫；任何失败只记录状态，不影响本地停训流程。"""
        self._sequence += 1
        notification = Notification(
            notification_id=f"ntf-{self._sequence:04d}",
            incident_id=incident_id,
            channel=channel,
            message=message,
            status=NotificationStatus.FAILED,
        )
        self._notifications[notification.notification_id] = notification
        self._deliver(notification)
        return notification

    def acknowledge(self, notification_id: str) -> None:
        self._notifications[notification_id].status = NotificationStatus.ACKNOWLEDGED

    def resend_unacknowledged(self) -> list[Notification]:
        """服务恢复后只补送尚未确认的通知。"""
        resent: list[Notification] = []
        for notification in self._notifications.values():
            if notification.status is NotificationStatus.ACKNOWLEDGED:
                continue
            self._deliver(notification)
            resent.append(notification)
        return resent

    def notifications_for(self, incident_id: str) -> list[Notification]:
        return [n for n in self._notifications.values() if n.incident_id == incident_id]

    def _deliver(self, notification: Notification) -> None:
        notification.attempts += 1
        try:
            self._transport(notification.channel, notification.message)
        except Exception:
            notification.status = NotificationStatus.FAILED
        else:
            notification.status = NotificationStatus.SENT
