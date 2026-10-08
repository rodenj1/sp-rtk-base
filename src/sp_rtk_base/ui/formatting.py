"""Formatting shared by the covered UI modules."""

from __future__ import annotations

from datetime import datetime


def format_checked_at(when: datetime) -> str:
    """``7 Oct 09:12``, in the base's local time."""
    local = when.astimezone()
    return f"{local.day} {local:%b %H:%M}"
