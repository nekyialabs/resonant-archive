"""wall_to_utc on the machine's own zone. Linux and macOS: unchanged. Windows: a date its C runtime cannot convert (before
1970) is read in RESONANT_ARCHIVE_TZ / TZ when set (historical offsets), else at today's offset."""
from __future__ import annotations

import datetime as dt
import sys

import pytest

from resonant_archive import timeutil
from resonant_archive.timeutil import wall_to_utc, zone


def test_a_named_zone_is_unaffected():
    assert wall_to_utc(dt.datetime(1969, 6, 1, 12, 0), zone("UTC")) == dt.datetime(1969, 6, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_the_fallback_uses_the_named_zone_with_its_historical_offset(monkeypatch):
    monkeypatch.setenv("RESONANT_ARCHIVE_TZ", "Europe/London")     # British Standard Time: UTC+1 all year in 1969
    out = timeutil._windows_local_fallback(dt.datetime(1969, 1, 15, 12, 0))
    assert out.utcoffset() == dt.timedelta(hours=1)
    monkeypatch.delenv("RESONANT_ARCHIVE_TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    assert timeutil._windows_local_fallback(dt.datetime(1969, 1, 15, 12, 0)).utcoffset() == dt.timedelta(hours=-5)


def test_the_fallback_ignores_an_unknown_zone_and_uses_todays_offset(monkeypatch):
    monkeypatch.setenv("RESONANT_ARCHIVE_TZ", "Not/AZone")
    monkeypatch.delenv("TZ", raising=False)
    out = timeutil._windows_local_fallback(dt.datetime(1969, 1, 15, 12, 0))
    assert out.utcoffset() == dt.datetime.now().astimezone().utcoffset()


@pytest.mark.skipif(sys.platform != "win32", reason="the fallback is Windows only")
def test_on_windows_a_local_time_before_1970_converts(monkeypatch):
    monkeypatch.setenv("RESONANT_ARCHIVE_TZ", "Europe/London")
    assert wall_to_utc(dt.datetime(1969, 1, 15, 12, 0), None) == dt.datetime(1969, 1, 15, 11, 0, tzinfo=dt.timezone.utc)


def test_elsewhere_the_local_conversion_is_exactly_astimezone(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows has the fallback")
    d = dt.datetime(2026, 3, 1, 10, 0)
    assert wall_to_utc(d, None) == timeutil.as_utc(d.astimezone())
