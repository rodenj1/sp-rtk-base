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


def _rows() -> list[dict[str, Any]]:
    return [p for p in _dashboard()["panels"] if p["type"] == "row"]


def _per_base_row() -> dict[str, Any]:
    [row] = [
        r for r in _rows() if r.get("repeat") == "base" and "Logs" not in r["title"]
    ]
    return row


class TestPerBaseSection:
    def test_the_per_base_row_repeats_on_base_and_is_open(self) -> None:
        row = _per_base_row()

        assert row["collapsed"] is False
        assert "$" in row["title"] and "base" in row["title"]

    def test_the_logs_row_repeats_on_base_collapsed_with_its_loki_panel_inside(
        self,
    ) -> None:
        [logs] = [
            r for r in _rows() if r.get("repeat") == "base" and "Logs" in r["title"]
        ]

        assert logs["collapsed"] is True
        assert [p["type"] for p in logs["panels"]] == ["logs"]
        assert "loki" in str(logs["panels"][0]["datasource"])

    def test_no_panel_reads_another_panels_results(self) -> None:
        # A repeated row's clones would all read the original row's panel.
        datasources = [
            str(p.get("datasource", "")) for p in _panels(_dashboard()["panels"])
        ]

        assert [d for d in datasources if "-- Dashboard --" in d] == []

    def test_every_per_base_query_is_filtered_by_base(self) -> None:
        panels = _dashboard()["panels"]
        start = panels.index(_per_base_row())
        section = [p for p in panels[start + 1 :] if p["type"] != "row"]

        assert section
        exprs = [e for p in section for e in _prometheus_exprs(p)]
        assert [e for e in exprs if 'base=~"$base"' not in e] == []


class TestLayout:
    def test_fleet_trends_follows_the_fleet_overview_with_one_colour_per_base(
        self,
    ) -> None:
        panels = _dashboard()["panels"]
        [trends] = [r for r in _rows() if r["title"] == "Fleet trends"]
        start = panels.index(trends)
        graphs = []
        for p in panels[start + 1 :]:
            if p["type"] == "row":
                break
            graphs.append(p)

        assert panels[start - 1]["title"] == "Fleet overview"
        assert len(graphs) == 4
        for g in graphs:
            assert (
                g["fieldConfig"]["defaults"]["color"]["mode"]
                == "palette-classic-by-name"
            )
            for t in g["targets"]:
                assert t["legendFormat"] == "{{base}}"
                assert 'base=~"$base"' in t["expr"]

    @pytest.mark.parametrize(
        "old",
        [
            "Service Overview",
            "Throughput",
            "Per-Destination Health",
            "Per-Destination Activity",
            "Reliability",
            "Signal Quality",
        ],
    )
    def test_the_old_rows_are_gone(self, old: str) -> None:
        assert [r["title"] for r in _rows() if old in r["title"]] == []

    def test_the_per_destination_graphs_are_dropped(self) -> None:
        titles = [p.get("title", "") for p in _panels(_dashboard()["panels"])]

        assert [t for t in titles if "per destination" in t.lower()] == []

    def test_the_version_is_bumped_past_the_fleet_overview_s(self) -> None:
        assert _dashboard()["version"] > 3


def _base_variable() -> dict[str, Any]:
    [variable] = [v for v in _dashboard()["templating"]["list"] if v["name"] == "base"]
    return variable


class TestBaseVariable:
    def test_it_lists_every_scraped_base_so_a_down_base_stays_selectable(
        self,
    ) -> None:
        query = _base_variable()["query"]["query"]

        assert re.search(r'\bup\{job="sp-rtk-base"\}', query)

    def test_its_query_selects_only_exported_metrics_or_scrape_status(self) -> None:
        names = metric_names(_base_variable()["query"]["query"])

        assert names - _exported_names() - SCRAPE_STATUS == set()

    def test_the_fleet_overview_drill_down_url_encodes_the_base(self) -> None:
        links = [
            link["url"]
            for o in _fleet_overview()["fieldConfig"]["overrides"]
            for prop in o["properties"]
            if prop["id"] == "links"
            for link in prop["value"]
        ]

        assert links
        assert all("var-base=${__data.fields.base:percentencode}" in u for u in links)
