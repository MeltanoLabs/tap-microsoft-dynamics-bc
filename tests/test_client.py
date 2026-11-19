"""Tests for the timestamp normalisation that keeps replication bookmarks exact."""

from __future__ import annotations

import pytest

from tap_microsoft_dynamics_bc.client import normalise_timestamp


def test_normalised_timestamps_sort_in_time_order() -> None:
    # Seen live: Business Central trims trailing zeros, and as strings the
    # earliest of these sorts last, so the bookmark lagged the latest row.
    raw = ["2026-11-03T03:18:18.4Z", "2026-11-03T03:18:18.457Z", "2026-11-03T03:18:18.45Z"]
    assert max(raw) == "2026-11-03T03:18:18.4Z"

    assert max(normalise_timestamp(v) for v in raw) == "2026-11-03T03:18:18.457000Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01T10:00:00Z", "2026-03-01T10:00:00.000000Z"),
        ("2026-03-01T10:00:00.123Z", "2026-03-01T10:00:00.123000Z"),
        # Truncating only ever moves a bookmark earlier, so no row is skipped.
        ("2026-03-01T10:00:00.1234567Z", "2026-03-01T10:00:00.123456Z"),
        ("2026-03-01T11:00:00.5+01:00", "2026-03-01T10:00:00.500000Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000Z"),
        ("2026-03-01", "2026-03-01"),
        ("not a timestamp", "not a timestamp"),
    ],
)
def test_normalise_timestamp(value: str, expected: str) -> None:
    assert normalise_timestamp(value) == expected
