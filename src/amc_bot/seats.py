"""Deterministic seat-block selection."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from statistics import median

from .models import Seat


SeatBlock = tuple[Seat, ...]


def _eligible(seat: Seat, lincoln_square_70mm: bool) -> bool:
    return seat.available and not seat.restricted and (not lincoln_square_70mm or seat.row_rank >= 3)


def contiguous_blocks(
    seats: Iterable[Seat],
    count: int,
    *,
    lincoln_square_70mm: bool = False,
    lincoln_square_70mm_imax_only: bool | None = None,
) -> tuple[SeatBlock, ...]:
    """Return all available same-row blocks of exactly ``count`` seats."""

    if lincoln_square_70mm_imax_only is not None:
        lincoln_square_70mm = lincoln_square_70mm_imax_only
    if count < 1:
        raise ValueError("count must be positive")

    all_seats = tuple(seats)
    grouped: dict[str, list[Seat]] = defaultdict(list)
    for seat in all_seats:
        if not isinstance(seat, Seat):
            raise TypeError("seats must contain Seat objects")
        if _eligible(seat, lincoln_square_70mm):
            grouped[seat.row].append(seat)

    blocks: list[SeatBlock] = []
    for row in sorted(grouped, key=lambda value: (value.casefold(), value)):
        row_seats = sorted(grouped[row], key=lambda seat: (seat.seat_number, seat.seat_id or ""))
        physical_gaps = [
            right.x - left.x
            for left, right in zip(row_seats, row_seats[1:])
            if left.x is not None
            and right.x is not None
            and right.seat_number == left.seat_number + 1
            and right.x > left.x
        ]
        typical_gap = median(physical_gaps) if physical_gaps else None
        run: list[Seat] = []
        for seat in row_seats:
            if run:
                previous = run[-1]
                crosses_number_gap = seat.seat_number != previous.seat_number + 1
                crosses_physical_aisle = (
                    typical_gap is not None
                    and previous.x is not None
                    and seat.x is not None
                    and seat.x - previous.x > typical_gap * 1.8
                )
                if crosses_number_gap or crosses_physical_aisle:
                    run = []
            run.append(seat)
            if len(run) >= count:
                blocks.extend(tuple(run[offset : offset + count]) for offset in range(len(run) - count + 1))
    return tuple(blocks)


def _block_score(block: SeatBlock, all_seats: tuple[Seat, ...]) -> tuple[float, float, str, int, tuple[int, ...]]:
    row = block[0].row
    row_seats = [seat for seat in all_seats if seat.row == row]
    physical_x = [seat.x for seat in all_seats if seat.x is not None]
    if physical_x and all(seat.x is not None for seat in block):
        theater_center = (min(physical_x) + max(physical_x)) / 2
        block_center = sum(seat.x for seat in block if seat.x is not None) / len(block)
        horizontal_distance = abs(block_center - theater_center)
    else:
        row_min = min(seat.seat_number for seat in row_seats)
        row_max = max(seat.seat_number for seat in row_seats)
        row_center = (row_min + row_max) / 2
        block_center = (block[0].seat_number + block[-1].seat_number) / 2
        horizontal_distance = abs(block_center - row_center)

    physical_y = [seat.y for seat in all_seats if seat.y is not None]
    if physical_y and all(seat.y is not None for seat in block):
        vertical_center = (min(physical_y) + max(physical_y)) / 2
        vertical_distance = abs(sum(seat.y for seat in block if seat.y is not None) / len(block) - vertical_center)
    else:
        ranks = [seat.row_rank for seat in all_seats]
        vertical_center = (min(ranks) + max(ranks)) / 2
        vertical_distance = abs(block[0].row_rank - vertical_center)
    return (
        horizontal_distance,
        vertical_distance,
        row.casefold(),
        block[0].seat_number,
        tuple(seat.seat_number for seat in block),
    )


def _individual_score(seat: Seat, all_seats: tuple[Seat, ...]) -> tuple[float, float, str, int]:
    physical_x = [candidate.x for candidate in all_seats if candidate.x is not None]
    if physical_x and seat.x is not None:
        horizontal = abs(seat.x - (min(physical_x) + max(physical_x)) / 2)
    else:
        same_row = [candidate.seat_number for candidate in all_seats if candidate.row == seat.row]
        horizontal = abs(seat.seat_number - (min(same_row) + max(same_row)) / 2)

    physical_y = [candidate.y for candidate in all_seats if candidate.y is not None]
    if physical_y and seat.y is not None:
        vertical = abs(seat.y - (min(physical_y) + max(physical_y)) / 2)
    else:
        ranks = [candidate.row_rank for candidate in all_seats]
        vertical = abs(seat.row_rank - (min(ranks) + max(ranks)) / 2)
    return horizontal, vertical, seat.row.casefold(), seat.seat_number


def _best_split_selection(
    all_seats: tuple[Seat, ...], count: int, lincoln_square_70mm: bool
) -> SeatBlock:
    eligible = tuple(seat for seat in all_seats if _eligible(seat, lincoln_square_70mm))
    if len(eligible) < count:
        raise ValueError("not enough available seats satisfy the request")

    # Prefer keeping everyone in one row even if intervening seats or an aisle
    # make the selection non-contiguous. Within each row, the comparator picks
    # the exact seats closest to the physical horizontal center.
    grouped: dict[str, list[Seat]] = defaultdict(list)
    for seat in eligible:
        grouped[seat.row].append(seat)
    same_row_candidates: list[SeatBlock] = []
    for row_seats in grouped.values():
        if len(row_seats) >= count:
            chosen = sorted(row_seats, key=lambda seat: _individual_score(seat, all_seats))[:count]
            same_row_candidates.append(tuple(sorted(chosen, key=lambda seat: seat.seat_number)))
    if same_row_candidates:
        return min(
            same_row_candidates,
            key=lambda choice: (
                sum(_individual_score(seat, all_seats)[0] for seat in choice),
                sum(_individual_score(seat, all_seats)[1] for seat in choice),
                choice[0].row.casefold(),
                tuple(seat.seat_number for seat in choice),
            ),
        )

    # If no row contains enough seats, take the globally most centered seats.
    chosen = sorted(eligible, key=lambda seat: _individual_score(seat, all_seats))[:count]
    return tuple(sorted(chosen, key=lambda seat: (seat.row.casefold(), seat.seat_number)))


def selection_is_contiguous(seats: Iterable[Seat], selection: SeatBlock) -> bool:
    if not selection:
        return False
    return selection in contiguous_blocks(seats, len(selection))


def select_seats(
    seats: Iterable[Seat],
    count: int,
    *,
    lincoln_square_70mm: bool = False,
    lincoln_square_70mm_imax_only: bool | None = None,
) -> SeatBlock:
    """Choose centered seats, preferring a contiguous block when available.

    If no contiguous block exists, choose discontiguous seats in one row when
    possible, then fall back to the individually most centered available seats.
    """

    all_seats = tuple(seats)
    blocks = contiguous_blocks(
        all_seats,
        count,
        lincoln_square_70mm=lincoln_square_70mm,
        lincoln_square_70mm_imax_only=lincoln_square_70mm_imax_only,
    )
    if blocks:
        return min(blocks, key=lambda block: _block_score(block, all_seats))
    if lincoln_square_70mm_imax_only is not None:
        lincoln_square_70mm = lincoln_square_70mm_imax_only
    return _best_split_selection(all_seats, count, lincoln_square_70mm)


def try_select_seats(
    seats: Iterable[Seat],
    count: int,
    *,
    lincoln_square_70mm: bool = False,
    lincoln_square_70mm_imax_only: bool | None = None,
) -> SeatBlock | None:
    try:
        return select_seats(
            seats,
            count,
            lincoln_square_70mm=lincoln_square_70mm,
            lincoln_square_70mm_imax_only=lincoln_square_70mm_imax_only,
        )
    except ValueError:
        return None


choose_seats = select_seats
find_best_seats = select_seats
