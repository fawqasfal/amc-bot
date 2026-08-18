"""Command-line entry point for the localhost Quart application."""

from __future__ import annotations

import argparse
import tempfile
import threading
import webbrowser
from pathlib import Path

from .browser import BrowserConfig, create_chrome
from .coordinator import WatcherCoordinator
from .polling import PollingPolicy
from .portal import SeleniumAmcPortal, SeleniumPortalConfig
from .web import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local AMC ticket watcher")
    parser.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    parser.add_argument(
        "--live-purchases",
        action="store_true",
        help="show the live-purchase option; the web form still requires typing PURCHASE",
    )
    parser.add_argument("--driver-path", type=Path, help="path to chromedriver")
    parser.add_argument("--chrome-binary", type=Path, help="path to the Chrome executable")
    parser.add_argument(
        "--profile-directory",
        type=Path,
        help="Chrome profile directory; defaults to a private temporary profile",
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        help="theater timezone used to interpret showtimes (default: America/New_York)",
    )
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="healthy polling floor (minimum: 1)")
    parser.add_argument("--max-poll-seconds", type=float, default=120.0, help="rate-limit backoff ceiling")
    parser.add_argument(
        "--ambiguous-submit-retries",
        type=int,
        default=1,
        help="extra purchase clicks allowed when confirmation is ambiguous (default: 1)",
    )
    parser.add_argument("--max-hours", type=float, default=12.0, help="maximum watcher runtime")
    parser.add_argument("--no-open-ui", action="store_true", help="do not open the localhost form automatically")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    profile = args.profile_directory
    if profile is None:
        profile = Path(tempfile.mkdtemp(prefix="amc-watcher-profile-"))
        profile.chmod(0o700)

    browser_config = BrowserConfig(
        driver_path=args.driver_path,
        chrome_binary=args.chrome_binary,
        profile_directory=profile,
        headless=False,
        detach=True,
    )
    portal_config = SeleniumPortalConfig(
        timezone=args.timezone,
        cleanup_profile_directory=str(profile) if args.profile_directory is None else None,
    )

    def portal_factory() -> SeleniumAmcPortal:
        return SeleniumAmcPortal(create_chrome(browser_config), portal_config)

    coordinator = WatcherCoordinator(
        portal_factory,
        live_purchases_allowed=args.live_purchases,
        polling_policy=PollingPolicy(
            minimum_seconds=args.poll_seconds,
            maximum_seconds=args.max_poll_seconds,
        ),
        max_runtime_seconds=args.max_hours * 60 * 60,
        ambiguous_submit_retries=args.ambiguous_submit_retries,
    )
    app = create_app(coordinator, live_purchases_allowed=args.live_purchases)
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_open_ui:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
