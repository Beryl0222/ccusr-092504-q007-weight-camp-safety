"""追加型事件日志、版本序列与离线补传幂等。

核心约束：
- 事件只能追加，每个聚合的 version 严格 +1，不允许覆盖或回填旧版本；
- 门店离线补传/穿戴设备上报以 ``(source_record_id, content_hash)`` 为幂等键：
  同标识同内容只保留一次（INGESTION_DEDUPED），同标识异内容不改写原记录，
  而是开数据复核单（DATA_REVIEW_OPENED），由复核结论（DATA_REVIEW_RESOLVED）收尾；
- 补传事件的 ``occurred_at`` 可以早于已入库事件（离线迟到），因此只校验
  版本序列，不强制事件时间单调。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .contracts import IMMUTABLE_FACTS, validate_event


class IngestOutcome(str, Enum):
    ADMITTED = "ADMITTED"
    DEDUPED = "DEDUPED"
    REVIEW_OPENED = "REVIEW_OPENED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    event_id: str
    detail: str
    issues: tuple[Mapping[str, str], ...] = ()

    @property
    def admitted(self) -> bool:
        return self.outcome is IngestOutcome.ADMITTED


def canonical_hash(payload: Mapping[str, Any]) -> str:
    """对业务载荷做规范化 SHA-256；信封字段（event_id/version/occurred_at）
    不参与哈希，保证同一条离线记录重传时内容判定稳定。"""
    import hashlib

    body = payload.get("data", payload)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventLog:
    """内存实现的追加日志；持久化实现只需复用同样的序列与幂等语义。"""

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._by_event_id: dict[str, dict[str, Any]] = {}
        self._seq: list[dict[str, Any]] = []
        # source_record_id -> (content_hash, event_id)
        self._sources: dict[str, tuple[str, str]] = {}

    # ----- 基础追加 -------------------------------------------------------

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        history = self._streams.get((aggregate_type, aggregate_id))
        return len(history) + 1 if history else 1

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        issues = validate_event(event)
        if issues:
            raise ValueError(f"事件未通过交换层校验: {issues}")

        key = (event["aggregate_type"], event["aggregate_id"])
        history = self._streams.setdefault(key, [])
        expected = len(history) + 1
        if event["version"] != expected:
            raise VersionConflict(key[0], key[1], expected, event["version"])
        if event["event_id"] in self._by_event_id:
            raise ValueError(f"event_id 重复: {event['event_id']}")

        stored = dict(event)
        history.append(stored)
        self._by_event_id[stored["event_id"]] = stored
        self._seq.append(stored)
        return stored

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._by_event_id.get(event_id)

    def stream(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return list(self._streams.get((aggregate_type, aggregate_id), ()))

    def all_events(self) -> list[dict[str, Any]]:
        """按追加（入库）顺序返回；离线迟到记录排在补传时刻，不改变既有事实。"""
        return list(self._seq)

    def immutable_aggregates(self) -> frozenset[str]:
        return IMMUTABLE_FACTS


class VersionConflict(RuntimeError):
    def __init__(self, aggregate_type: str, aggregate_id: str, expected: int, got: int) -> None:
        super().__init__(
            f"聚合 {aggregate_type}/{aggregate_id} 版本序列断裂：期望 {expected}，收到 {got}"
        )
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.got = got


class IngestionGateway:
    """离线补传/穿戴数据入口：幂等去重与同标识异内容复核。"""

    def __init__(self, log: EventLog) -> None:
        self.log = log

    def admit(self, event: Mapping[str, Any]) -> IngestResult:
        # 离线/穿戴端不持有聚合版本序列，入口统一代为分配后再校验。
        incoming = dict(event)
        incoming["version"] = self.log.next_version(event["aggregate_type"], event["aggregate_id"])
        issues = validate_event(incoming)
        if issues:
            return IngestResult(
                IngestOutcome.REJECTED,
                str(event.get("event_id", "")),
                "交换层校验失败",
                tuple({"field": i.field, "code": i.code, "message": i.message} for i in issues),
            )

        event = incoming
        source_id = event.get("source_record_id")
        content_hash = event.get("content_hash")
        if not source_id:
            stored = self.log.append(event)
            return IngestResult(IngestOutcome.ADMITTED, stored["event_id"], "无来源标识，直接入库")

        existing = self.log._sources.get(source_id)
        if existing is not None:
            first_hash, first_event_id = existing
            if first_hash == content_hash:
                dedup = self._append_bookkeeping(
                    "INGESTION_DEDUPED",
                    source_id,
                    {
                        "source_record_id": source_id,
                        "content_hash": content_hash,
                        "summary": f"重复记录已丢弃，仅保留首次入库 {first_event_id}",
                        "basis_refs": [first_event_id],
                        "actor_role": "system",
                        "participant_id": event.get("participant_id"),
                        "site_id": event.get("site_id"),
                    },
                )
                return IngestResult(
                    IngestOutcome.DEDUPED,
                    dedup["event_id"],
                    f"与 {first_event_id} 内容一致，保持一次记录",
                )
            review = self._append_bookkeeping(
                "DATA_REVIEW_OPENED",
                source_id,
                {
                    "source_record_id": source_id,
                    "content_hash": content_hash,
                    "summary": f"同标识 {source_id} 内容与首次入库 {first_event_id} 不一致，进入复核",
                    "basis_refs": [first_event_id],
                    "actor_role": "system",
                    "conflicting_payload": dict(event),
                    "participant_id": event.get("participant_id"),
                    "site_id": event.get("site_id"),
                },
            )
            return IngestResult(
                IngestOutcome.REVIEW_OPENED,
                review["event_id"],
                "同标识异内容：原记录保留，冲突载荷未入库",
            )

        stored = self.log.append(event)
        self.log._sources[source_id] = (str(content_hash), stored["event_id"])
        return IngestResult(IngestOutcome.ADMITTED, stored["event_id"], "首次到达，已入库")

    def resolve_review(
        self,
        source_record_id: str,
        *,
        event_id: str,
        resolution: str,
        actor_id: str,
        actor_role: str = "medical_reviewer",
    ) -> IngestResult:
        existing = self.log._sources.get(source_record_id)
        if existing is None:
            return IngestResult(IngestOutcome.REJECTED, event_id, "来源标识不存在，无需复核")
        first_event = self.log.get(existing[1]) or {}
        stored = self._append_bookkeeping(
            "DATA_REVIEW_RESOLVED",
            source_record_id,
            {
                "source_record_id": source_record_id,
                "content_hash": existing[0],
                "summary": resolution,
                "basis_refs": [existing[1]],
                "actor_id": actor_id,
                "actor_role": actor_role,
                "event_id_hint": event_id,
                "participant_id": first_event.get("participant_id"),
                "site_id": first_event.get("site_id"),
            },
        )
        return IngestResult(IngestOutcome.ADMITTED, stored["event_id"], "复核结论已留存")

    # ----- 内部 -----------------------------------------------------------

    def _append_bookkeeping(self, event_type: str, source_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        hint = fields.get("event_id_hint")
        aggregate_id = f"ingest:{source_id}"
        payload: dict[str, Any] = {
            "event_id": hint or f"{event_type.lower()}:{source_id}:{self.log.next_version('ingestion_record', aggregate_id)}",
            "event_type": event_type,
            "aggregate_type": "ingestion_record",
            "aggregate_id": aggregate_id,
            "occurred_at": fields.get("occurred_at") or _now_iso(),
            "version": self.log.next_version("ingestion_record", aggregate_id),
            "summary": fields["summary"],
        }
        for passthrough in ("source_record_id", "content_hash", "basis_refs", "actor_id", "actor_role", "participant_id", "site_id", "conflicting_payload"):
            if fields.get(passthrough) is not None:
                payload[passthrough] = fields[passthrough]
        return self.log.append(payload)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
