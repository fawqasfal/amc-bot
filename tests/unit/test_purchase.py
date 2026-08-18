import pytest

from amc_bot.errors import PurchaseDisabled
from amc_bot.purchase import PurchaseGuard


@pytest.mark.parametrize(
    "runtime,requested,ack",
    [
        (False, True, "PURCHASE"),
        (True, False, "PURCHASE"),
        (True, True, "purchase"),
        (True, True, ""),
    ],
)
def test_purchase_guard_needs_both_keys_and_exact_phrase(runtime, requested, ack):
    guard = PurchaseGuard(runtime, requested, ack)
    assert guard.permitted is False
    with pytest.raises(PurchaseDisabled):
        guard.require()


def test_purchase_guard_allows_the_explicit_live_combination():
    PurchaseGuard(True, True, "PURCHASE").require()
