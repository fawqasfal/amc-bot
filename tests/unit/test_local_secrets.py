import stat

import pytest

from amc_bot.local_secrets import LocalCvvStore


def test_local_cvv_store_round_trip_and_private_permissions(tmp_path):
    path = tmp_path / "data" / "payment_cvv"
    store = LocalCvvStore(path)

    assert store.load() is None
    store.save("1234")

    assert store.load() == "1234"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    store.delete()
    assert store.load() is None


@pytest.mark.parametrize("value", ["", "12", "12345", "1x3"])
def test_local_cvv_store_rejects_invalid_values(tmp_path, value):
    store = LocalCvvStore(tmp_path / "payment_cvv")
    with pytest.raises(ValueError):
        store.save(value)
