"""Bounded polling with rate-limit-aware backoff and jitter."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Callable, Generic, TypeVar

from .errors import RateLimited

T = TypeVar("T")


class PollOutcome(StrEnum):
    NO_SEATS = "no_seats"
    TRANSIENT_ERROR = "transient_error"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class PollingPolicy:
    """Fast full-page polling with immediate rate-limit backoff.

    Page-load time naturally bounds the request rate. A one-second configurable
    floor prevents a CPU spin while allowing quick inventory checks.
    """

    minimum_seconds: float = 3.0
    maximum_seconds: float = 120.0
    multiplier: float = 1.8
    jitter_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.minimum_seconds < 1:
            raise ValueError("minimum_seconds must be at least 1 second")
        if self.maximum_seconds < self.minimum_seconds:
            raise ValueError("maximum_seconds must be >= minimum_seconds")
        if self.multiplier <= 1:
            raise ValueError("multiplier must be greater than one")
        if not 0 <= self.jitter_ratio <= 0.5:
            raise ValueError("jitter_ratio must be between 0 and 0.5")


class AdaptiveDelay:
    def __init__(self, policy: PollingPolicy, rng: random.Random | None = None) -> None:
        self.policy = policy
        self.rng = rng or random.Random()
        self._current = policy.minimum_seconds

    def after_no_seats(self) -> float:
        # A healthy response resets prior error backoff.
        self._current = self.policy.minimum_seconds
        return self._jittered(self._current)

    def after_error(self) -> float:
        self._current = min(self.policy.maximum_seconds, self._current * self.policy.multiplier)
        return self._jittered(self._current)

    def after_rate_limit(self, retry_after: float | None = None) -> float:
        requested = retry_after or self._current * self.policy.multiplier
        self._current = min(self.policy.maximum_seconds, max(self.policy.minimum_seconds, requested))
        # Never jitter below an explicit Retry-After value.
        if retry_after is not None:
            return max(retry_after, self._jittered(self._current))
        return self._jittered(self._current)

    def _jittered(self, seconds: float) -> float:
        spread = seconds * self.policy.jitter_ratio
        return max(self.policy.minimum_seconds, seconds + self.rng.uniform(-spread, spread))


class PollLoop(Generic[T]):
    """Reusable synchronous loop; Selenium remains on its owning thread."""

    def __init__(
        self,
        policy: PollingPolicy,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.delays = AdaptiveDelay(policy, rng=rng)
        self.sleep = sleep

    def run(
        self,
        check: Callable[[], T | None],
        stop: Event,
        on_wait: Callable[[float, PollOutcome], None] | None = None,
    ) -> T | None:
        while not stop.is_set():
            try:
                result = check()
            except RateLimited as exc:
                delay = self.delays.after_rate_limit(exc.retry_after)
                outcome = PollOutcome.RATE_LIMITED
            except Exception:
                delay = self.delays.after_error()
                outcome = PollOutcome.TRANSIENT_ERROR
            else:
                if result is not None:
                    return result
                delay = self.delays.after_no_seats()
                outcome = PollOutcome.NO_SEATS

            if on_wait:
                on_wait(delay, outcome)
            # Event.wait makes shutdown immediate and remains injectable in
            # higher-level tests via a fake Event/check function.
            if stop.wait(delay):
                break
        return None
