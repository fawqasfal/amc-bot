import pytest

from amc_bot.models import Seat
from amc_bot.seats import contiguous_blocks, select_seats, try_select_seats


def row_seats(row: str, numbers: range, *, available: set[int] | None = None, rank: int = 3):
    available = set(numbers) if available is None else available
    return [Seat(row, number, number in available, rank) for number in numbers]


def test_selects_available_contiguous_same_row_seats_near_horizontal_center():
    seats = row_seats("A", range(1, 11), available={1, 2, 3, 4, 5, 6, 7, 8, 9, 10})
    assert [seat.number for seat in select_seats(seats, 3)] == [4, 5, 6]


def test_unavailable_seat_breaks_contiguity():
    seats = row_seats("A", range(1, 8), available={1, 2, 4, 5, 6, 7})
    assert [seat.number for seat in select_seats(seats, 3)] == [4, 5, 6]
    assert all(block[0].row == block[-1].row for block in contiguous_blocks(seats, 2))


def test_horizontal_tie_prefers_reasonable_vertical_center_then_stable_row():
    seats = row_seats("A", range(1, 5), rank=0) + row_seats("B", range(1, 5), rank=1) + row_seats(
        "C", range(1, 5), rank=2
    )
    # Every row has the same horizontal center; rank 1 is the vertical center.
    assert select_seats(seats, 2)[0].row == "B"


def test_lincoln_square_excludes_first_three_screen_facing_rows():
    seats = row_seats("A", range(1, 6), rank=0) + row_seats("B", range(1, 6), rank=1) + row_seats(
        "C", range(1, 6), rank=2
    ) + row_seats("D", range(1, 6), rank=3)
    chosen = select_seats(seats, 2, lincoln_square_70mm=True)
    assert chosen[0].row == "D"


def test_no_contiguous_block_falls_back_to_centered_split_seats():
    seats = row_seats("A", range(1, 4), available={1, 3})
    assert [seat.number for seat in try_select_seats(seats, 2)] == [1, 3]
    assert [seat.number for seat in select_seats(seats, 2)] == [1, 3]


def test_not_enough_available_seats_returns_none_or_raises():
    seats = row_seats("A", range(1, 4), available={2})
    assert try_select_seats(seats, 2) is None
    with pytest.raises(ValueError, match="not enough available"):
        select_seats(seats, 2)


def test_physical_center_wins_when_seat_numbers_are_asymmetric():
    seats = [
        Seat("A", 1, x=0),
        Seat("A", 2, x=10),
        Seat("A", 3, x=20),
        Seat("A", 4, x=35),
        Seat("A", 5, x=45),
    ]
    assert [seat.number for seat in select_seats(seats, 2)] == [3, 4]


def test_large_physical_aisle_breaks_otherwise_consecutive_numbers():
    seats = [
        Seat("A", 1, x=0),
        Seat("A", 2, x=10),
        Seat("A", 3, x=20),
        Seat("A", 4, x=80),
        Seat("A", 5, x=90),
        Seat("A", 6, x=100),
    ]
    blocks = contiguous_blocks(seats, 2)
    assert [3, 4] not in [[seat.number for seat in block] for block in blocks]
