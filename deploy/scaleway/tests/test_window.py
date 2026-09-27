import datetime as dt

import pytest

from documenso_scw.window import CREDENTIAL_GRACE, LIFETIME, SessionWindow, WindowError, format_timestamp

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)


def test_lifetime_is_48_hours():
    assert LIFETIME == dt.timedelta(hours=48)


def test_opening_sets_expiry_48h_later_and_drops_microseconds():
    window = SessionWindow.opening(START.replace(microsecond=123456))

    assert window.starts_at == START
    assert window.expires_at == dt.datetime(2026, 10, 3, 8, 0, 0, tzinfo=UTC)


def test_credentials_outlive_the_session_by_the_grace_period():
    window = SessionWindow.opening(START)

    assert window.credentials_expire_at == window.expires_at + CREDENTIAL_GRACE
    assert window.credentials_expire_at == dt.datetime(2026, 10, 3, 20, 0, 0, tzinfo=UTC)


def test_parse_round_trips_format():
    window = SessionWindow.opening(START)

    parsed = SessionWindow.parse(format_timestamp(window.starts_at), format_timestamp(window.expires_at))

    assert parsed == window
    assert format_timestamp(parsed.expires_at) == "2026-10-03T08:00:00Z"


@pytest.mark.parametrize(
    ("starts", "expires"),
    [
        ("2026-10-01T08:00:00Z", "2026-10-03T08:00:01Z"),
        ("2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z"),
        ("2026-10-01T08:00:00Z", "2026-10-10T08:00:00Z"),
    ],
)
def test_parse_rejects_any_lifetime_other_than_48h(starts, expires):
    with pytest.raises(WindowError, match="exactly 2 days"):
        SessionWindow.parse(starts, expires)


@pytest.mark.parametrize(
    "raw", ["2026-10-01T08:00:00", "2026-10-01T08:00:00+02:00", "2026-10-01 08:00:00Z", "tomorrow"]
)
def test_parse_rejects_non_utc_or_malformed_timestamps(raw):
    with pytest.raises(WindowError):
        SessionWindow.parse(raw, "2026-10-03T08:00:00Z")


def test_is_expired_at_and_after_expiry_only():
    window = SessionWindow.opening(START)

    assert window.is_expired(window.expires_at - dt.timedelta(seconds=1)) is False
    assert window.is_expired(window.expires_at) is True
    assert window.is_expired(window.expires_at + dt.timedelta(hours=1)) is True


def test_naive_datetimes_are_refused():
    with pytest.raises(WindowError, match="timezone-aware UTC"):
        SessionWindow.opening(dt.datetime(2026, 10, 1, 8, 0, 0))

    window = SessionWindow.opening(START)
    with pytest.raises(WindowError):
        window.is_expired(dt.datetime(2026, 10, 5))


def test_non_utc_offsets_are_refused():
    paris = dt.timezone(dt.timedelta(hours=2))

    with pytest.raises(WindowError):
        SessionWindow.opening(dt.datetime(2026, 10, 1, 10, 0, 0, tzinfo=paris))
