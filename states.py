"""Redis-backed restore job states for PBS → Proxmox VE."""

from __future__ import annotations

from enum import Enum


class RestoreState(str, Enum):
    PENDING = "PENDING"
    RESTORING = "RESTORING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
