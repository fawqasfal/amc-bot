import random

import pytest

from amc_bot.polling import AdaptiveDelay, PollingPolicy


def test_polling_policy_forbids_tight_loop():
    with pytest.raises(ValueError, match="at least 1"):
        PollingPolicy(minimum_seconds=0.5)
    assert PollingPolicy(minimum_seconds=1).minimum_seconds == 1


def test_backoff_respects_retry_after_and_resets_after_healthy_response():
    delays = AdaptiveDelay(
        PollingPolicy(minimum_seconds=10, maximum_seconds=120, jitter_ratio=0),
        rng=random.Random(1),
    )
    assert delays.after_error() == 18
    assert delays.after_rate_limit(60) == 60
    assert delays.after_no_seats() == 10
