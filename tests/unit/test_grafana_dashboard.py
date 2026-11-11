"""Tests for the shipped Grafana dashboard JSON.

They parse ``docs/monitoring/grafana-dashboard-sp-rtk-base.json`` the way
an import would, and check what it queries against what the app exports,
so a metric rename can't silently empty a panel.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sp_rtk_base.services.metrics_service import MetricsService

DASHBOARD = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "monitoring"
    / "grafana-dashboard-sp-rtk-base.json"
)

# Prometheus's own per-target series, written on every scrape attempt.
SCRAPE_STATUS = {"up"}

PROMQL_KEYWORDS = {
    "and",
    "or",
    "unless",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
    "offset",
    "inf",
    "nan",
}


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text())


def _panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels", []))


def _prometheus_exprs(panel: dict[str, Any]) -> list[str]:
    return [
        target["expr"]
        for target in panel.get("targets", [])
        if "expr" in target and "loki" not in str(target.get("datasource", ""))
    ]


def _fleet_overview() -> dict[str, Any]:
    [panel] = [
        p for p in _panels(_dashboard()["panels"]) if p.get("title") == "Fleet overview"
    ]
    return panel


def metric_names(expr: str) -> set[str]:
    """The metric names a PromQL expression selects."""
    expr = re.sub(r'"(?:[^"\\]|\\.)*"', "", expr)  # string literals
    expr = re.sub(r"\{[^}]*\}", "", expr)  # label matchers
    expr = re.sub(r"\[[^\]]*\]", "", expr)  # range and subquery durations
    expr = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", "", expr
    )
    expr = re.sub(r"\$\{?\w+\}?", "", expr)  # Grafana variables
    names = re.findall(r"(?<![\w.])([a-zA-Z_:][\w:]*)\b(?!\s*\()", expr)
    return {n for n in names if n.lower() not in PROMQL_KEYWORDS}


def _exported_names() -> set[str]:
    names: set[str] = set()
    for family in MetricsService().registry.collect():
        names.add(family.name)
        if family.type == "counter":
            names.update({f"{family.name}_total", f"{family.name}_created"})
    return names


class TestMetricNameParsing:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('max by (base) (up{job="sp-rtk-base"})', {"up"}),
            (
                "increase(sp_rtk_base_host_undervoltage_events_total[24h]) > 0",
                {"sp_rtk_base_host_undervoltage_events_total"},
            ),
            (
                "a * on(base, interface) group_left(band) b or vector(2.4e9)",
                {"a", "b"},
            ),
            ('label_replace(x, "band", "5 GHz", "", "")', {"x"}),
        ],
    )
    def test_it_finds_the_selected_metrics_and_nothing_else(
        self, expr: str, expected: set[str]
    ) -> None:
        assert metric_names(expr) == expected


class TestShippedDashboard:
    def test_the_uid_is_unchanged_so_links_and_reimports_keep_working(self) -> None:
        assert _dashboard()["uid"] == "sp-rtk-base-overview"

    def test_every_queried_metric_is_exported_or_is_scrape_status(self) -> None:
        queried = {
            name
            for panel in _panels(_dashboard()["panels"])
            for expr in _prometheus_exprs(panel)
            for name in metric_names(expr)
        }

        assert queried, "the dashboard queries no metrics"
        assert queried - _exported_names() - SCRAPE_STATUS == set()

    def test_the_fleet_overview_is_anchored_on_scrape_status_for_the_job(
        self,
    ) -> None:
        exprs = _prometheus_exprs(_fleet_overview())

        assert any(re.search(r'\bup\{job="sp-rtk-base"\}', e) for e in exprs)

    def test_no_fleet_overview_query_uses_the_base_variable(self) -> None:
        exprs = _prometheus_exprs(_fleet_overview())

        assert exprs
        assert [e for e in exprs if re.search(r"\$\{?base\b", e)] == []
