"""The 48-hour lifetime of a signing session, as values rather than prose.

A session stack carries `startsAt` and `expiresAt` in its config, written once by
`session_ctl.py create`. The program never reads the clock: a `pulumi up` run on
hour 30 must declare exactly what the run on hour 0 declared, or credentials and
tags would drift with every refresh.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

LIFETIME = dt.timedelta(hours=48)

# Session credentials outlive the session by this much. The reaper archives and
# destroys at `expires_at`; the key expiry is the failsafe for a reaper that did
# not run, so it must not fire while a scheduled archive is still reading.
CREDENTIAL_GRACE = dt.timedelta(hours=12)


class WindowError(ValueError):
    """A session window that cannot be what it claims to be."""


@dataclasses.dataclass(frozen=True)
class SessionWindow:
    starts_at: dt.datetime
    expires_at: dt.datetime

    @property
    def credentials_expire_at(self) -> dt.datetime:
        return self.expires_at + CREDENTIAL_GRACE

    def is_expired(self, now: dt.datetime) -> bool:
        return _require_utc(now, "now") >= self.expires_at

    @classmethod
    def opening(cls, starts_at: dt.datetime) -> SessionWindow:
        starts = _require_utc(starts_at, "starts_at").replace(microsecond=0)
        return cls(starts_at=starts, expires_at=starts + LIFETIME)

    @classmethod
    def parse(cls, starts_at: str, expires_at: str) -> SessionWindow:
        starts = _parse_timestamp(starts_at, "startsAt")
        expires = _parse_timestamp(expires_at, "expiresAt")

        if expires - starts != LIFETIME:
            raise WindowError(
                f"expiresAt must be exactly {LIFETIME} after startsAt; "
                f"got {expires - starts} ({starts_at} -> {expires_at})."
            )

        return cls(starts_at=starts, expires_at=expires)


def format_timestamp(value: dt.datetime) -> str:
    """RFC 3339 in UTC with a `Z` suffix, the form Scaleway's API accepts."""
    return _require_utc(value, "value").strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(raw: str, field: str) -> dt.datetime:
    if not raw.endswith("Z"):
        raise WindowError(f"{field} must be a UTC timestamp ending in 'Z', got {raw!r}.")

    try:
        parsed = dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise WindowError(f"{field} is not YYYY-MM-DDTHH:MM:SSZ: {raw!r}.") from exc

    return parsed.replace(tzinfo=dt.timezone.utc)


def _require_utc(value: dt.datetime, field: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise WindowError(f"{field} must be timezone-aware UTC, got {value!r}.")

    return value
