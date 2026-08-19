"""Real Selenium integration tests against a localhost-only fake cinema.

Nothing in this module can navigate to AMC: ``SeleniumPortalConfig(test_mode)``
rejects every non-localhost base URL, and the final order method is permanently
disabled in test mode.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest
from selenium.common.exceptions import WebDriverException

from amc_bot.browser import BrowserConfig, create_chrome
from amc_bot.errors import HumanActionRequired, PurchaseDisabled
from amc_bot.models import MovieRequest, TimeWindow, WeeklyAvailability
from amc_bot.portal import Credentials, SeleniumAmcPortal, SeleniumPortalConfig
from amc_bot.purchase import PurchaseGuard
from amc_bot.seats import select_seats


LOGIN = """<!doctype html><html><body>
<form method="post" action="/account">
  <input type="email" name="email"><input type="password" name="password">
  <button type="submit">Sign in</button>
</form></body></html>"""

HOME = """<!doctype html><html><body><a href="/login">Sign in</a></body></html>"""
ACCOUNT = """<!doctype html><html><body><button>Sign out</button></body></html>"""

SEARCH = """<!doctype html><html><body>
<article data-movie-title="Dune: Part Two" data-theater-name="AMC Lincoln Square 13"
 data-format="70MM IMAX" data-auditorium="IMAX" data-showtime="2026-08-18T20:00:00-04:00"
 data-distance="2.1 miles">
 <a href="/showtime/1" data-showtime="2026-08-18T20:00:00-04:00">8:00 PM</a>
</article>
<article data-movie-title="Wrong Movie" data-theater-name="AMC Lincoln Square 13"
 data-format="70MM IMAX" data-showtime="2026-08-18T20:00:00-04:00">
 <a href="/showtime/2" data-showtime="2026-08-18T20:00:00-04:00">8:00 PM</a>
</article>
<article data-movie-title="Dune: Part Two" data-theater-name="AMC Lincoln Square 13"
 data-format="70MM IMAX" data-showtime="2026-08-18T20:00:00-04:00" data-distance="1 mile">
 <a href="https://evil.example/fake-seat-map" data-showtime="2026-08-18T20:00:00-04:00">8:00 PM</a>
</article>
</body></html>"""


def seat_map() -> str:
    buttons = []
    for row_index, row in enumerate("ABCD"):
        for number in range(1, 9):
            x = number * 36
            y = 70 + row_index * 45
            buttons.append(
                f'<button data-seat-id="{row}-{number}" data-row="{row}" '
                f'data-seat-number="{number}" aria-label="Row {row} Seat {number} available" '
                f'style="position:absolute;left:{x}px;top:{y}px">{row}{number}</button>'
            )
    return """<!doctype html><html><body>
    <select name="quantity"><option value="1">1</option><option value="2">2</option></select>
    <div data-testid="screen" style="position:absolute;left:80px;top:20px">SCREEN</div>
    %s
    <button data-testid="purchase" onclick="document.body.dataset.purchased='true'">Purchase</button>
    </body></html>""" % "".join(buttons)


CHALLENGE = """<!doctype html><html><body>
<main><div role="group">Botdeflector human verification</div>
<div>Verification in progress</div></main></body></html>"""

PAYMENT = """<!doctype html><html><body>
<label>CVV <input name="cvv" value=""></label>
</body></html>"""

PURCHASE_ERROR = """<!doctype html><html><body>
<div role="alert">AMC checkout: An error has occurred while processing the order.</div>
<button>Place order</button>
</body></html>"""

PURCHASE_NETWORK_ERROR = """<!doctype html><html><body>
<div role="alert">NetworkError when attempting to fetch resource.</div>
<button>Place order</button>
</body></html>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        search = SEARCH if "dune" in parse_qs(parsed.query).get("q", [""])[0].casefold() else "<html><body></body></html>"
        body = {
            "/": HOME,
            "/login": LOGIN,
            "/account": ACCOUNT,
            "/search": search,
            "/showtime/1": seat_map(),
            "/showtime/2": seat_map(),
            "/challenge": CHALLENGE,
            "/payment": PAYMENT,
            "/purchase-error": PURCHASE_ERROR,
            "/purchase-network-error": PURCHASE_NETWORK_ERROR,
        }.get(path, "not found")
        self.send_response(200 if body != "not found" else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(303)
        self.send_header("Location", "/account")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A003
        pass


@contextmanager
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(2)


@pytest.fixture(scope="module")
def driver():
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("local Chrome is unavailable")
    with TemporaryDirectory(prefix="amc-fixture-profile-") as profile:
        try:
            value = create_chrome(
                BrowserConfig(
                    chrome_binary=chrome,
                    profile_directory=Path(profile),
                    headless=True,
                    detach=False,
                )
            )
        except WebDriverException as exc:
            pytest.skip(f"local Chrome integration unavailable: {exc.__class__.__name__}")
        yield value
        value.quit()


def test_local_flow_parses_showtime_and_ranks_without_clicking_or_buying(driver):
    with fixture_server() as base_url:
        portal = SeleniumAmcPortal(
            driver,
            SeleniumPortalConfig(base_url=base_url, test_mode=True, element_timeout=2),
        )
        portal.login(Credentials("fixture@example.com", "fixture-password"))
        request = MovieRequest(
            "Dune: Part Two",
            weekly_availability=WeeklyAvailability({1: [TimeWindow(time(19), time(22))]}),
            location="10023",
            radius_miles=5,
            ticket_count=2,
            lincoln_square_70mm_imax_only=True,
        )
        showtimes = portal.discover_showtimes(request)
        assert len(showtimes) == 1

        seats = portal.read_seat_map(showtimes[0], 2, strict_screen_rows=True)
        block = select_seats(seats, 2, lincoln_square_70mm=True)
        assert block[0].row == "D"
        assert [seat.number for seat in block] == [4, 5]
        assert driver.find_elements("css selector", "button[aria-pressed='true']") == []

        with pytest.raises(PurchaseDisabled, match="Test mode"):
            portal.submit_order(PurchaseGuard(True, True, "PURCHASE"))
        assert driver.find_element("tag name", "body").get_attribute("data-purchased") is None


def test_alias_is_used_as_an_actual_search_query(driver):
    with fixture_server() as base_url:
        portal = SeleniumAmcPortal(
            driver,
            SeleniumPortalConfig(base_url=base_url, test_mode=True, element_timeout=2),
        )
        request = MovieRequest(
            "Internal Title",
            aliases=("Dune: Part Two",),
            weekly_availability=WeeklyAvailability({1: [TimeWindow(time(19), time(22))]}),
            location="10023",
            radius_miles=5,
        )
        assert len(portal.discover_showtimes(request)) == 1


def test_completed_or_hidden_cvv_control_can_resume(driver):
    with fixture_server() as base_url:
        portal = SeleniumAmcPortal(
            driver,
            SeleniumPortalConfig(base_url=base_url, test_mode=True, element_timeout=1),
        )
        driver.get(f"{base_url}payment")
        with pytest.raises(HumanActionRequired):
            portal._ensure_no_payment_challenge()
        field = driver.find_element("css selector", "input[name='cvv']")
        portal._fill_cvv("123")
        assert field.get_attribute("value") == "123"
        portal._ensure_no_payment_challenge()
        driver.execute_script("arguments[0].value=''; arguments[0].style.display='none'", field)
        portal._ensure_no_payment_challenge()


def test_local_human_gate_is_detected_and_never_bypassed(driver):
    with fixture_server() as base_url:
        portal = SeleniumAmcPortal(
            driver,
            SeleniumPortalConfig(base_url=base_url, test_mode=True, element_timeout=1),
        )
        driver.get(f"{base_url}challenge")
        with pytest.raises(HumanActionRequired):
            portal.page.ensure_not_blocked()


@pytest.mark.parametrize("path", ["purchase-error", "purchase-network-error"])
def test_vague_purchase_error_substrings_are_recognized_as_retryable(driver, path):
    with fixture_server() as base_url:
        portal = SeleniumAmcPortal(
            driver,
            SeleniumPortalConfig(base_url=base_url, test_mode=True, element_timeout=1),
        )
        driver.get(f"{base_url}{path}")
        assert portal._retryable_checkout_error_present()


def test_test_mode_refuses_a_production_origin():
    with pytest.raises(ValueError, match="localhost"):
        SeleniumPortalConfig(base_url="https://www.amctheatres.com/", test_mode=True)
