"""AMC browser port and the Selenium implementation.

All assumptions about AMC markup live here and in :mod:`amc_bot.selectors`.
The coordinator only sees typed showtimes, seats, and checkout results.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Sequence
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select, WebDriverWait

from .browser import BrowserPage
from .errors import HumanActionRequired, PurchaseDisabled, PurchaseOutcomeUnknown, SiteChanged
from .matching import normalize_title, title_matches
from .models import MovieRequest, Seat
from .ports import AmcPortal, Credentials, PortalShowtime, PurchaseResult
from .purchase import PurchaseGuard
from .selectors import AMC_SELECTORS


LINCOLN_SQUARE = "amc lincoln square 13"
_DISTANCE = re.compile(r"(?P<miles>\d+(?:\.\d+)?)\s*(?:mi|miles?)\b", re.IGNORECASE)
_SEAT_LABELS = (
    re.compile(r"\brow\s*(?P<row>[a-z]{1,3})\b.*?\bseat\s*(?P<number>\d+)\b", re.IGNORECASE),
    re.compile(r"\bseat\s*(?P<row>[a-z]{1,3})[\s-]*(?P<number>\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?P<row>[a-z]{1,3})[\s-]+(?P<number>\d+)\b", re.IGNORECASE),
)
_UNAVAILABLE = ("unavailable", "occupied", "sold", "taken", "not available")
_RESTRICTED = ("wheelchair", "companion", "accessible only", "restricted")
_PAYMENT_ACTION = (
    "one-time code",
    "verification code",
    "verify payment",
    "3-d secure",
    "3d secure",
)
_CONFIRMATION = ("order confirmed", "purchase confirmed", "thank you for your purchase", "confirmation number")
_CVV_SELECTORS = (
    "input[autocomplete='cc-csc']",
    "input[name*='cvv' i]",
    "input[id*='cvv' i]",
    "input[name*='cvc' i]",
    "input[id*='cvc' i]",
    "input[name*='security-code' i]",
)
_OTP_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
)


@dataclass(frozen=True, slots=True)
class SeleniumPortalConfig:
    base_url: str = "https://www.amctheatres.com/"
    timezone: str = "America/New_York"
    element_timeout: float = 12.0
    test_mode: bool = False
    cleanup_profile_directory: str | None = None
    confirmation_timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if self.test_mode and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("test_mode only permits a localhost fixture site")


class SeleniumAmcPortal:
    def __init__(self, driver: WebDriver, config: SeleniumPortalConfig | None = None) -> None:
        self.driver = driver
        self.config = config or SeleniumPortalConfig()
        self.page = BrowserPage(driver, timeout=self.config.element_timeout)
        self.timezone = ZoneInfo(self.config.timezone)
        self._seat_elements: dict[str, WebElement] = {}
        self._left_open = False
        parsed_base = urlparse(self.config.base_url)
        self._origin = (parsed_base.scheme.casefold(), parsed_base.netloc.casefold())

    def _ensure_allowed_url(self, url: str | None = None) -> str:
        value = url or self.driver.current_url
        parsed = urlparse(value)
        if (parsed.scheme.casefold(), parsed.netloc.casefold()) != self._origin:
            raise SiteChanged("Browser navigation left the configured AMC origin")
        if not self.config.test_mode and parsed.scheme.casefold() != "https":
            raise SiteChanged("Production AMC navigation must remain on HTTPS")
        return value

    def _navigate(self, url: str) -> None:
        self._ensure_allowed_url(url)
        self.driver.get(url)
        self._ensure_allowed_url()

    def login(self, credentials: Credentials) -> None:
        self._navigate(self.config.base_url)
        self.page.ensure_not_blocked()

        # A visible logout/account control means this profile is already signed in.
        body = self.page.body_text()
        if "sign out" in body or "log out" in body:
            return

        try:
            link = self.page.first(AMC_SELECTORS.login_link, clickable=True, timeout=3)
            link.click()
        except SiteChanged:
            # Local fixtures and some AMC deployments expose the form directly.
            pass

        self.page.ensure_not_blocked()
        email = self.page.first(AMC_SELECTORS.email)
        password = self.page.first(AMC_SELECTORS.password)
        email.clear()
        email.send_keys(credentials.email)
        password.clear()
        password.send_keys(credentials.password)
        submit = self.page.first(AMC_SELECTORS.login_submit, clickable=True)
        submit.click()

        try:
            WebDriverWait(self.driver, self.config.element_timeout).until(
                lambda driver: not driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                or "sign out" in self.page.body_text()
            )
        except TimeoutException as exc:
            self.page.ensure_not_blocked()
            raise SiteChanged("AMC login did not complete; check the credentials in the open browser") from exc
        self.page.ensure_not_blocked()

    def discover_showtimes(self, movie: MovieRequest) -> list[PortalShowtime]:
        location = quote_plus(str(getattr(movie.location, "name", movie.location) or ""))
        found: dict[str, PortalShowtime] = {}
        search_names = dict.fromkeys(
            value.strip() for value in (movie.title, *movie.aliases) if value.strip()
        )
        for search_name in search_names:
            query = quote_plus(search_name)
            search_url = urljoin(
                self.config.base_url,
                f"search?q={query}&location={location}&radius={movie.radius_miles:g}",
            )
            self._navigate(search_url)
            self.page.ensure_not_blocked()

            for element in self.page.all(AMC_SELECTORS.showtime_link):
                parsed = self._parse_showtime_element(element)
                if parsed is None:
                    continue
                if not title_matches(parsed.movie_title, movie.title, movie.aliases):
                    continue
                if not movie.weekly_availability.is_available(parsed.starts_at):
                    continue
                # Radius is a hard constraint. Do not trust a query parameter alone
                # if the rendered listing does not expose a verifiable distance.
                if parsed.distance_miles is None or parsed.distance_miles > movie.radius_miles:
                    continue
                if movie.lincoln_square_70mm_imax_only:
                    if normalize_title(parsed.theater_name) != LINCOLN_SQUARE:
                        continue
                    if set(normalize_title(parsed.format).split()) != {"imax", "70mm"}:
                        continue
                found[parsed.booking_url] = parsed
        return sorted(found.values(), key=lambda item: (item.starts_at, item.theater_name, item.format))

    def _parse_showtime_element(self, element: WebElement) -> PortalShowtime | None:
        context = element
        try:
            context = element.find_element(
                By.XPATH,
                "ancestor::*[@data-movie-title or @data-theater-name or @data-showtime][1]",
            )
        except NoSuchElementException:
            pass

        def attr(*names: str) -> str:
            for name in names:
                value = element.get_attribute(name) or context.get_attribute(name)
                if value:
                    return value.strip()
            return ""

        href = element.get_attribute("href") or attr("data-href", "data-booking-url")
        title = attr("data-movie-title")
        theater = attr("data-theater-name", "data-theater")
        format_name = attr("data-format", "data-experience")
        auditorium = attr("data-auditorium")
        starts = attr("data-showtime", "data-start", "datetime")
        label = " ".join(
            value for value in (element.get_attribute("aria-label"), element.get_attribute("title"), context.text) if value
        )
        distance_match = _DISTANCE.search(attr("data-distance") or label)
        distance = float(distance_match.group("miles")) if distance_match else None

        if not all((href, title, theater, starts)):
            return None
        try:
            starts_at = datetime.fromisoformat(starts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=self.timezone)
        else:
            starts_at = starts_at.astimezone(self.timezone)
        booking_url = urljoin(self.config.base_url, href)
        try:
            self._ensure_allowed_url(booking_url)
        except SiteChanged:
            return None
        return PortalShowtime(
            movie_title=title,
            starts_at=starts_at,
            theater_name=theater,
            auditorium_name=auditorium,
            format=format_name,
            booking_url=booking_url,
            distance_miles=distance,
        )

    def read_seat_map(
        self,
        showtime: PortalShowtime,
        ticket_count: int,
        *,
        strict_screen_rows: bool,
    ) -> tuple[Seat, ...]:
        self._navigate(showtime.booking_url)
        self.page.ensure_not_blocked()
        self._choose_ticket_count(ticket_count)

        seat_elements = self.page.all(AMC_SELECTORS.seat)
        if not seat_elements:
            try:
                self.page.click_text(("continue", "select seats", "next"), timeout=3)
            except SiteChanged:
                pass
            seat_elements = self.page.all(AMC_SELECTORS.seat)
        if not seat_elements:
            raise SiteChanged("The seat map did not expose recognizable seat controls")

        screen_y = self._screen_y()
        parsed_rows: list[tuple[WebElement, str, int, float, float, bool, bool, str]] = []
        for element in seat_elements:
            parsed = self._parse_seat_element(element)
            if parsed is not None:
                parsed_rows.append((element, *parsed))
        if not parsed_rows:
            raise SiteChanged("Seat controls were found, but their row/number labels could not be read")

        row_y = {row: y for _, row, _, _, y, _, _, _ in parsed_rows}
        if strict_screen_rows and (screen_y is None or len(row_y) < 4):
            raise SiteChanged(
                "Cannot reliably identify the first three screen-facing rows on this seat map"
            )
        ordered_rows = sorted(row_y, key=lambda row: abs(row_y[row] - screen_y)) if screen_y is not None else sorted(row_y)
        ranks = {row: rank for rank, row in enumerate(ordered_rows)}

        self._seat_elements.clear()
        seats: list[Seat] = []
        for element, row, number, x, y, available, restricted, identifier in parsed_rows:
            self._seat_elements[identifier] = element
            seats.append(
                Seat(
                    row=row,
                    seat_number=number,
                    available=available,
                    row_rank=ranks[row],
                    seat_id=identifier,
                    x=x,
                    y=y,
                    restricted=restricted,
                )
            )
        return tuple(seats)

    def _choose_ticket_count(self, ticket_count: int) -> None:
        selects: list[WebElement] = []
        for selector in AMC_SELECTORS.ticket_quantity:
            selects = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if selects:
                break
        if selects:
            element = selects[0]
            if element.tag_name.casefold() == "select":
                control = Select(element)
                try:
                    control.select_by_value(str(ticket_count))
                except NoSuchElementException:
                    match = next(
                        (option for option in control.options if re.search(rf"\b{ticket_count}\b", option.text)),
                        None,
                    )
                    if match is None:
                        raise SiteChanged(f"Ticket quantity {ticket_count} is not offered")
                    match.click()
                selected = control.first_selected_option
                if selected.get_attribute("value") != str(ticket_count) and not re.search(
                    rf"\b{ticket_count}\b", selected.text
                ):
                    raise SiteChanged("AMC did not retain the requested ticket quantity")
            elif element.tag_name.casefold() == "input":
                element.clear()
                element.send_keys(str(ticket_count))
                if element.get_attribute("value") != str(ticket_count):
                    raise SiteChanged("AMC did not retain the requested ticket quantity")
            else:
                raise SiteChanged("The ticket quantity control has an unsupported type")
            return
        raise SiteChanged("The requested ticket quantity could not be set and verified")

    def _screen_y(self) -> float | None:
        candidates = self.driver.find_elements(
            By.CSS_SELECTOR,
            "[data-testid*='screen'], [aria-label*='screen' i], [class*='screen' i]",
        )
        return float(candidates[0].rect["y"]) if candidates else None

    def _parse_seat_element(
        self, element: WebElement
    ) -> tuple[str, int, float, float, bool, bool, str] | None:
        label = " ".join(
            value
            for value in (
                element.get_attribute("aria-label"),
                element.get_attribute("data-seat"),
                element.get_attribute("data-seat-id"),
                element.text,
            )
            if value
        )
        row = element.get_attribute("data-row") or ""
        number_text = element.get_attribute("data-seat-number") or ""
        if not (row and number_text.isdigit()):
            match = next((pattern.search(label) for pattern in _SEAT_LABELS if pattern.search(label)), None)
            if not match:
                return None
            row, number_text = match.group("row"), match.group("number")
        lowered = label.casefold()
        restricted = any(marker in lowered for marker in _RESTRICTED)
        available = element.is_enabled() and not any(marker in lowered for marker in _UNAVAILABLE)
        identifier = (
            element.get_attribute("data-seat-id")
            or element.get_attribute("data-seat")
            or element.get_attribute("id")
            or f"{row.upper()}-{number_text}"
        )
        return (
            row.upper(),
            int(number_text),
            float(element.rect["x"]),
            float(element.rect["y"]),
            available,
            restricted,
            identifier,
        )

    def select_seats(self, seats: Sequence[Seat], guard: PurchaseGuard) -> None:
        guard.require()
        if self.config.test_mode:
            raise PurchaseDisabled("Test mode disables seat selection and inventory holds")
        self._ensure_allowed_url()
        for seat in seats:
            if not seat.seat_id or seat.seat_id not in self._seat_elements:
                raise SiteChanged(f"Seat control {seat.row}{seat.seat_number} is no longer present")
            element = self._seat_elements[seat.seat_id]
            if not element.is_enabled():
                raise SiteChanged(f"Seat {seat.row}{seat.seat_number} became unavailable")
            element.click()

    def prepare_checkout(
        self,
        showtime: PortalShowtime,
        seats: Sequence[Seat],
        ticket_count: int,
        payment_cvv: str | None,
        guard: PurchaseGuard,
    ) -> None:
        guard.require()
        if self.config.test_mode:
            raise PurchaseDisabled("Test mode disables checkout interactions")
        self._ensure_allowed_url()
        self.page.click_text(("continue", "checkout", "next"))
        self._ensure_allowed_url()
        self.page.ensure_not_blocked()
        self._fill_cvv(payment_cvv)
        self._ensure_no_payment_challenge()

        # Prefer a saved payment radio/card if the page requires an explicit choice.
        saved = self.driver.find_elements(
            By.XPATH,
            "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'saved payment')]",
        )
        if saved:
            try:
                saved[0].click()
            except Exception as exc:
                raise SiteChanged("Saved payment method could not be selected") from exc
        self._fill_cvv(payment_cvv)
        self._ensure_no_payment_challenge()
        self._verify_checkout_summary(showtime, seats, ticket_count)

    def _fill_cvv(self, payment_cvv: str | None) -> None:
        if not payment_cvv:
            return
        for selector in _CVV_SELECTORS:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled() and not element.get_attribute("value"):
                    element.send_keys(payment_cvv)
                    element.send_keys(Keys.TAB)

    def _payment_action_pending(self) -> bool:
        text = self.page.body_text()
        actionable_inputs = [
            element
            for selector in (*_CVV_SELECTORS, *_OTP_SELECTORS)
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector)
            if element.is_displayed() and element.is_enabled() and not element.get_attribute("value")
        ]
        foreign_frames = [
            frame
            for frame in self.driver.find_elements(By.CSS_SELECTOR, "iframe")
            if frame.is_displayed()
            and frame.get_attribute("src")
            and urlparse(frame.get_attribute("src")).netloc.casefold() != self._origin[1]
        ]
        return bool(actionable_inputs or foreign_frames or (
            any(marker in text for marker in _PAYMENT_ACTION)
            and not self._exact_order_buttons()
        ))

    def _ensure_no_payment_challenge(self) -> None:
        if self._payment_action_pending():
            raise HumanActionRequired(
                "AMC or the card issuer requires CVV, OTP, or payment verification in the open browser"
            )

    def human_action_pending(self) -> bool:
        try:
            self.page.ensure_not_blocked()
        except HumanActionRequired:
            return True
        return self._payment_action_pending()

    def _verify_checkout_summary(
        self, showtime: PortalShowtime, seats: Sequence[Seat], ticket_count: int
    ) -> None:
        text = normalize_title(self.page.body_text())
        required = (
            normalize_title(showtime.movie_title),
            normalize_title(showtime.theater_name),
            normalize_title(showtime.format),
        )
        if any(value not in text for value in required):
            raise SiteChanged("Checkout summary does not match the selected movie, theater, and format")
        for seat in seats:
            alternatives = (
                normalize_title(f"{seat.row}{seat.seat_number}"),
                normalize_title(f"{seat.row} {seat.seat_number}"),
                normalize_title(f"row {seat.row} seat {seat.seat_number}"),
            )
            if not any(value in text for value in alternatives):
                raise SiteChanged("Checkout summary does not match the selected seats")
        start_time = showtime.starts_at
        time_alternatives = (
            normalize_title(start_time.strftime("%-I:%M %p")),
            normalize_title(start_time.strftime("%H:%M")),
        )
        if not any(value in text for value in time_alternatives):
            raise SiteChanged("Checkout summary does not match the selected showtime")
        quantity_patterns = (
            rf"\b{ticket_count}\s+tickets?\b",
            rf"\bquantity\s+{ticket_count}\b",
        )
        if not any(re.search(pattern, text) for pattern in quantity_patterns):
            raise SiteChanged("Checkout summary does not verify the requested ticket quantity")

    def _exact_order_buttons(self) -> list[WebElement]:
        allowed = {"purchase", "place order", "complete order", "buy tickets"}
        return [
            candidate
            for candidate in self.page.all(AMC_SELECTORS.order_button)
            if normalize_title(candidate.text or candidate.get_attribute("aria-label") or "") in allowed
            and candidate.is_displayed()
            and candidate.is_enabled()
        ]

    def submit_order(self, guard: PurchaseGuard) -> PurchaseResult:
        guard.require()
        if self.config.test_mode:
            raise PurchaseDisabled("Test mode permanently disables the final purchase action")
        self._ensure_allowed_url()
        self.page.ensure_not_blocked()
        self._ensure_no_payment_challenge()

        buttons = self._exact_order_buttons()
        if len(buttons) != 1:
            raise SiteChanged("The final order button could not be identified unambiguously")
        button = buttons[0]

        # One click per call. The coordinator may make a bounded retry when the
        # confirmation result is ambiguous.
        button.click()
        try:
            def confirmation_found(_driver: WebDriver) -> bool:
                for handle in self.driver.window_handles:
                    self.driver.switch_to.window(handle)
                    self._ensure_allowed_url()
                    if "confirmation" in self.driver.current_url.casefold() or any(
                        marker in self.page.body_text() for marker in _CONFIRMATION
                    ):
                        return True
                return False

            WebDriverWait(self.driver, self.config.confirmation_timeout_seconds).until(confirmation_found)
        except Exception as exc:
            self.leave_open()
            raise PurchaseOutcomeUnknown(
                "The order was submitted, but AMC confirmation could not be verified"
            ) from exc

        self.leave_open()
        reference = self._confirmation_reference()
        return PurchaseResult(self.driver.current_url, reference)

    def _confirmation_reference(self) -> str | None:
        text = self.page.body_text()
        match = re.search(r"(?:confirmation|order)\s*(?:number|#|id)?\s*[:#]?\s*([a-z0-9-]{6,})", text, re.IGNORECASE)
        return match.group(1) if match else None

    def leave_open(self) -> None:
        self._left_open = True

    def close(self) -> None:
        if not self._left_open:
            self.driver.quit()
            cleanup = self.config.cleanup_profile_directory
            if cleanup:
                path = Path(cleanup).resolve()
                if path.name.startswith("amc-watcher-profile-"):
                    shutil.rmtree(path, ignore_errors=True)

    def __enter__(self) -> "SeleniumAmcPortal":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
