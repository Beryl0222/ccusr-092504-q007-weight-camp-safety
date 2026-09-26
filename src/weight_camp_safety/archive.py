"""只增不改的版本化档案库。

学员同意、医学与运动评估、禁忌与转诊意见、教练资质、饮食训练处方、
每日执行、体征症状、应急处置及商业承诺分别按类别与标识追加版本，
历史版本永远可读，任何写入都不会覆盖既有事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .models import RecordKind, Role


class ArchiveError(Exception):
    """档案写入违反留存或权限约定。"""


#: 业绩人员只允许登记商业承诺，医疗、评估与处置事实对其关闭。
_PERFORMANCE_WRITABLE: frozenset[RecordKind] = frozenset({RecordKind.COMMERCIAL_PROMISE})

#: 一经登记即不可在后续版本中改写的字段（原始体重、不良事件关键事实）。
_IMMUTABLE_FIELDS: dict[RecordKind, frozenset[str]] = {
    RecordKind.MEDICAL_ASSESSMENT: frozenset({"participant_id", "original_weight_kg"}),
    RecordKind.EMERGENCY_RESPONSE: frozenset({"incident_id", "participant_id", "symptoms"}),
}


@dataclass(frozen=True)
class RecordVersion:
    """档案的一个不可变版本。"""

    kind: RecordKind
    record_id: str
    version: int
    payload: Mapping[str, Any]
    recorded_by: Role
    recorded_at: datetime
    note: str = ""


class VersionedArchive:
    """按类别与标识追加版本的档案库。"""

    def __init__(self) -> None:
        self._records: dict[tuple[RecordKind, str], list[RecordVersion]] = {}

    def append(
        self,
        kind: RecordKind,
        record_id: str,
        payload: Mapping[str, Any],
        recorded_by: Role,
        recorded_at: datetime,
        note: str = "",
    ) -> RecordVersion:
        """追加一个新版本；越权或改写不可变字段时拒绝。"""
        if recorded_by is Role.PERFORMANCE_STAFF and kind not in _PERFORMANCE_WRITABLE:
            raise ArchiveError("业绩人员不能修改医疗、评估、处置等事实档案")
        key = (kind, record_id)
        history = self._records.setdefault(key, [])
        if history:
            first = history[0].payload
            for field_name in _IMMUTABLE_FIELDS.get(kind, frozenset()):
                if field_name in first and payload.get(field_name) != first[field_name]:
                    raise ArchiveError(f"字段 {field_name} 一经登记不可改写")
        version = RecordVersion(
            kind=kind,
            record_id=record_id,
            version=len(history) + 1,
            payload=dict(payload),
            recorded_by=recorded_by,
            recorded_at=recorded_at,
            note=note,
        )
        history.append(version)
        return version

    def history(self, kind: RecordKind, record_id: str) -> list[RecordVersion]:
        """按登记顺序返回全部版本。"""
        return list(self._records.get((kind, record_id), []))

    def latest(self, kind: RecordKind, record_id: str) -> RecordVersion | None:
        history = self._records.get((kind, record_id))
        return history[-1] if history else None
