"""减重训练安全干预台领域契约与规则库。"""

from .contracts import (
    ACTOR_ROLES,
    AGGREGATE_TYPES,
    EVENT_TYPES,
    IMMUTABLE_FACTS,
    ContractIssue,
    validate_event,
)
from .emergency import EmergencyOutbox
from .eventlog import (
    EventLog,
    IngestOutcome,
    IngestResult,
    IngestionGateway,
    VersionConflict,
    canonical_hash,
)
from .rules import (
    Action,
    ApprovedRuleSet,
    RiskTier,
    StopDecision,
    approved_rule_set,
    check_action,
    evaluate_stop_signals,
    stratify_risk,
)
from .trace import build_incident_trace, build_participant_view, check_basis_closure

__all__ = [
    "ACTOR_ROLES",
    "AGGREGATE_TYPES",
    "EVENT_TYPES",
    "IMMUTABLE_FACTS",
    "Action",
    "ApprovedRuleSet",
    "ContractIssue",
    "EmergencyOutbox",
    "EventLog",
    "IngestOutcome",
    "IngestResult",
    "IngestionGateway",
    "RiskTier",
    "StopDecision",
    "VersionConflict",
    "approved_rule_set",
    "build_incident_trace",
    "build_participant_view",
    "canonical_hash",
    "check_action",
    "check_basis_closure",
    "evaluate_stop_signals",
    "stratify_risk",
    "validate_event",
]
