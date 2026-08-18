"""Single-run application service coordinating Quart and Selenium."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import time as wall_time
from threading import Event, Lock, Thread
from typing import Any, Callable

from .errors import HumanActionRequired, PurchaseOutcomeUnknown, RateLimited, SiteChanged
from .models import MovieRequest, TimeWindow, WeeklyAvailability
from .polling import AdaptiveDelay, PollOutcome, PollingPolicy
from .ports import AmcPortal, Credentials, PortalShowtime, PurchaseResult
from .purchase import PurchaseGuard
from .seats import SeatBlock, selection_is_contiguous, try_select_seats
from .state import Phase, StatusStore


@dataclass(slots=True)
class SearchJob:
    credentials: Credentials | None
    movie: MovieRequest
    dry_run: bool
    acknowledgement: str
    payment_cvv: str | None


def availability_from_hour_tokens(tokens: list[str]) -> WeeklyAvailability:
    """Compress 7x24 checkbox values into half-open daily time windows."""

    hours_by_day: dict[int, set[int]] = {day: set() for day in range(7)}
    for token in tokens:
        day_text, hour_text = token.split(":", 1)
        day, hour = int(day_text), int(hour_text)
        if not 0 <= day <= 6 or not 0 <= hour <= 23:
            raise ValueError(f"invalid availability token: {token!r}")
        hours_by_day[day].add(hour)

    windows: dict[int, list[TimeWindow]] = {}
    for day, selected in hours_by_day.items():
        if not selected:
            continue
        if len(selected) == 24:
            windows[day] = [TimeWindow(wall_time(0), wall_time(0))]
            continue
        starts = sorted(hour for hour in selected if (hour - 1) not in selected)
        day_windows: list[TimeWindow] = []
        for start in starts:
            end = start
            while end in selected:
                end += 1
            day_windows.append(
                TimeWindow(wall_time(start), wall_time(0) if end == 24 else wall_time(end))
            )
        windows[day] = day_windows
    return WeeklyAvailability(windows)


def job_from_payload(payload: dict[str, Any]) -> SearchJob:
    """Build typed configuration without retaining the original form mapping."""

    credentials = Credentials(str(payload["email"]), str(payload["password"]))
    movie = MovieRequest(
        title=str(payload["primary_movie"]),
        aliases=tuple(str(alias) for alias in payload.get("aliases", ())),
        weekly_availability=availability_from_hour_tokens(list(payload["availability"])),
        location=str(payload["location"]),
        radius_miles=float(payload["radius_miles"]),
        ticket_count=int(payload["ticket_count"]),
        lincoln_square_70mm_imax_only=bool(payload.get("lincoln_square_70mm_imax_only")),
    )
    return SearchJob(
        credentials=credentials,
        movie=movie,
        dry_run=bool(payload.get("dry_run", True)),
        acknowledgement=str(payload.get("live_purchase_acknowledgement", "")),
        payment_cvv=str(payload.get("payment_cvv", "")).strip() or None,
    )


class WatcherCoordinator:
    """Own one browser worker and expose credential-free status to Quart."""

    def __init__(
        self,
        portal_factory: Callable[[], AmcPortal],
        *,
        live_purchases_allowed: bool = False,
        polling_policy: PollingPolicy | None = None,
        max_attempts: int = 2_160,
        max_runtime_seconds: float = 43_200,
        ambiguous_submit_retries: int = 1,
        showtime_refresh_seconds: float = 300.0,
        rng: random.Random | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")
        if ambiguous_submit_retries < 0:
            raise ValueError("ambiguous_submit_retries cannot be negative")
        if showtime_refresh_seconds < 1:
            raise ValueError("showtime_refresh_seconds must be at least 1 second")
        self.portal_factory = portal_factory
        self.live_purchases_allowed = live_purchases_allowed
        self.polling_policy = polling_policy or PollingPolicy()
        self.max_attempts = max_attempts
        self.max_runtime_seconds = max_runtime_seconds
        self.ambiguous_submit_retries = ambiguous_submit_retries
        self.showtime_refresh_seconds = showtime_refresh_seconds
        self.status_store = StatusStore()
        self._rng = rng
        self._thread: Thread | None = None
        self._lock = Lock()
        self._irreversible_lock = Lock()
        self._stop = Event()
        self._resume = Event()
        self._cached_showtimes: list[PortalShowtime] = []
        self._showtimes_refreshed_at = 0.0

    def start(self, payload: dict[str, Any]) -> None:
        job = job_from_payload(dict(payload))
        guard = PurchaseGuard(
            runtime_enabled=self.live_purchases_allowed,
            request_live_purchase=not job.dry_run,
            acknowledgement=job.acknowledgement,
        )
        if not job.dry_run:
            guard.require()

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A watcher is already running")
            self._stop.clear()
            self._resume.clear()
            self._cached_showtimes = []
            self._showtimes_refreshed_at = 0.0
            self.status_store.update(
                phase=Phase.STARTING_BROWSER,
                message="Starting the visible AMC browser",
                attempts=0,
                matching_showtimes=0,
                selected_showtime=None,
                selected_seats=[],
                next_poll_seconds=None,
            )
            self._thread = Thread(
                target=self._run,
                args=(job, guard),
                name="amc-selenium-worker",
                daemon=True,
            )
            self._thread.start()

    def status(self) -> dict[str, Any]:
        return self.status_store.get().public_dict()

    def resume(self) -> None:
        if self.status_store.get().phase != Phase.NEEDS_HUMAN:
            raise RuntimeError("The watcher is not waiting for human action")
        self._resume.set()

    def stop(self) -> None:
        with self._irreversible_lock:
            self._stop.set()
            self._resume.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout)

    def _pause_for_human(self, portal: AmcPortal, exc: HumanActionRequired) -> bool:
        self._resume.clear()
        self.status_store.update(
            phase=Phase.NEEDS_HUMAN,
            message=f"{exc}. Monitoring the browser and continuing automatically when it clears.",
            next_poll_seconds=None,
        )
        while not self._stop.is_set():
            if self._resume.wait(0.2):
                self._resume.clear()
                return True
            try:
                if not portal.human_action_pending():
                    return True
            except RateLimited:
                if self._stop.wait(self.polling_policy.minimum_seconds):
                    return False
        return False

    def _login(self, portal: AmcPortal, job: SearchJob) -> bool:
        while not self._stop.is_set():
            if job.credentials is None:
                raise RuntimeError("Credentials were cleared before login")
            try:
                portal.login(job.credentials)
                return True
            except HumanActionRequired as exc:
                if not self._pause_for_human(portal, exc):
                    return False
        return False

    def _run(self, job: SearchJob, guard: PurchaseGuard) -> None:
        portal: AmcPortal | None = None
        try:
            portal = self.portal_factory()
            if not self._login(portal, job):
                return
            # Best-effort lifetime reduction; Python strings cannot be securely
            # zeroized, so the important guarantee is that they are never logged,
            # persisted, or included in status.
            job.credentials = None
            self.status_store.update(phase=Phase.DISCOVERING, message="Looking for matching showtimes")
            self._watch(portal, job, guard)
        except PurchaseOutcomeUnknown as exc:
            if portal:
                portal.leave_open()
            self.status_store.update(phase=Phase.ERROR, message=str(exc), next_poll_seconds=None)
        except SiteChanged as exc:
            self.status_store.update(phase=Phase.ERROR, message=str(exc), next_poll_seconds=None)
        except Exception:
            # Fail closed and keep all exception details out of UI/status because
            # a third-party exception can include form field values.
            self.status_store.update(
                phase=Phase.ERROR,
                message="The watcher stopped because of an unexpected browser error",
                next_poll_seconds=None,
            )
        finally:
            job.credentials = None
            job.payment_cvv = None
            if self._stop.is_set() and self.status_store.get().phase not in {
                Phase.PURCHASED,
                Phase.DRY_RUN_COMPLETE,
                Phase.ERROR,
            }:
                self.status_store.update(phase=Phase.STOPPED, message="Watcher stopped", next_poll_seconds=None)
            close = getattr(portal, "close", None)
            if callable(close):
                close()

    def _watch(self, portal: AmcPortal, job: SearchJob, guard: PurchaseGuard) -> None:
        started = time.monotonic()
        attempts = 0
        delay_policy = AdaptiveDelay(self.polling_policy, rng=self._rng)

        while (
            not self._stop.is_set()
            and attempts < self.max_attempts
            and time.monotonic() - started < self.max_runtime_seconds
        ):
            attempts += 1
            self.status_store.update(
                phase=Phase.DISCOVERING,
                message="Refreshing matching AMC showtimes",
                attempts=attempts,
                next_poll_seconds=None,
            )
            try:
                match = self._find_available_block(portal, job)
            except HumanActionRequired as exc:
                if not self._pause_for_human(portal, exc):
                    return
                continue
            except RateLimited as exc:
                delay = delay_policy.after_rate_limit(exc.retry_after)
                outcome = PollOutcome.RATE_LIMITED
            except SiteChanged:
                raise
            except Exception:
                delay = delay_policy.after_error()
                outcome = PollOutcome.TRANSIENT_ERROR
            else:
                if match is not None:
                    self._finish_match(portal, job, guard, *match)
                    return
                delay = delay_policy.after_no_seats()
                outcome = PollOutcome.NO_SEATS

            self.status_store.update(
                phase=Phase.POLLING,
                message=(
                    "AMC asked the watcher to slow down"
                    if outcome == PollOutcome.RATE_LIMITED
                    else (
                        "A temporary browser error occurred; retrying with backoff"
                        if outcome == PollOutcome.TRANSIENT_ERROR
                        else "Not enough suitable seats yet"
                    )
                ),
                next_poll_seconds=round(delay, 1),
            )
            if self._stop.wait(delay):
                return

        if not self._stop.is_set():
            self.status_store.update(
                phase=Phase.STOPPED,
                message="Polling limit or run deadline reached without finding seats",
                next_poll_seconds=None,
            )

    def _find_available_block(
        self, portal: AmcPortal, job: SearchJob
    ) -> tuple[PortalShowtime, SeatBlock, bool] | None:
        now = time.monotonic()
        if (
            not self._cached_showtimes
            or now - self._showtimes_refreshed_at >= self.showtime_refresh_seconds
        ):
            self._cached_showtimes = portal.discover_showtimes(job.movie)
            self._showtimes_refreshed_at = now
        showtimes = self._cached_showtimes
        self.status_store.update(matching_showtimes=len(showtimes))
        for showtime in showtimes:
            seats = portal.read_seat_map(
                showtime,
                job.movie.ticket_count,
                strict_screen_rows=job.movie.lincoln_square_70mm_imax_only,
            )
            block = try_select_seats(
                seats,
                job.movie.ticket_count,
                lincoln_square_70mm_imax_only=job.movie.lincoln_square_70mm_imax_only,
            )
            if block:
                return showtime, block, selection_is_contiguous(seats, block)
        return None

    def _finish_match(
        self,
        portal: AmcPortal,
        job: SearchJob,
        guard: PurchaseGuard,
        showtime: PortalShowtime,
        block: SeatBlock,
        contiguous: bool,
    ) -> PurchaseResult | None:
        seat_labels = [f"{seat.row}{seat.seat_number}" for seat in block]
        self.status_store.update(
            phase=Phase.SELECTING_SEATS,
            message=(
                "Found the best centered contiguous seat block"
                if contiguous
                else "No contiguous block was available; using the best centered split seats"
            ),
            selected_showtime=showtime.identity,
            selected_seats=seat_labels,
            next_poll_seconds=None,
        )
        if job.dry_run:
            self.status_store.update(
                phase=Phase.DRY_RUN_COMPLETE,
                message="Dry run complete; no seats were clicked and no order was placed",
            )
            return None

        portal.select_seats(block, guard)
        self.status_store.update(phase=Phase.CHECKOUT, message="Preparing checkout with saved payment")
        while not self._stop.is_set():
            try:
                portal.prepare_checkout(
                    showtime,
                    block,
                    job.movie.ticket_count,
                    job.payment_cvv,
                    guard,
                )
                break
            except HumanActionRequired as exc:
                if not self._pause_for_human(portal, exc):
                    return None
        if self._stop.is_set():
            return None

        ambiguous_failures = 0
        while not self._stop.is_set():
            try:
                with self._irreversible_lock:
                    if self._stop.is_set():
                        return None
                    result = portal.submit_order(guard)
                break
            except HumanActionRequired as exc:
                if not self._pause_for_human(portal, exc):
                    return None
            except PurchaseOutcomeUnknown:
                if ambiguous_failures >= self.ambiguous_submit_retries:
                    raise
                ambiguous_failures += 1
                self.status_store.update(
                    phase=Phase.CHECKOUT,
                    message=f"Confirmation was ambiguous; retrying purchase ({ambiguous_failures}/{self.ambiguous_submit_retries})",
                )
        portal.leave_open()
        self.status_store.update(
            phase=Phase.PURCHASED,
            message=(
                f"Purchase confirmed: {result.reference}"
                if result.reference
                else "Purchase confirmed in the open AMC browser"
            ),
        )
        return result
