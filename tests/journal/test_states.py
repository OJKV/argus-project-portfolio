from argus.journal.states import (
    TERMINAL,
    TRANSITIONS,
    OrderState,
    PlanState,
    PositionState,
    ProtectionState,
    can_transition,
    reaches_terminal,
)


def test_every_state_reaches_terminal() -> None:
    for enum in (PlanState, OrderState, PositionState, ProtectionState):
        for state in enum:
            assert reaches_terminal(enum, state.value), (
                f"{enum.__name__}.{state} cannot reach a terminal state"
            )


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for enum, terminals in TERMINAL.items():
        for t in terminals:
            assert t not in TRANSITIONS[enum]


def test_no_widening_from_trailing() -> None:
    assert not can_transition(ProtectionState, "TRAILING", "SL_ATTACHED")


def test_legal_transitions_only() -> None:
    assert can_transition(PlanState, "DRAFT", "GATED")
    assert can_transition(PositionState, "ORPHAN", "OPEN")  # adopted, then protected
    assert can_transition(ProtectionState, "SL_ATTACHED", "TRAILING")
    # A plan cannot skip the gate, and a closed position cannot reopen.
    assert not can_transition(PlanState, "DRAFT", "SUBMITTED")
    assert not can_transition(PositionState, "CLOSED", "OPEN")
    assert not can_transition(ProtectionState, "TRAILING", "NONE")


def test_a_position_can_never_sit_unprotected_forever() -> None:
    # ProtectionState.NONE is legal for less than one run; it must be able to leave.
    assert TRANSITIONS[ProtectionState]["NONE"]
    assert reaches_terminal(ProtectionState, "NONE")
