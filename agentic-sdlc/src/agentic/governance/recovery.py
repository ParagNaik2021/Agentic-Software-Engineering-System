"""RecoveryManager (Section 6.4): classifies an execution failure and
selects retry / fallback / rollback / escalate / safe-stop, with bounded
exponential backoff and jitter for retries.

Rollback itself (git revert to checkpoint) lives in tools/git.py; this
module decides *when* to call it, not how.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from agentic.core.models import ErrorClass, FallbackSpec, RetryPolicy

RecoveryStrategy = Literal["retry", "fallback", "rollback", "escalate", "safe_stop"]


@dataclass
class RecoveryDecision:
    strategy: RecoveryStrategy
    error_class: ErrorClass
    delay_seconds: float | None = None
    reason: str = ""


def compute_backoff(attempt: int, policy: RetryPolicy, rng: random.Random | None = None) -> float:
    """Exponential backoff with up to 10% jitter, capped at
    policy.cap_seconds. attempt is 1-indexed (the first retry)."""
    rng = rng or random.Random()
    base_delay = min(policy.base_seconds * (2 ** (attempt - 1)), policy.cap_seconds)
    jitter = rng.uniform(0, base_delay * 0.1)
    return base_delay + jitter


class RecoveryManager:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    def decide(
        self,
        error_class: ErrorClass,
        attempt: int,
        retry_policy: RetryPolicy,
        fallback_spec: FallbackSpec | None = None,
        detail: str = "",
    ) -> RecoveryDecision:
        """attempt is the attempt number that just failed (1-indexed).

        `detail` is the concrete cause, supplied by the caller (the
        Engine passes the failed node's error message). It exists so a
        SYSTEMIC safe-stop can say what actually happened instead of
        listing every condition that might have.
        """
        if error_class == ErrorClass.TRANSIENT:
            return self._decide_transient(attempt, retry_policy)
        if error_class == ErrorClass.MALFORMED_OUTPUT:
            return self._decide_malformed_output(attempt)
        if error_class == ErrorClass.QUALITY_FAILURE:
            return self._decide_quality_failure(attempt)
        if error_class == ErrorClass.POLICY_DENY:
            return RecoveryDecision(
                strategy="rollback", error_class=error_class,
                reason="policy DENY verdict; no retry, escalating to human approval with the verdict attached",
            )
        if error_class == ErrorClass.CONTRACT_BREACH:
            return self._decide_contract_breach(fallback_spec)
        if error_class == ErrorClass.SYSTEMIC:
            # Deliberately no retry: a SYSTEMIC classification means the
            # failure's retry-safety is unknown (engine._classify_exception),
            # so re-running the node could repeat a harmful side effect.
            # The reason names the actual cause rather than enumerating
            # budget/audit-chain/deadlock/replan-exhaustion, each of which
            # safe-stops from its own call site with its own reason.
            return RecoveryDecision(
                strategy="safe_stop", error_class=error_class,
                reason=f"systemic failure: {detail}" if detail else "systemic failure: unclassified error",
            )
        raise ValueError(f"unknown error class: {error_class}")

    def _decide_transient(self, attempt: int, retry_policy: RetryPolicy) -> RecoveryDecision:
        if attempt < retry_policy.max_attempts:
            delay = compute_backoff(attempt, retry_policy, self.rng)
            return RecoveryDecision(
                strategy="retry", error_class=ErrorClass.TRANSIENT, delay_seconds=delay,
                reason=f"transient error, retrying (attempt {attempt + 1}/{retry_policy.max_attempts})",
            )
        return RecoveryDecision(
            strategy="rollback", error_class=ErrorClass.TRANSIENT,
            reason=f"transient retries exhausted after {retry_policy.max_attempts} attempts",
        )

    def _decide_malformed_output(self, attempt: int) -> RecoveryDecision:
        # Section 9.2/6.4: one bounded repair attempt, then fallback.
        if attempt < 2:
            return RecoveryDecision(
                strategy="retry", error_class=ErrorClass.MALFORMED_OUTPUT, delay_seconds=0.0,
                reason="malformed output, one repair attempt with the validation error appended",
            )
        return RecoveryDecision(
            strategy="fallback", error_class=ErrorClass.MALFORMED_OUTPUT,
            reason="repair attempt failed, falling back",
        )

    def _decide_quality_failure(self, attempt: int) -> RecoveryDecision:
        if attempt < 2:
            return RecoveryDecision(
                strategy="fallback", error_class=ErrorClass.QUALITY_FAILURE,
                reason="tests failed / coverage below floor / security findings; "
                       "re-invoking with the failure report appended",
            )
        return RecoveryDecision(
            strategy="rollback", error_class=ErrorClass.QUALITY_FAILURE,
            reason="quality failure persisted after fallback",
        )

    def _decide_contract_breach(self, fallback_spec: FallbackSpec | None) -> RecoveryDecision:
        if fallback_spec is not None:
            return RecoveryDecision(
                strategy="fallback", error_class=ErrorClass.CONTRACT_BREACH,
                reason=f"node produced none of its declared artifacts; using declared "
                       f"fallback strategy {fallback_spec.strategy.value}",
            )
        return RecoveryDecision(
            strategy="rollback", error_class=ErrorClass.CONTRACT_BREACH,
            reason="node produced none of its declared artifacts; no fallback declared",
        )
