"""Formatting shared by the covered UI modules."""

from __future__ import annotations

from datetime import datetime


def format_checked_at(when: datetime) -> str:
    """``7 Oct 09:12``, in the base's local time."""
    local = when.astimezone()
    return f"{local.day} {local:%b %H:%M}"


def format_byte_rate(per_second: float) -> str:
    """Format a byte-rate value as '12.3 KB/s' (or B/s, MB/s, GB/s).

    Rate is the primary 'is data flowing right now' signal — totals
    keep climbing for days and stop being informative once the
    instance has been up long enough that everything looks like a
    big number.  0 B/s reads as 'nothing flowing' immediately;
    a stuck total reads as 'don't know'.
    """
    if per_second < 1024:
        return f"{per_second:.0f} B/s"
    if per_second < 1024 * 1024:
        return f"{per_second / 1024:.1f} KB/s"
    if per_second < 1024 * 1024 * 1024:
        return f"{per_second / (1024 * 1024):.1f} MB/s"
    return f"{per_second / (1024 * 1024 * 1024):.2f} GB/s"
