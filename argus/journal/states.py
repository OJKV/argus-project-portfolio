"""State machines with terminal states (ARCHITECTURE.md §8; FAILURE_LOG F-025)."""

from __future__ import annotations

from enum import StrEnum


class PlanState(StrEnum):
    DRAFT = "DRAFT"
    GATED = "GATED"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderState(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class PositionState(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    REDUCED = "REDUCED"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"
    ORPHAN = "ORPHAN"


class ProtectionState(StrEnum):
    NONE = "NONE"
    SL_ATTACHED = "SL_ATTACHED"
    TP1_HIT_BE = "TP1_HIT_BE"
    TRAILING = "TRAILING"
    EXITED = "EXITED"


TERMINAL: dict[type[StrEnum], frozenset[str]] = {
    PlanState: frozenset({"FILLED", "EXPIRED", "REJECTED", "CANCELLED"}),
    OrderState: frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}),
    PositionState: frozenset({"CLOSED"}),
    ProtectionState: frozenset({"EXITED"}),
}

TRANSITIONS: dict[type[StrEnum], dict[str, frozenset[str]]] = {
    PlanState: {
        "DRAFT": frozenset({"GATED", "REJECTED"}),
        "GATED": frozenset({"QUEUED", "SUBMITTED", "REJECTED"}),
        "QUEUED": frozenset({"SUBMITTED", "EXPIRED", "REJECTED"}),
        "SUBMITTED": frozenset({"FILLED", "EXPIRED", "CANCELLED"}),
    },
    OrderState: {
        "NEW": frozenset({"OPEN", "REJECTED"}),
        "OPEN": frozenset({"PARTIAL", "FILLED", "CANCELLED", "EXPIRED"}),
        "PARTIAL": frozenset({"FILLED", "CANCELLED", "EXPIRED"}),
    },
    PositionState: {
        "PENDING_ENTRY": frozenset({"OPEN", "CLOSED"}),
        "OPEN": frozenset({"REDUCED", "PENDING_EXIT", "CLOSED"}),
        "REDUCED": frozenset({"PENDING_EXIT", "CLOSED"}),
        "PENDING_EXIT": frozenset({"CLOSED", "OPEN"}),
        "ORPHAN": frozenset({"OPEN", "CLOSED"}),
    },
    ProtectionState: {
        "NONE": frozenset({"SL_ATTACHED", "EXITED"}),
        "SL_ATTACHED": frozenset({"TP1_HIT_BE", "TRAILING", "EXITED"}),
        "TP1_HIT_BE": frozenset({"TRAILING", "EXITED"}),
        "TRAILING": frozenset({"EXITED"}),
    },
}


def can_transition(enum: type[StrEnum], src: str, dst: str) -> bool:
    return dst in TRANSITIONS[enum].get(src, frozenset())


def reaches_terminal(enum: type[StrEnum], src: str) -> bool:
    """Every state must have a path to a terminal state."""
    seen: set[str] = set()
    stack = [src]
    while stack:
        s = stack.pop()
        if s in TERMINAL[enum]:
            return True
        if s in seen:
            continue
        seen.add(s)
        stack.extend(TRANSITIONS[enum].get(s, frozenset()))
    return False
