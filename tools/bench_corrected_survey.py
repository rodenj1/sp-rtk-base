#!/usr/bin/env python3
"""Log a Corrected survey-in on real hardware, for the bench acceptance (#197).

Run it next to a station (e.g. test-base) while a Corrected survey-in runs.
It polls the station's API and records, once a second:

- the survey's progress (``GET /api/device/survey-in``): RTK status,
  correction age, whether the source is connected and its last error,
  observation time and accuracy, outcome;
- the live position (``GET /api/device/position``), for the spread of RTK
  Fixed positions over 5 and 30 minutes;
- every ``--ack-every`` seconds, a receiver read (``GET /api/device/base-config``,
  a CFG-VALGET round trip): whether it answered, and how long it took, while
  corrections are being written on the same port.

It writes every sample to CSV and prints a summary to paste into #197: the
outcome, time to first Fixed, accuracy, the spread of Fixed positions over 5
and 30 minutes, the committed position (compare it across runs for
repeatability), the receiver reads, and whether the base is back on air.

Know what the figures can and can't show:

- The spread uses the live position (NAV-PVT, about 1 cm resolution); a few
  millimetres of spread is below what it can resolve.
- A survey completes once it has its minimum Fixed time, so the 30-minute
  spread needs a run with ``--min-duration 1800``.
- Time to first Fixed counts from when the logger started: start the survey
  with ``--start`` for it to mean "from the survey's start".
- The driver retries a receiver read that got no reply (up to 3 times), so a
  missed reply shows as a slow read, not a failed one: watch ``slow``.
- The logger's own polls share the receiver port with the survey.

Usage:
    uv run python tools/bench_corrected_survey.py --url http://test-base:8080 \\
        --start "EarthScope P475" --csv run1.csv

``--start NAME`` starts a Corrected survey-in against the saved Correction
source NAME (with ``--min-duration`` and ``--accuracy-mm``); without it, the
logger follows a survey already started from the Survey page. It stops when
the survey ends, or on Ctrl-C (the survey keeps running).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# The geodesy the station itself uses; importable wherever sp-rtk-base is.
from sp_rtk_base.services.geodesy import llh_to_ecef

FIVE_MIN_S = 300.0
THIRTY_MIN_S = 1800.0
# A window counts as covered when the run ends within this of its end (the
# survey can commit a second before the logger's clock reaches it).
WINDOW_SLACK_S = 2.0
# A receiver read slower than this was probably retried after a missed reply.
SLOW_READ_MS = 1000.0


@dataclass(frozen=True)
class Sample:
    """One second of a run: the live position and the survey's progress."""

    t: float  # seconds since the logger started
    rtk_status: str
    latitude: float
    longitude: float
    altitude_m: float
    horizontal_accuracy_m: float
    vertical_accuracy_m: float
    progress: dict[str, Any] = field(default_factory=dict[str, Any])
    # False when the position read failed (the station reports 0, 0, 0).
    position_ok: bool = True


@dataclass(frozen=True)
class Ack:
    """One receiver read while corrections were being injected."""

    t: float
    ok: bool
    latency_ms: float


@dataclass(frozen=True)
class Summary:
    """The figures #197 records for one run."""

    outcome: str | None
    abort_reason: str | None
    run_time_s: float | None
    time_to_first_fixed_s: float | None
    observation_time_s: int | None
    final_accuracy_mm: float | None
    typical_accuracy_3d_mm: float | None
    spread_5min_mm: float | None
    spread_30min_mm: float | None
    # The committed fixed base (a completed survey only): compare across runs.
    latitude: float | None
    longitude: float | None
    altitude_m: float | None
    acks: int
    ack_failures: int
    ack_slow: int
    ack_p50_ms: float | None
    ack_p95_ms: float | None
    ack_max_ms: float | None


def _spread_mm(samples: list[Sample], start: float, window_s: float) -> float | None:
    """3D std (ECEF) of the Fixed positions in ``[start, start + window_s)``.

    None unless the run covered the whole window.
    """
    if not samples or samples[-1].t < start + window_s - WINDOW_SLACK_S:
        return None
    points = [
        llh_to_ecef(s.latitude, s.longitude, s.altitude_m)
        for s in samples
        if s.rtk_status == "fixed" and s.position_ok and start <= s.t < start + window_s
    ]
    if len(points) < 2:
        return None
    variance = 0.0
    for axis in range(3):
        values = [p[axis] for p in points]
        mean = statistics.fmean(values)
        variance += statistics.fmean((v - mean) ** 2 for v in values)
    return math.sqrt(variance) * 1000.0


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[round(q) - 1]


def summarise(samples: list[Sample], acks: list[Ack]) -> Summary:
    """The run's figures: time to first Fixed, spread, accuracy, acks."""
    fixed = [s for s in samples if s.rtk_status == "fixed"]
    first_fixed = fixed[0].t if fixed else None
    last = samples[-1].progress if samples else {}
    latencies = [a.latency_ms for a in acks if a.ok]
    committed = last.get("outcome") == "completed"
    return Summary(
        outcome=last.get("outcome"),
        abort_reason=last.get("abort_reason"),
        run_time_s=samples[-1].t if samples else None,
        time_to_first_fixed_s=first_fixed,
        observation_time_s=last.get("duration_seconds"),
        final_accuracy_mm=last.get("mean_accuracy_mm"),
        typical_accuracy_3d_mm=(
            statistics.median(
                math.hypot(s.horizontal_accuracy_m, s.vertical_accuracy_m) * 1000.0
                for s in fixed
                if s.position_ok
            )
            if any(s.position_ok for s in fixed)
            else None
        ),
        spread_5min_mm=(
            _spread_mm(samples, first_fixed, FIVE_MIN_S)
            if first_fixed is not None
            else None
        ),
        spread_30min_mm=(
            _spread_mm(samples, first_fixed, THIRTY_MIN_S)
            if first_fixed is not None
            else None
        ),
        latitude=last.get("latitude") if committed else None,
        longitude=last.get("longitude") if committed else None,
        altitude_m=last.get("altitude_m") if committed else None,
        acks=len(acks),
        ack_failures=sum(1 for a in acks if not a.ok),
        ack_slow=sum(1 for latency in latencies if latency > SLOW_READ_MS),
        ack_p50_ms=_percentile(latencies, 50),
        ack_p95_ms=_percentile(latencies, 95),
        ack_max_ms=max(latencies) if latencies else None,
    )


# ---------------------------------------------------------------------------
# Talking to the station
# ---------------------------------------------------------------------------


def _request(
    url: str, body: dict[str, Any] | None = None, timeout: float = 10.0
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _start(base: str, source: str, min_duration: int, accuracy_mm: int) -> None:
    try:
        reply = _request(
            f"{base}/api/device/configure/corrected-survey-in",
            {
                "correction_source": source,
                "min_duration_seconds": min_duration,
                "accuracy_limit_mm": accuracy_mm,
            },
        )
    except urllib.error.HTTPError as exc:
        sys.exit(f"Start refused ({exc.code}): {exc.read().decode(errors='replace')}")
    print(reply.get("message", reply))


def _base_mode(base: str) -> str | None:
    """The receiver's mode now (``fixed`` once a survey has committed)."""
    try:
        return str(_request(f"{base}/api/device/base-config").get("mode"))
    except (OSError, ValueError):
        return None


def _fmt(value: float | None, unit: str = "", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def print_summary(summary: Summary, base_mode: str | None = None) -> None:
    """The run's figures, as a Markdown row-ready block for #197."""
    print()
    print("## Run summary")
    print(f"- Outcome: {summary.outcome} {summary.abort_reason or ''}".rstrip())
    print(f"- Run time: {_fmt(summary.run_time_s, ' s', 0)}")
    print(f"- Time to first Fixed: {_fmt(summary.time_to_first_fixed_s, ' s', 0)}")
    print(f"- Observation (Fixed) time: {summary.observation_time_s} s")
    print(f"- Final survey accuracy: {_fmt(summary.final_accuracy_mm, ' mm')}")
    print(f"- Typical Fixed 3D accuracy: {_fmt(summary.typical_accuracy_3d_mm, ' mm')}")
    print(
        f"- Spread of Fixed positions, first 5 min: {_fmt(summary.spread_5min_mm, ' mm')}"
    )
    print(
        f"- Spread of Fixed positions, first 30 min: {_fmt(summary.spread_30min_mm, ' mm')}"
    )
    if summary.latitude is not None:
        print(
            f"- Committed position: {summary.latitude:.9f}, "
            f"{summary.longitude:.9f}, {_fmt(summary.altitude_m, ' m', 4)}"
        )
    print(
        f"- Receiver reads during injection: {summary.acks}, "
        f"{summary.ack_failures} failed, {summary.ack_slow} slow (over "
        f"{SLOW_READ_MS:.0f} ms, likely retried); "
        f"latency p50 {_fmt(summary.ack_p50_ms, ' ms', 0)}, "
        f"p95 {_fmt(summary.ack_p95_ms, ' ms', 0)}, max {_fmt(summary.ack_max_ms, ' ms', 0)}"
    )
    if base_mode is not None:
        print(
            f"- Receiver mode afterwards: {base_mode} (fixed = back on air as a base)"
        )


# The survey progress fields the CSV keeps. The RTK status comes from the
# survey too, but is stored with the position it was paired with.
_PROGRESS_FIELDS = [
    "outcome",
    "abort_reason",
    "valid",
    "correction_age_s",
    "source_connected",
    "source_last_error",
    "duration_seconds",
    "observations",
    "mean_accuracy_mm",
    "seconds_without_fixed",
]
_CSV_FIELDS = [
    "t",
    "rtk_status",
    "position_ok",
    "latitude",
    "longitude",
    "altitude_m",
    "h_acc_m",
    "v_acc_m",
    *_PROGRESS_FIELDS,
    "survey_latitude",
    "survey_longitude",
    "survey_altitude_m",
    "ack_ok",
    "ack_latency_ms",
]


def run(args: argparse.Namespace) -> Summary:
    """Poll until the survey ends (or Ctrl-C); write the CSV; summarise."""
    base = args.url.rstrip("/")
    if args.start:
        _start(base, args.start, args.min_duration, args.accuracy_mm)
    samples: list[Sample] = []
    acks: list[Ack] = []
    started = time.monotonic()
    next_ack = 0.0
    with open(args.csv, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        try:
            while True:
                t = time.monotonic() - started
                row: dict[str, Any] = {"t": round(t, 1)}
                try:
                    progress = _request(f"{base}/api/device/survey-in")
                    position = _request(f"{base}/api/device/position")
                except (OSError, ValueError) as exc:  # URLError is an OSError
                    print(f"[{t:6.0f}s] poll failed: {exc}")
                    time.sleep(args.interval)
                    continue
                sample = Sample(
                    t=t,
                    # The survey's own reading counts; the live display's
                    # is the fallback (e.g. before the survey has sampled).
                    rtk_status=str(
                        progress.get("rtk_status") or position.get("rtk_status", "none")
                    ),
                    latitude=float(position.get("latitude") or 0.0),
                    longitude=float(position.get("longitude") or 0.0),
                    altitude_m=float(position.get("altitude_m") or 0.0),
                    horizontal_accuracy_m=float(
                        position.get("horizontal_accuracy_m", 0.0)
                    ),
                    vertical_accuracy_m=float(position.get("vertical_accuracy_m", 0.0)),
                    progress=progress,
                    # A read that got no reply comes back as 0, 0 with no fix.
                    position_ok=position.get("fix_type") == "3d"
                    and bool(position.get("latitude") or position.get("longitude")),
                )
                samples.append(sample)
                row.update(
                    rtk_status=sample.rtk_status,
                    position_ok=sample.position_ok,
                    latitude=sample.latitude,
                    longitude=sample.longitude,
                    altitude_m=sample.altitude_m,
                    h_acc_m=sample.horizontal_accuracy_m,
                    v_acc_m=sample.vertical_accuracy_m,
                    **{k: progress.get(k) for k in _PROGRESS_FIELDS},
                    survey_latitude=progress.get("latitude"),
                    survey_longitude=progress.get("longitude"),
                    survey_altitude_m=progress.get("altitude_m"),
                )
                if args.ack_every and t >= next_ack:
                    next_ack = t + args.ack_every
                    began = time.monotonic()
                    try:
                        _request(f"{base}/api/device/base-config")
                        ok = True
                    except (OSError, ValueError):
                        ok = False
                    latency = (time.monotonic() - began) * 1000.0
                    acks.append(Ack(t=t, ok=ok, latency_ms=latency))
                    row.update(ack_ok=ok, ack_latency_ms=round(latency))
                writer.writerow(row)
                handle.flush()
                print(
                    f"[{t:6.0f}s] {progress.get('outcome')}  RTK {sample.rtk_status:5}  "
                    f"age {_fmt(progress.get('correction_age_s'), ' s')}  "
                    f"source {'up' if progress.get('source_connected') else 'down'}  "
                    f"Fixed {progress.get('duration_seconds')} s  "
                    f"acc {_fmt(progress.get('mean_accuracy_mm'), ' mm')}"
                )
                if progress.get("outcome") not in (None, "running"):
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped logging (the survey keeps running).")
        finally:
            handle.flush()
    summary = summarise(samples, acks)
    print_summary(summary, _base_mode(base) if summary.outcome else None)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8080", help="the station")
    parser.add_argument("--start", metavar="SOURCE", help="start a Corrected survey-in")
    parser.add_argument("--min-duration", type=int, default=300, help="Fixed time (s)")
    parser.add_argument("--accuracy-mm", type=int, default=50, help="accuracy limit")
    parser.add_argument("--csv", default="corrected-survey.csv", help="samples file")
    parser.add_argument("--interval", type=float, default=1.0, help="poll period (s)")
    parser.add_argument(
        "--ack-every",
        type=float,
        default=10.0,
        help="receiver read period (s); 0 = off",
    )
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
