"""AMC UI selector candidates kept separate from workflow code.

AMC changes markup frequently.  The adapter uses semantic controls first and
falls back to these CSS selectors.  Updating this file should be sufficient for
most routine site changes.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectorSet:
    login_link: tuple[str, ...] = (
        "a[href*='login']",
        "a[href*='sign-in']",
        "button[data-testid*='sign-in']",
    )
    email: tuple[str, ...] = (
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='username']",
    )
    password: tuple[str, ...] = (
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
    )
    login_submit: tuple[str, ...] = (
        "button[type='submit']",
        "button[data-testid*='sign-in']",
    )
    movie_search: tuple[str, ...] = (
        "input[type='search']",
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
    )
    showtime_link: tuple[str, ...] = (
        "a[href*='showtime']",
        "a[href*='movies/'][href*='tickets']",
        "button[data-testid*='showtime']",
    )
    ticket_quantity: tuple[str, ...] = (
        "select[name*='quantity']",
        "select[id*='quantity']",
        "input[name*='quantity']",
    )
    continue_button: tuple[str, ...] = (
        "button[type='submit']",
        "button[data-testid*='continue']",
        "button[data-testid*='next']",
    )
    seat: tuple[str, ...] = (
        "button[data-seat]",
        "button[data-seat-id]",
        "[role='button'][aria-label*='Seat']",
        "button[class*='seat']",
    )
    order_button: tuple[str, ...] = (
        "button[data-testid*='purchase']",
        "button[data-testid*='place-order']",
        "button[type='submit']",
    )


AMC_SELECTORS = SelectorSet()
