"""Typed failures used to drive the watcher state machine."""


class WatcherError(RuntimeError):
    """Base class for expected watcher failures."""


class HumanActionRequired(WatcherError):
    """The browser needs a person to complete a challenge or verification."""


class RateLimited(WatcherError):
    """AMC asked the browser to slow down."""

    def __init__(self, message: str = "AMC requested a slower polling rate", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SiteChanged(WatcherError):
    """The expected page controls could not be found."""


class PurchaseDisabled(WatcherError):
    """A final order was attempted without both live-purchase safeguards."""


class PurchaseOutcomeUnknown(WatcherError):
    """The order click occurred but confirmation could not be established."""


class NoSuitableSeats(WatcherError):
    """There are not enough valid seats for the requested ticket count."""
