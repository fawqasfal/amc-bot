from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from amc_bot.coordinator import WatcherCoordinator, availability_from_hour_tokens
from amc_bot.errors import HumanActionRequired, PurchaseDisabled, PurchaseOutcomeUnknown
from amc_bot.models import Seat
from amc_bot.ports import PortalShowtime, PurchaseResult
from amc_bot.state import Phase


class FakePortal:
    def __init__(self, *, human_gate_once: bool = False, ambiguous_once: bool = False):
        self.human_gate_once = human_gate_once
        self.ambiguous_once = ambiguous_once
        self.human_pending_checks = 0
        self.login_calls = 0
        self.selected = []
        self.prepared = 0
        self.submitted = 0
        self.left_open = 0
        self.closed = 0
        self.received_cvv = None
        starts_at = datetime.now(ZoneInfo("America/New_York")).replace(minute=0, second=0, microsecond=0)
        self.showtime = PortalShowtime(
            "Dune: Part Two",
            starts_at,
            "AMC Lincoln Square 13",
            "IMAX",
            "70MM IMAX",
            "http://fixture.invalid/showtime/1",
            2.0,
        )

    def login(self, credentials):
        self.login_calls += 1
        if self.human_gate_once and self.login_calls == 1:
            self.human_pending_checks = 2
            raise HumanActionRequired("Complete the fixture challenge")

    def human_action_pending(self):
        if self.human_pending_checks:
            self.human_pending_checks -= 1
            return True
        return False

    def discover_showtimes(self, movie):
        return [self.showtime]

    def read_seat_map(self, showtime, ticket_count, *, strict_screen_rows):
        return tuple(
            Seat("D", number, row_rank=3, seat_id=f"D-{number}", x=number * 10, y=100)
            for number in range(1, 11)
        )

    def select_seats(self, seats, guard):
        guard.require()
        self.selected = list(seats)

    def prepare_checkout(self, showtime, seats, ticket_count, payment_cvv, guard):
        guard.require()
        self.prepared += 1
        self.received_cvv = payment_cvv

    def submit_order(self, guard):
        guard.require()
        self.submitted += 1
        if self.ambiguous_once and self.submitted == 1:
            raise PurchaseOutcomeUnknown("fixture confirmation timeout")
        return PurchaseResult("http://fixture.invalid/confirmation", "fixture-123")

    def leave_open(self):
        self.left_open += 1

    def close(self):
        self.closed += 1


def payload_for(portal: FakePortal, *, dry_run: bool = True):
    starts = portal.showtime.starts_at
    return {
        "email": "person@example.com",
        "password": "do-not-leak",
        "payment_cvv": "123",
        "primary_movie": "Dune: Part Two",
        "aliases": ["Dune 2"],
        "location": "10023",
        "radius_miles": 10,
        "ticket_count": 2,
        "availability": [f"{starts.weekday()}:{starts.hour}"],
        "lincoln_square_70mm_imax_only": True,
        "dry_run": dry_run,
        "live_purchase_acknowledgement": "PURCHASE" if not dry_run else "",
    }


def wait_for_phase(coordinator: WatcherCoordinator, phase: Phase, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if coordinator.status()["phase"] == phase.value:
            return
        time.sleep(0.01)
    raise AssertionError(f"watcher did not reach {phase}; status={coordinator.status()}")


def test_dry_run_reads_and_ranks_but_never_clicks_seats_or_checkout():
    portal = FakePortal()
    coordinator = WatcherCoordinator(lambda: portal, max_attempts=1)
    coordinator.start(payload_for(portal))
    coordinator.join(3)

    assert coordinator.status()["phase"] == Phase.DRY_RUN_COMPLETE.value
    assert coordinator.status()["selected_seats"] == ["D5", "D6"]
    assert portal.selected == []
    assert portal.prepared == 0
    assert portal.submitted == 0
    assert portal.left_open == 0
    assert portal.closed == 1
    assert "do-not-leak" not in repr(coordinator.status())
    assert "123" not in repr(coordinator.status())


def test_direct_live_request_is_rejected_when_runtime_flag_is_off():
    portal = FakePortal()
    coordinator = WatcherCoordinator(lambda: portal, live_purchases_allowed=False)
    with pytest.raises(PurchaseDisabled):
        coordinator.start(payload_for(portal, dry_run=False))
    assert portal.login_calls == 0


def test_fake_live_path_submits_once_and_stops():
    portal = FakePortal()
    coordinator = WatcherCoordinator(lambda: portal, live_purchases_allowed=True)
    coordinator.start(payload_for(portal, dry_run=False))
    coordinator.join(3)

    assert coordinator.status()["phase"] == Phase.PURCHASED.value
    assert len(portal.selected) == 2
    assert portal.prepared == 1
    assert portal.submitted == 1
    assert portal.received_cvv == "123"


def test_human_gate_continues_automatically_when_challenge_clears():
    portal = FakePortal(human_gate_once=True)
    coordinator = WatcherCoordinator(lambda: portal)
    coordinator.start(payload_for(portal))
    wait_for_phase(coordinator, Phase.NEEDS_HUMAN)
    assert portal.login_calls == 1
    coordinator.join(3)
    assert coordinator.status()["phase"] == Phase.DRY_RUN_COMPLETE.value
    assert portal.login_calls == 2


def test_ambiguous_purchase_is_retried_once():
    portal = FakePortal(ambiguous_once=True)
    coordinator = WatcherCoordinator(
        lambda: portal,
        live_purchases_allowed=True,
        ambiguous_submit_retries=1,
    )
    coordinator.start(payload_for(portal, dry_run=False))
    coordinator.join(3)
    assert coordinator.status()["phase"] == Phase.PURCHASED.value
    assert portal.submitted == 2


def test_coordinator_accepts_split_seats_when_no_contiguous_pair_exists():
    portal = FakePortal()

    def split_map(showtime, ticket_count, *, strict_screen_rows):
        return (
            Seat("D", 4, row_rank=3, seat_id="D-4", x=40, y=100),
            Seat("D", 6, row_rank=3, seat_id="D-6", x=60, y=100),
        )

    portal.read_seat_map = split_map
    coordinator = WatcherCoordinator(lambda: portal)
    coordinator.start(payload_for(portal))
    coordinator.join(3)
    assert coordinator.status()["phase"] == Phase.DRY_RUN_COMPLETE.value
    assert coordinator.status()["selected_seats"] == ["D4", "D6"]


def test_hour_grid_compresses_runs_and_handles_midnight():
    availability = availability_from_hour_tokens(["0:22", "0:23", "1:0", "1:1"])
    monday_late = datetime(2026, 8, 17, 23, 30)
    tuesday_early = datetime(2026, 8, 18, 1, 30)
    assert availability.is_available(monday_late)
    assert availability.is_available(tuesday_early)
