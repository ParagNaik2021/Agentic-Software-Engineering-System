"""P1: exhaustive legal/illegal transition matrix (Section 4.3)."""

import pytest

from agentic.core.states import TRANSITIONS, IllegalTransition, NodeStatus, assert_transition


@pytest.mark.parametrize(
    "frm,to",
    [(frm, to) for frm, tos in TRANSITIONS.items() for to in tos],
)
def test_every_declared_transition_is_legal(frm: NodeStatus, to: NodeStatus) -> None:
    assert_transition(frm, to)  # must not raise


@pytest.mark.parametrize(
    "frm,to",
    [
        (frm, to)
        for frm in NodeStatus
        for to in NodeStatus
        if to not in TRANSITIONS.get(frm, frozenset())
    ],
)
def test_every_undeclared_transition_is_illegal(frm: NodeStatus, to: NodeStatus) -> None:
    with pytest.raises(IllegalTransition):
        assert_transition(frm, to)


def test_halted_is_terminal() -> None:
    assert TRANSITIONS[NodeStatus.HALTED] == frozenset()
    for status in NodeStatus:
        with pytest.raises(IllegalTransition):
            assert_transition(NodeStatus.HALTED, status)


def test_illegal_transition_carries_from_and_to() -> None:
    try:
        assert_transition(NodeStatus.SUCCEEDED, NodeStatus.RUNNING)
    except IllegalTransition as exc:
        assert exc.frm == NodeStatus.SUCCEEDED
        assert exc.to == NodeStatus.RUNNING
    else:
        pytest.fail("expected IllegalTransition")
