"""RecoveryManager: error classification -> strategy (Section 6.4)."""

import random

import pytest

from agentic.core.models import ErrorClass, FallbackSpec, FallbackStrategy, RetryPolicy
from agentic.governance.recovery import RecoveryManager, compute_backoff


@pytest.fixture
def manager() -> RecoveryManager:
    return RecoveryManager(rng=random.Random(42))


def test_transient_retries_with_bounded_backoff(manager: RecoveryManager) -> None:
    policy = RetryPolicy(max_attempts=3, base_seconds=2.0, cap_seconds=30.0)

    decision = manager.decide(ErrorClass.TRANSIENT, attempt=1, retry_policy=policy)
    assert decision.strategy == "retry"
    assert decision.delay_seconds is not None
    assert 2.0 <= decision.delay_seconds <= 2.0 * 1.1

    decision = manager.decide(ErrorClass.TRANSIENT, attempt=2, retry_policy=policy)
    assert decision.strategy == "retry"
    assert 4.0 <= decision.delay_seconds <= 4.0 * 1.1


def test_transient_exhausted_routes_to_rollback(manager: RecoveryManager) -> None:
    policy = RetryPolicy(max_attempts=3, base_seconds=2.0, cap_seconds=30.0)
    decision = manager.decide(ErrorClass.TRANSIENT, attempt=3, retry_policy=policy)
    assert decision.strategy == "rollback"


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_seconds=2.0, cap_seconds=10.0)
    delay = compute_backoff(attempt=10, policy=policy, rng=random.Random(0))
    assert delay <= 10.0 * 1.1  # cap plus max jitter


def test_malformed_output_gets_one_repair_attempt_then_falls_back(manager: RecoveryManager) -> None:
    policy = RetryPolicy()
    first = manager.decide(ErrorClass.MALFORMED_OUTPUT, attempt=1, retry_policy=policy)
    assert first.strategy == "retry"
    assert first.delay_seconds == 0.0

    second = manager.decide(ErrorClass.MALFORMED_OUTPUT, attempt=2, retry_policy=policy)
    assert second.strategy == "fallback"


def test_quality_failure_falls_back_then_rolls_back(manager: RecoveryManager) -> None:
    policy = RetryPolicy()
    first = manager.decide(ErrorClass.QUALITY_FAILURE, attempt=1, retry_policy=policy)
    assert first.strategy == "fallback"

    second = manager.decide(ErrorClass.QUALITY_FAILURE, attempt=2, retry_policy=policy)
    assert second.strategy == "rollback"


def test_policy_deny_never_retries(manager: RecoveryManager) -> None:
    decision = manager.decide(ErrorClass.POLICY_DENY, attempt=1, retry_policy=RetryPolicy())
    assert decision.strategy == "rollback"


def test_contract_breach_falls_back_when_fallback_declared(manager: RecoveryManager) -> None:
    fallback = FallbackSpec(strategy=FallbackStrategy.TEMPLATE)
    decision = manager.decide(
        ErrorClass.CONTRACT_BREACH, attempt=1, retry_policy=RetryPolicy(), fallback_spec=fallback
    )
    assert decision.strategy == "fallback"


def test_contract_breach_rolls_back_without_fallback(manager: RecoveryManager) -> None:
    decision = manager.decide(
        ErrorClass.CONTRACT_BREACH, attempt=1, retry_policy=RetryPolicy(), fallback_spec=None
    )
    assert decision.strategy == "rollback"


def test_systemic_always_safe_stops(manager: RecoveryManager) -> None:
    decision = manager.decide(ErrorClass.SYSTEMIC, attempt=1, retry_policy=RetryPolicy())
    assert decision.strategy == "safe_stop"
