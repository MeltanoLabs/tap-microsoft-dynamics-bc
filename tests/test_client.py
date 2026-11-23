"""Tests for the timestamp normalisation that keeps replication bookmarks exact."""

from __future__ import annotations

import json

import pytest
import requests

from tap_microsoft_dynamics_bc.client import is_unavailable, normalise_timestamp


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


def _response(status: int, body: object) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()  # noqa: SLF001
    return response


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, {"error": {"code": "Authorization_InsufficientPermissions"}}, True),
        (404, {"error": {"code": "BadRequest_NotFound"}}, True),
        # Seen live: `journals` in a company with no general journal template.
        (400, {"error": {"code": "Internal_DataNotFoundFilter", "message": "x"}}, True),
        # Any other 400 is a real failure and must still stop the run.
        (400, {"error": {"code": "BadRequest_InvalidToken", "message": "x"}}, False),
        (400, {"unexpected": "shape"}, False),
        (500, {"error": {"code": "Internal_DataNotFoundFilter"}}, False),
    ],
)
def test_is_unavailable(status: int, body: object, expected: bool) -> None:  # noqa: FBT001
    assert is_unavailable(_response(status, body)) is expected


def test_is_unavailable_non_json_400() -> None:
    response = requests.Response()
    response.status_code = 400
    response._content = b"<html>"  # noqa: SLF001
    assert is_unavailable(response) is False
