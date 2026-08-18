"""Movie-title matching with conservative normalization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any


_SPACE = re.compile(r"\s+")
_FORMAT_WORDS = {
    "2d",
    "3d",
    "4dx",
    "70mm",
    "cc",
    "cinema",
    "d-box",
    "dbox",
    "digital",
    "dolby",
    "event",
    "fan",
    "imax",
    "laser",
    "open",
    "prime",
    "reald",
    "captioned",
    "sensory",
    "screenx",
    "standard",
    "theatre",
    "theater",
    "x-l",
}


def normalize_title(value: str) -> str:
    """Normalize punctuation, whitespace, and case without losing words."""

    if not isinstance(value, str):
        raise TypeError("title must be a string")
    value = unicodedata.normalize("NFKC", value).casefold()
    # Treat punctuation (including smart quotes and dashes) as a separator;
    # this keeps "Spider-Man" and "Spider Man" equivalent.
    value = "".join(char if char.isalnum() else " " for char in value)
    return _SPACE.sub(" ", value).strip()


def _tokens(value: str) -> tuple[str, ...]:
    normalized = normalize_title(value)
    return tuple(normalized.split())


def _candidate_has_requested_tokens(candidate: tuple[str, ...], requested: tuple[str, ...]) -> bool:
    if not requested or len(requested) == 1:
        return False
    width = len(requested)
    for offset in range(len(candidate) - width + 1):
        if candidate[offset : offset + width] != requested:
            continue
        extras = candidate[:offset] + candidate[offset + width :]
        # Listings commonly wrap a movie in format labels, but arbitrary
        # subtitles and sequels must not be silently treated as the same film.
        if all(token in _FORMAT_WORDS for token in extras):
            return True
    return False


def _one_word_format_suffix(candidate: tuple[str, ...], requested: tuple[str, ...]) -> bool:
    """Permit ``Dune`` vs ``IMAX Dune`` without making ``It`` too broad."""

    if len(requested) != 1 or requested[0] in _FORMAT_WORDS:
        return False
    if requested[0] not in candidate:
        return False
    extras = candidate[: candidate.index(requested[0])] + candidate[candidate.index(requested[0]) + 1 :]
    return bool(extras) and all(token in _FORMAT_WORDS for token in extras)


def title_matches(candidate_title: str, requested_title: str, aliases: Iterable[str] = ()) -> bool:
    """Return whether a listing title belongs to the requested movie.

    Matching is exact after normalization, or token-aware containment for
    multi-word titles. Containment requires complete word boundaries, avoiding
    accidental matches such as ``The Martian`` matching ``Martian 2`` by a
    shared substring. Single-word titles only accept recognized format labels
    as extra prefix/suffix words.
    """

    candidate = _tokens(candidate_title)
    if not candidate:
        return False
    for requested_title_value in (requested_title, *tuple(aliases)):
        requested = _tokens(requested_title_value)
        if not requested:
            continue
        if candidate == requested:
            return True
        if _candidate_has_requested_tokens(candidate, requested):
            return True
        if _one_word_format_suffix(candidate, requested):
            return True
    return False


def matches_title(candidate_title: str, requested_title: str, aliases: Iterable[str] = ()) -> bool:
    return title_matches(candidate_title, requested_title, aliases)


def movie_matches(candidate: Any, request: Any, aliases: Iterable[str] = ()) -> bool:
    """Match a listing or title against a string or ``MovieRequest`` object."""

    candidate_title = getattr(candidate, "movie_title", candidate)
    requested_title = getattr(request, "title", request)
    configured_aliases = getattr(request, "aliases", aliases)
    return title_matches(candidate_title, requested_title, configured_aliases)


def matches_movie(candidate: Any, request: Any, aliases: Iterable[str] = ()) -> bool:
    return movie_matches(candidate, request, aliases)


normalize_movie_title = normalize_title
