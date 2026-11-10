"""Time helpers shared by the importers. Everything stored is timezone-aware UTC."""

from __future__ import annotations

import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc


def as_utc(value: dt.datetime) -> dt.datetime:
    """A timezone-aware datetime in UTC, millisecond precision. Naive datetimes are rejected on purpose."""
    if value.tzinfo is None:
        raise ValueError("naive datetime: say which timezone it is in")
    value = value.astimezone(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def parse_iso(text: str) -> dt.datetime:
    """ISO-8601 (``Z`` or an offset) -> UTC. A string with no offset is read as UTC."""
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    value = dt.datetime.fromisoformat(s)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return as_utc(value)


def from_epoch_ms(ms: float | int) -> dt.datetime:
    return as_utc(dt.datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC))


def zone(name: str | None) -> dt.tzinfo | None:
    """A named zone (``Europe/Berlin``), or None meaning "this machine's local zone"."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def _windows_local_fallback(naive: dt.datetime) -> dt.datetime:
    """Windows' C runtime cannot convert a local time before 1970 (OSError, Errno 22). Use the zone named by
    RESONANT_ARCHIVE_TZ or TZ when one is set and valid (zoneinfo knows the historical offsets); only without one, the
    offset in force today, which can be an hour or more off for an old date in a zone with daylight saving."""
    for var in ("RESONANT_ARCHIVE_TZ", "TZ"):
        name = os.environ.get(var, "").strip().lstrip(":")
        if name:
            try:
                return naive.replace(tzinfo=ZoneInfo(name))
            except (ZoneInfoNotFoundError, ValueError, OSError):
                continue
    return naive.replace(tzinfo=dt.timezone(dt.datetime.now().astimezone().utcoffset()))


def wall_to_utc(naive: dt.datetime, tz: dt.tzinfo | None) -> dt.datetime:
    """Wall-clock time in ``tz`` (None = this machine's zone, with its daylight-saving rules) -> UTC.

    On the ambiguous autumn hour the first occurrence is taken (fold=0).
    """
    if tz is None:
        if sys.platform != "win32":
            return as_utc(naive.astimezone())  # a naive datetime is interpreted as local time (raises where libc cannot)
        try:
            return as_utc(naive.astimezone())
        except (OSError, OverflowError, ValueError):
            return as_utc(_windows_local_fallback(naive))
    return as_utc(naive.replace(tzinfo=tz))
