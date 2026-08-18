"""Small Quart web UI for configuring and starting the AMC watcher."""

from __future__ import annotations

import inspect
import re
import secrets
from typing import Any

from quart import Quart, abort, jsonify, render_template, request

from .local_secrets import LocalCvvStore


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_RADIUS_MILES = 500.0
_MAX_TICKETS = 20
_ACKNOWLEDGEMENT = "PURCHASE"


def _initial_status() -> dict[str, Any]:
    return {"state": "idle", "message": "No watcher started."}


def _safe_status(state: str, message: str) -> dict[str, Any]:
    """Create a status object from controlled values only.

    In particular, never include an exception message or coordinator response:
    either could accidentally contain credentials.
    """

    return {"state": state, "message": message}


def _form_values(form: Any) -> dict[str, Any]:
    """Return non-secret values suitable for re-rendering after validation."""

    aliases = form.get("aliases", "")
    return {
        "email": form.get("email", ""),
        "primary_movie": form.get("primary_movie", ""),
        "aliases": aliases,
        "location": form.get("location", ""),
        "radius_miles": form.get("radius_miles", ""),
        "ticket_count": form.get("ticket_count", "1"),
        "availability": form.getlist("availability"),
        "lincoln_square_70mm_imax_only": form.get("lincoln_square_70mm_imax_only")
        == "on",
        "save_payment_cvv": form.get("save_payment_cvv") == "on",
        "dry_run": form.get("dry_run") == "on",
    }


def _parse_payload(
    form: Any,
    *,
    live_purchases_allowed: bool,
    saved_cvv: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse a MultiDict into a plain dict and validation messages."""

    errors: list[str] = []
    email = form.get("email", "").strip()
    password = form.get("password", "")
    payment_cvv = form.get("payment_cvv", "").strip()
    save_payment_cvv = form.get("save_payment_cvv") == "on"
    primary_movie = form.get("primary_movie", "").strip()
    location = form.get("location", "").strip()
    aliases = []
    seen_aliases: set[str] = set()
    raw_aliases = form.get("aliases", "")
    if len(raw_aliases) > 4_000 or len(raw_aliases.splitlines()) > 20:
        errors.append("Enter no more than 20 short movie aliases.")
    for raw_alias in raw_aliases.splitlines()[:20]:
        alias = raw_alias.strip()
        if len(alias) > 200:
            errors.append("Each movie alias must be 200 characters or fewer.")
            continue
        if alias and alias.casefold() not in seen_aliases:
            aliases.append(alias)
            seen_aliases.add(alias.casefold())

    if not email or not _EMAIL_RE.fullmatch(email):
        errors.append("Enter a valid AMC email address.")
    elif len(email) > 254:
        errors.append("AMC email address is too long.")
    if not password:
        errors.append("Enter your AMC password.")
    elif len(password) > 1_024:
        errors.append("AMC password is too long.")
    if payment_cvv and (not payment_cvv.isdigit() or len(payment_cvv) not in {3, 4}):
        errors.append("Saved-card CVV must be 3 or 4 digits.")
    if save_payment_cvv and not payment_cvv and not saved_cvv:
        errors.append("Enter a CVV before choosing to save it for future runs.")
    if not primary_movie:
        errors.append("Enter a primary movie name.")
    elif len(primary_movie) > 200:
        errors.append("Primary movie name must be 200 characters or fewer.")
    if not location:
        errors.append("Enter a location.")
    elif len(location) > 200:
        errors.append("Location must be 200 characters or fewer.")

    radius_raw = form.get("radius_miles", "").strip()
    try:
        radius_miles = float(radius_raw)
        if not 0 < radius_miles <= _MAX_RADIUS_MILES:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"Radius must be greater than 0 and no more than {_MAX_RADIUS_MILES:g} miles.")
        radius_miles = 0.0

    ticket_raw = form.get("ticket_count", "").strip()
    try:
        ticket_count = int(ticket_raw)
        if not 1 <= ticket_count <= _MAX_TICKETS:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"Ticket count must be a whole number from 1 to {_MAX_TICKETS}.")
        ticket_count = 1

    availability = form.getlist("availability")
    if len(availability) > 168:
        errors.append("Availability cannot contain more than 168 hourly cells.")
        availability = availability[:168]
    valid_availability: list[str] = []
    for token in availability:
        try:
            day_raw, hour_raw = token.split(":", 1)
            day, hour = int(day_raw), int(hour_raw)
            if not 0 <= day <= 6 or not 0 <= hour <= 23 or token != f"{day}:{hour}":
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Availability must use weekday-hour values such as 0:18.")
            continue
        if token not in valid_availability:
            valid_availability.append(token)
    if not valid_availability:
        errors.append("Select at least one available hour.")

    dry_run = form.get("dry_run") == "on"
    # The public/default configuration cannot opt into purchases by crafting a
    # POST; only an explicitly enabled factory can accept a live request.
    if not live_purchases_allowed:
        dry_run = True
    acknowledgement = form.get("live_purchase_acknowledgement", "").strip()
    if live_purchases_allowed and not dry_run and acknowledgement != _ACKNOWLEDGEMENT:
        errors.append("Type PURCHASE to acknowledge live ticket purchases.")

    if errors:
        return None, errors

    # Deliberately a plain payload: callers can adapt it to their domain model.
    return {
        "email": email,
        "password": password,
        "payment_cvv": payment_cvv or (saved_cvv if save_payment_cvv else ""),
        "save_payment_cvv": save_payment_cvv,
        "manage_saved_cvv": form.get("save_payment_cvv_present") == "1" or save_payment_cvv,
        "primary_movie": primary_movie,
        "aliases": aliases,
        "location": location,
        "radius_miles": radius_miles,
        "ticket_count": ticket_count,
        "availability": valid_availability,
        "lincoln_square_70mm_imax_only": form.get("lincoln_square_70mm_imax_only") == "on",
        "dry_run": dry_run,
        "live_purchase_acknowledgement": acknowledgement,
    }, []


async def _call_start(coordinator: Any, payload: dict[str, Any]) -> None:
    result = coordinator.start(payload)
    if inspect.isawaitable(result):
        await result


def create_app(
    coordinator: Any = None,
    live_purchases_allowed: bool = False,
    cvv_store: LocalCvvStore | None = None,
) -> Quart:
    """Build the bounded watcher UI.

    ``coordinator`` may expose either a synchronous or asynchronous ``start``.
    The object is intentionally injected so this module does not depend on the
    watcher/domain implementation.
    """

    app = Quart(__name__, template_folder="templates", static_folder="static")
    app.config["LIVE_PURCHASES_ALLOWED"] = bool(live_purchases_allowed)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.config["FORM_TOKEN"] = secrets.token_urlsafe(32)
    app.extensions["amc_status"] = _initial_status()
    app.extensions["amc_cvv_store"] = cvv_store or LocalCvvStore()

    def load_saved_cvv() -> str | None:
        return app.extensions["amc_cvv_store"].load()

    @app.before_request
    async def require_local_host() -> None:
        # Binding to loopback is the first boundary; host validation also
        # prevents a DNS-rebinding origin from reading the form token.
        hostname = request.host.partition(":")[0].strip("[]").casefold()
        if hostname not in {"127.0.0.1", "localhost"}:
            abort(400)

    @app.after_request
    async def secure_local_response(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        return response

    def current_status() -> dict[str, Any]:
        status_method = getattr(coordinator, "status", None)
        if callable(status_method):
            try:
                value = status_method()
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return dict(app.extensions["amc_status"])

    def valid_form_token(form: Any) -> bool:
        return secrets.compare_digest(
            str(form.get("form_token", "")), str(app.config["FORM_TOKEN"])
        )

    @app.get("/")
    async def index() -> str:
        try:
            has_saved_cvv = load_saved_cvv() is not None
        except OSError:
            has_saved_cvv = False
        return await render_template(
            "index.html",
            values={
                "email": "",
                "primary_movie": "",
                "aliases": "",
                "location": "",
                "radius_miles": "25",
                "ticket_count": "1",
                "availability": [],
                "lincoln_square_70mm_imax_only": False,
                "save_payment_cvv": has_saved_cvv,
                "dry_run": True,
            },
            errors=[],
            status=current_status(),
            form_token=app.config["FORM_TOKEN"],
            live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
            weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        )

    @app.post("/start")
    async def start() -> tuple[str, int] | str:
        form = await request.form
        if not valid_form_token(form):
            return "Invalid or missing local form token.", 403
        values = _form_values(form)
        try:
            saved_cvv = load_saved_cvv()
        except OSError:
            saved_cvv = None
        payload, errors = _parse_payload(
            form,
            live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
            saved_cvv=saved_cvv,
        )
        if errors:
            return (
                await render_template(
                    "index.html",
                    values=values,
                    errors=errors,
                    status=current_status(),
                    form_token=app.config["FORM_TOKEN"],
                    live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
                    weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                ),
                400,
            )

        if coordinator is None or not callable(getattr(coordinator, "start", None)):
            app.extensions["amc_status"] = _safe_status("error", "Watcher coordinator is unavailable.")
            return (
                await render_template(
                    "index.html",
                    values=values,
                    errors=["Watcher coordinator is unavailable."],
                    status=current_status(),
                    form_token=app.config["FORM_TOKEN"],
                    live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
                    weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                ),
                503,
            )

        save_payment_cvv = bool(payload.pop("save_payment_cvv"))
        manage_saved_cvv = bool(payload.pop("manage_saved_cvv"))
        if manage_saved_cvv:
            try:
                if save_payment_cvv:
                    app.extensions["amc_cvv_store"].save(payload["payment_cvv"])
                else:
                    app.extensions["amc_cvv_store"].delete()
            except (OSError, ValueError):
                return (
                    await render_template(
                        "index.html",
                        values=values,
                        errors=["The locally saved CVV could not be updated."],
                        status=current_status(),
                        form_token=app.config["FORM_TOKEN"],
                        live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
                        weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                    ),
                    500,
                )

        try:
            await _call_start(coordinator, payload)  # type: ignore[arg-type]
        except Exception:
            # Do not expose exception text: coordinators may include the login.
            app.extensions["amc_status"] = _safe_status("error", "Watcher could not be started.")
            return (
                await render_template(
                    "index.html",
                    values=values,
                    errors=["Watcher could not be started."],
                    status=current_status(),
                    form_token=app.config["FORM_TOKEN"],
                    live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
                    weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                ),
                500,
            )

        app.extensions["amc_status"] = _safe_status("running", "Watcher started.")
        return await render_template(
            "index.html",
            values=values,
            errors=[],
            status=current_status(),
            form_token=app.config["FORM_TOKEN"],
            live_purchases_allowed=app.config["LIVE_PURCHASES_ALLOWED"],
            weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        )

    @app.get("/status")
    async def status() -> Any:
        return jsonify(current_status())

    @app.post("/resume")
    async def resume() -> tuple[Any, int] | Any:
        form = await request.form
        if not valid_form_token(form):
            return jsonify({"error": "invalid form token"}), 403
        method = getattr(coordinator, "resume", None)
        if not callable(method):
            return jsonify({"error": "resume is unavailable"}), 503
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
        except Exception:
            return jsonify({"error": "watcher is not waiting for human action"}), 409
        return jsonify({"ok": True})

    @app.post("/stop")
    async def stop() -> tuple[Any, int] | Any:
        form = await request.form
        if not valid_form_token(form):
            return jsonify({"error": "invalid form token"}), 403
        method = getattr(coordinator, "stop", None)
        if not callable(method):
            return jsonify({"error": "stop is unavailable"}), 503
        result = method()
        if inspect.isawaitable(result):
            await result
        return jsonify({"ok": True})

    return app
