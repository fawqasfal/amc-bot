"""Small, dependency-free domain objects used by the AMC watcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import IntEnum
from math import isfinite
from typing import Iterable, Mapping


class Weekday(IntEnum):
    """ISO weekday numbers (Monday is zero, as returned by ``date.weekday``)."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


def _weekday(value: int | str | Weekday) -> int:
    if isinstance(value, Weekday):
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 6:
            return value
    if isinstance(value, str):
        value = value.strip().lower()
        names = {day.name.lower(): int(day) for day in Weekday}
        names.update({day.name[:3].lower(): int(day) for day in Weekday})
        if value in names:
            return names[value]
    raise ValueError(f"invalid weekday: {value!r}")


def _time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"invalid time: {value!r}") from exc
    raise TypeError("time values must be datetime.time or an ISO time string")


@dataclass(frozen=True, order=True)
class TimeWindow:
    """A half-open local-time interval.

    If ``end`` is earlier than ``start``, the interval continues into the next
    day. Equal endpoints represent an all-day window, which is useful for a
    request that has no time-of-day restriction.
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _time(self.start))
        object.__setattr__(self, "end", _time(self.end))

    @property
    def crosses_midnight(self) -> bool:
        return self.end < self.start

    def contains(self, value: time) -> bool:
        value = _time(value)
        if self.start == self.end:
            return True
        if self.crosses_midnight:
            return value >= self.start or value < self.end
        return self.start <= value < self.end


@dataclass(frozen=True)
class WeeklyAvailability:
    """One or more time windows for each day of the week."""

    windows: Mapping[int | str | Weekday, Iterable[TimeWindow | tuple[time | str, time | str]]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        normalized: dict[int, tuple[TimeWindow, ...]] = {}
        for day, values in self.windows.items():
            weekday = _weekday(day)
            converted: list[TimeWindow] = []
            for value in values:
                if isinstance(value, TimeWindow):
                    converted.append(value)
                else:
                    try:
                        start, end = value
                    except (TypeError, ValueError) as exc:
                        raise ValueError("availability windows must be (start, end) pairs") from exc
                    converted.append(TimeWindow(start, end))
            normalized[weekday] = tuple(converted)
        object.__setattr__(self, "windows", normalized)

    def is_available(self, showtime: datetime) -> bool:
        """Return whether ``showtime`` falls in one of the configured windows.

        A timezone-aware datetime is evaluated in its own timezone. A naive
        datetime is accepted for callers that already use local time.
        """

        if not isinstance(showtime, datetime):
            raise TypeError("showtime must be a datetime")
        day = showtime.weekday()
        current_time = showtime.timetz().replace(tzinfo=None)
        for window in self.windows.get(day, ()):
            if window.contains(current_time):
                return True

        # The after-midnight part belongs to the preceding day's window.
        previous_day = (day - 1) % 7
        for window in self.windows.get(previous_day, ()):
            if window.crosses_midnight and current_time < window.end:
                return True
        return False

    def available(self, showtime: datetime) -> bool:
        return self.is_available(showtime)


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float | None = None
    longitude: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("location name cannot be empty")
        for value, label in ((self.latitude, "latitude"), (self.longitude, "longitude")):
            if value is not None and (not isfinite(value)):
                raise ValueError(f"{label} must be finite")


@dataclass(frozen=True, init=False)
class Seat:
    """A seat returned by a theater listing.

    ``number`` is accepted as a convenient synonym for ``seat_number``.
    ``row_rank`` is measured from the screen, starting at zero.
    """

    row: str
    seat_number: int
    available: bool
    row_rank: int
    seat_id: str | None
    x: float | None
    y: float | None
    restricted: bool

    def __init__(
        self,
        row: str,
        seat_number: int | None = None,
        available: bool = True,
        row_rank: int = 0,
        seat_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
        restricted: bool = False,
        *,
        number: int | None = None,
    ) -> None:
        if seat_number is None:
            seat_number = number
        if seat_number is None:
            raise TypeError("seat_number (or number) is required")
        if number is not None and int(seat_number) != int(number):
            raise ValueError("seat_number and number disagree")
        if not isinstance(row, str) or not row.strip():
            raise ValueError("seat row cannot be empty")
        if int(seat_number) != seat_number or int(seat_number) < 0:
            raise ValueError("seat_number must be a non-negative integer")
        if int(row_rank) != row_rank or row_rank < 0:
            raise ValueError("row_rank must be a non-negative integer")
        for coordinate, label in ((x, "x"), (y, "y")):
            if coordinate is not None and not isfinite(coordinate):
                raise ValueError(f"{label} must be finite")
        object.__setattr__(self, "row", row)
        object.__setattr__(self, "seat_number", int(seat_number))
        object.__setattr__(self, "available", bool(available))
        object.__setattr__(self, "row_rank", int(row_rank))
        object.__setattr__(self, "seat_id", seat_id)
        object.__setattr__(self, "x", float(x) if x is not None else None)
        object.__setattr__(self, "y", float(y) if y is not None else None)
        object.__setattr__(self, "restricted", bool(restricted))

    @property
    def number(self) -> int:
        return self.seat_number


@dataclass(frozen=True, init=False)
class Showtime:
    movie_title: str
    starts_at: datetime
    theater_name: str
    auditorium_name: str
    format: str | None
    seats: tuple[Seat, ...]

    def __init__(
        self,
        movie_title: str = "",
        starts_at: datetime | None = None,
        theater_name: str = "",
        auditorium_name: str = "",
        format: str | None = None,
        seats: Iterable[Seat] = (),
        *,
        start_time: datetime | None = None,
        datetime_value: datetime | None = None,
        theater: str | None = None,
        auditorium: str | None = None,
    ) -> None:
        if starts_at is None:
            starts_at = start_time
        if starts_at is None:
            starts_at = datetime_value
        if starts_at is None or not isinstance(starts_at, datetime):
            raise TypeError("starts_at (or start_time) must be a datetime")
        if start_time is not None and starts_at != start_time:
            raise ValueError("starts_at and start_time disagree")
        if datetime_value is not None and starts_at != datetime_value:
            raise ValueError("starts_at and datetime_value disagree")
        if theater is not None:
            theater_name = theater
        if auditorium is not None:
            auditorium_name = auditorium
        object.__setattr__(self, "movie_title", movie_title)
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "theater_name", theater_name)
        object.__setattr__(self, "auditorium_name", auditorium_name)
        object.__setattr__(self, "format", format)
        object.__setattr__(self, "seats", tuple(seats))

    @property
    def start_time(self) -> datetime:
        return self.starts_at

    @property
    def theater(self) -> str:
        return self.theater_name

    @property
    def auditorium(self) -> str:
        return self.auditorium_name


@dataclass(frozen=True, init=False)
class MovieRequest:
    """A movie search/watch request and its ticket constraints."""

    title: str
    aliases: tuple[str, ...] = ()
    weekly_availability: WeeklyAvailability = field(default_factory=WeeklyAvailability)
    location: str | Location | None = None
    radius_miles: float = 25.0
    ticket_count: int = 1
    lincoln_square_70mm_imax_only: bool = False

    def __init__(
        self,
        title: str,
        aliases: Iterable[str] = (),
        weekly_availability: WeeklyAvailability | Mapping[object, Iterable[object]] | None = None,
        location: str | Location | None = None,
        radius_miles: float = 25.0,
        ticket_count: int = 1,
        lincoln_square_70mm_imax_only: bool = False,
        *,
        radius: float | None = None,
        tickets: int | None = None,
        lincoln_square_70mm: bool | None = None,
        lincoln_square_70mm_only: bool | None = None,
    ) -> None:
        if radius is not None:
            radius_miles = radius
        if tickets is not None:
            ticket_count = tickets
        if lincoln_square_70mm is not None:
            lincoln_square_70mm_imax_only = lincoln_square_70mm
        if lincoln_square_70mm_only is not None:
            lincoln_square_70mm_imax_only = lincoln_square_70mm_only
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "aliases", (aliases,) if isinstance(aliases, str) else tuple(aliases))
        if weekly_availability is None:
            weekly_availability = WeeklyAvailability()
        elif not isinstance(weekly_availability, WeeklyAvailability):
            weekly_availability = WeeklyAvailability(weekly_availability)
        object.__setattr__(self, "weekly_availability", weekly_availability)
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "radius_miles", radius_miles)
        object.__setattr__(self, "ticket_count", ticket_count)
        object.__setattr__(self, "lincoln_square_70mm_imax_only", bool(lincoln_square_70mm_imax_only))
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("movie title cannot be empty")
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if not isinstance(self.weekly_availability, WeeklyAvailability):
            object.__setattr__(self, "weekly_availability", WeeklyAvailability(self.weekly_availability))
        if self.radius_miles < 0 or not isfinite(self.radius_miles):
            raise ValueError("radius_miles must be a finite non-negative number")
        if self.ticket_count < 1:
            raise ValueError("ticket_count must be positive")

    @property
    def lincoln_square_70mm_imax(self) -> bool:
        return self.lincoln_square_70mm_imax_only

    @property
    def radius(self) -> float:
        return self.radius_miles

    @property
    def tickets(self) -> int:
        return self.ticket_count


# Friendly aliases for adapters that use slightly different terminology.
Availability = WeeklyAvailability
AvailabilityWindow = TimeWindow
Movie = MovieRequest
MovieSpec = MovieRequest
SearchCriteria = MovieRequest
WatchRequest = MovieRequest
TicketRequest = MovieRequest
