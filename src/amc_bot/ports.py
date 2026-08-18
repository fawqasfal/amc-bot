"""Dependency-light interfaces shared by orchestration and browser adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from .models import MovieRequest, Seat
from .purchase import PurchaseGuard


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(email=<redacted>, password=<redacted>)"


@dataclass(frozen=True, slots=True)
class PortalShowtime:
    movie_title: str
    starts_at: datetime
    theater_name: str
    auditorium_name: str
    format: str
    booking_url: str
    distance_miles: float | None = None

    @property
    def identity(self) -> str:
        return f"{self.theater_name} — {self.starts_at.isoformat()} — {self.format}"


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    confirmation_url: str
    reference: str | None = None


class AmcPortal(Protocol):
    def login(self, credentials: Credentials) -> None: ...

    def discover_showtimes(self, movie: MovieRequest) -> list[PortalShowtime]: ...

    def read_seat_map(
        self, showtime: PortalShowtime, ticket_count: int, *, strict_screen_rows: bool
    ) -> tuple[Seat, ...]: ...

    def select_seats(self, seats: Sequence[Seat], guard: PurchaseGuard) -> None: ...

    def prepare_checkout(
        self,
        showtime: PortalShowtime,
        seats: Sequence[Seat],
        ticket_count: int,
        payment_cvv: str | None,
        guard: PurchaseGuard,
    ) -> None: ...

    def human_action_pending(self) -> bool: ...

    def submit_order(self, guard: PurchaseGuard) -> PurchaseResult: ...

    def leave_open(self) -> None: ...
