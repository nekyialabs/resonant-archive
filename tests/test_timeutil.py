"""wall_to_utc on a machine's own zone must not raise for a date the C library cannot convert (Windows: before 1970)."""
from __future__ import annotations

import datetime as dt

from resonant_archive.timeutil import wall_to_utc


def test_a_local_time_before_1970_is_converted_instead_of_raising():
    out = wall_to_utc(dt.datetime(1969, 6, 1, 12, 0), None)
    assert out.tzinfo is not None and out.year in (1969,) and abs((out.replace(tzinfo=None) - dt.datetime(1969, 6, 1, 12)).total_seconds()) <= 15 * 3600


def test_a_named_zone_is_unaffected():
    from resonant_archive.timeutil import zone

    assert wall_to_utc(dt.datetime(1969, 6, 1, 12, 0), zone("UTC")) == dt.datetime(1969, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
