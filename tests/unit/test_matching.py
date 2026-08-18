from datetime import datetime, time

from amc_bot.matching import normalize_title, title_matches
from amc_bot.models import MovieRequest, Showtime, TimeWindow, WeeklyAvailability


def test_normalization_ignores_case_punctuation_and_whitespace():
    assert normalize_title("  Spider—Man: Across the Spider-Verse! ") == "spider man across the spider verse"


def test_title_matches_exact_and_format_prefix_or_suffix():
    assert title_matches("The Batman", "the-batman")
    assert title_matches("IMAX — The Batman (70MM)", "The Batman")
    assert title_matches("Dune IMAX", "Dune")


def test_title_matches_aliases_but_not_unsafe_substrings():
    assert title_matches("Dead Reckoning", "Mission Impossible", ["Dead Reckoning"])
    assert not title_matches("The Martian 2", "The Martian")
    assert not title_matches("It Ends With Us", "It")


def test_movie_request_can_drive_matching():
    request = MovieRequest(title="Spider-Man", aliases=("Spidey",))
    showtime = Showtime("IMAX Spidey", datetime(2026, 8, 18, 20, tzinfo=None))
    from amc_bot.matching import movie_matches

    assert movie_matches(showtime, request)


def test_timezone_aware_datetime_is_accepted_by_availability():
    availability = WeeklyAvailability({"monday": [TimeWindow(time(20), time(1))]})
    assert availability.is_available(datetime(2026, 8, 18, 0, 30))  # Tuesday, carried from Monday
    assert availability.is_available(datetime(2026, 8, 17, 23, 30).astimezone())
