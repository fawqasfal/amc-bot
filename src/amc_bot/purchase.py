"""The two-key guard around the irreversible final checkout click."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PurchaseDisabled


ACKNOWLEDGEMENT = "PURCHASE"


@dataclass(frozen=True, slots=True)
class PurchaseGuard:
    runtime_enabled: bool
    request_live_purchase: bool
    acknowledgement: str = ""

    @property
    def permitted(self) -> bool:
        return (
            self.runtime_enabled
            and self.request_live_purchase
            and self.acknowledgement == ACKNOWLEDGEMENT
        )

    def require(self) -> None:
        if not self.permitted:
            raise PurchaseDisabled(
                "Final purchase requires --live-purchases and the exact acknowledgement PURCHASE"
            )
