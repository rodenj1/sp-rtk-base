"""How a Good / Marginal / Poor level looks on a page."""

from __future__ import annotations

from sp_rtk_base.models.level_models import Level

# Quasar colour of each level.
LEVEL_COLOR: dict[Level, str] = {
    "Good": "positive",
    "Marginal": "warning",
    "Poor": "negative",
}
