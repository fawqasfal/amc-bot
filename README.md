# AMC Ticket Watcher

A local Quart application that uses a visible, ordinary Selenium/Chrome session
to watch AMC showtimes, rank seats, and optionally check out using a
payment method already saved on the account.

If AMC presents a human-verification or issuer challenge, the watcher monitors
the visible browser five times per second and continues automatically as soon as
the person completes it. Rate-limit pages trigger exponential backoff and retry.

## What it accepts

- AMC email and password, retained only long enough to log in and never written
  to a configuration file, response, or status event.
- A primary movie title and explicit aliases, one per line. Matching normalizes
  punctuation/case but will not accept arbitrary substring matches.
- A Monday-Sunday, 24-hour checkbox grid. A selected `18:00` cell means the
  half-open interval from 18:00 through 18:59 in the configured theater timezone.
- ZIP/city, maximum radius, and ticket count.
- Optional `AMC Lincoln Square 13` + `70MM IMAX` mode. The adapter derives row
  order from the seat-map screen geometry and excludes the three screen-nearest
  rows. If screen geometry cannot be established, it stops instead of guessing.

Seats must be available and unrestricted. The watcher prefers a contiguous block
in one row, but if none exists it chooses the most centered split seats—first in
one row when possible, then across rows if necessary. The comparator uses
physical seat coordinates when AMC exposes them, prioritizing horizontal center
and using vertical center and stable labels only as tie breakers.

## Install and run

Python 3.11+, Google Chrome, and a compatible `chromedriver` are required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
PYTHONPATH=src .venv/bin/python -m amc_bot.cli
```

Open <http://127.0.0.1:8765/> if it does not open automatically. The server is
intentionally bound to `127.0.0.1`, not the LAN.

Dry-run is the default. It logs in, discovers showtimes, reads the seat map, and
reports the best block. It does **not** click seats because even a seat click can
create a temporary inventory hold.

Live mode needs two independent choices:

```bash
PYTHONPATH=src .venv/bin/python -m amc_bot.cli --live-purchases
```

Then uncheck **Dry run** and type the exact acknowledgement `PURCHASE` in the
form. A crafted form submission cannot enable purchase unless the process was
also started with `--live-purchases`.

Useful options:

```text
--driver-path /path/to/chromedriver
--chrome-binary /path/to/chrome
--profile-directory /private/profile/path
--timezone America/New_York
--poll-seconds 3
--max-poll-seconds 120
--ambiguous-submit-retries 5
--max-hours 12
```

Polling defaults to a three-second healthy interval, permits a one-second floor,
adds jitter, caches discovered showtimes for five minutes so seat-map checks stay
fast, and backs off immediately on rate limits or transient failures.

## Payment verification

A saved card does not guarantee a fully unattended checkout. The startup form
accepts an optional saved-card CVV and has a simple **Save this CVV locally for
future runs** checkbox. When unchecked, the CVV is retained only for the current
run and any previously saved value is removed. When checked, the app writes only
the CVV to a user-private file (`0700` directory and `0600` file), never renders
the saved digits into the page, and lets a blank CVV field reuse it on later
runs. The default macOS location is:

```text
~/Library/Application Support/AMC Ticket Watcher/payment_cvv
```

On other platforms it uses `$XDG_DATA_HOME/amc-ticket-watcher/payment_cvv` or
`~/.local/share/amc-ticket-watcher/payment_cvv`. Set `AMC_WATCHER_DATA_DIR` to
override the directory. Uncheck the setup checkbox and start a run to delete the
saved value.

The watcher autofills matching CVV/CVC controls. If AMC or the issuer asks for a
one-time code, reauthentication, CAPTCHA, or 3-D Secure, complete it in Chrome;
the bot detects when the challenge clears and resumes without another click.

If confirmation is ambiguous after the final order click, the watcher retries
the purchase up to five times by default (configurable with
`--ambiguous-submit-retries`). On confirmed purchase it leaves the confirmation
tab open and stops all searching.

## Testing without spending money

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

The suite has three boundaries:

1. Pure unit tests cover aliases, weekly/midnight windows, seat contiguity,
   split-seat fallback, physical-center scoring, Lincoln Square row exclusion, polling, and the
   two-key purchase guard.
2. Coordinator tests use an in-memory fake portal and assert dry-run never calls
   seat selection, checkout, or final submit. A fake live path verifies that
   submit is called once without contacting any website.
3. Selenium integration tests run against a localhost fixture cinema. Adapter
   test mode rejects every non-localhost base URL and raises before the final
   purchase control can be clicked. The test also asserts the fixture's purchase
   marker is untouched.

No test uses AMC production, real credentials, or a real payment method.

## AMC markup changes

AMC changes its frontend and currently presents automated sessions with a
human-verification gate. Semantic fallbacks and CSS candidates are isolated in
[`src/amc_bot/selectors.py`](src/amc_bot/selectors.py), while parsing and click
logic are isolated in [`src/amc_bot/portal.py`](src/amc_bot/portal.py). The local
fixture proves the workflow and safety boundaries, but production selectors may
need calibration after a person completes AMC's gate. The adapter fails closed
when it cannot identify a control unambiguously; it does not guess or bypass the
gate.
