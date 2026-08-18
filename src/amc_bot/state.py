"""Thread-safe watcher status exposed to the local UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock
from typing import Any


class Phase(StrEnum):
    IDLE = "idle"
    STARTING_BROWSER = "starting_browser"
    NEEDS_HUMAN = "needs_human"
    DISCOVERING = "discovering"
    POLLING = "polling"
    SELECTING_SEATS = "selecting_seats"
    CHECKOUT = "checkout"
    DRY_RUN_COMPLETE = "dry_run_complete"
    PURCHASED = "purchased"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(slots=True)
class Status:
    phase: Phase = Phase.IDLE
    message: str = "Ready"
    attempts: int = 0
    matching_showtimes: int = 0
    selected_showtime: str | None = None
    selected_seats: list[str] = field(default_factory=list)
    next_poll_seconds: float | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["phase"] = self.phase.value
        return value


class StatusStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._value = Status()

    def get(self) -> Status:
        with self._lock:
            return Status(**asdict(self._value))

    def update(self, **changes: Any) -> Status:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._value, key):
                    raise AttributeError(key)
                setattr(self._value, key, value)
            self._value.updated_at = datetime.now(timezone.utc).isoformat()
            return Status(**asdict(self._value))
