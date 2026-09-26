"""离线补传与穿戴数据的幂等接入。

同标识同内容只保留一次记录；同标识异内容不直接落库，
进入复核队列由人工核对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class IngestStatus(str, Enum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ConflictReview:
    """同标识异内容的待复核条目。"""

    source: str
    external_id: str
    existing_fingerprint: str
    incoming_fingerprint: str
    incoming_content: Mapping[str, Any]


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    source: str
    external_id: str
    fingerprint: str


def _fingerprint(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IngestGateway:
    """按（来源，外部标识）去重的接入口。"""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], str] = {}
        self.conflicts: list[ConflictReview] = []

    def receive(self, source: str, external_id: str, content: Mapping[str, Any]) -> IngestResult:
        key = (source, external_id)
        fingerprint = _fingerprint(content)
        known = self._seen.get(key)
        if known is None:
            self._seen[key] = fingerprint
            return IngestResult(IngestStatus.STORED, source, external_id, fingerprint)
        if known == fingerprint:
            return IngestResult(IngestStatus.DUPLICATE, source, external_id, fingerprint)
        self.conflicts.append(
            ConflictReview(source, external_id, known, fingerprint, dict(content))
        )
        return IngestResult(IngestStatus.CONFLICT, source, external_id, fingerprint)
