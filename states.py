"""Redis-backed restore job and recovery-plan states for PBS → Proxmox VE."""

from __future__ import annotations

from enum import Enum


class RestoreState(str, Enum):
    PENDING = "PENDING"
    RESTORING = "RESTORING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PlanVerification(str, Enum):
    """enterprise-style readiness verification for a recovery plan."""

    NOT_VERIFIED = "NOT_VERIFIED"
    NEEDS_VERIFIED = "NEEDS_VERIFIED"
    VERIFIED = "VERIFIED"


class PlanAssurance(str, Enum):
    """Recoverability proof from assurance drills (separate from readiness verification)."""

    UNKNOWN = "UNKNOWN"
    IN_PROGRESS = "IN_PROGRESS"
    ASSURED = "ASSURED"
    FAILED = "FAILED"


class PlanRunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
