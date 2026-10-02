#!/usr/bin/env python3
"""Probe how corrections reach the receiver, in about two minutes (#197).

Starts a Corrected survey-in against a saved Correction source, watches its
progress for ``--seconds`` (90 by default), cancels it, and prints what the
link did, then a verdict: GREEN, or RED with the reasons. Exit code 0 for
green, 1 for red, 2 if the survey couldn't start.

It's red when any of these holds:

- Frames were dropped before they were written (the pump fell behind);
- written Frames' data was older than MAX_FRAME_AGE_S (stale corrections;
  measured by the station's clock, so it needs NTP);
- the survey sampled less often than every MAX_SAMPLE_INTERVAL_S;
- the receiver counted fewer RTCM 3 messages than were written;
- the receiver used fewer than MIN_USED_RATIO of the MSM it reported on.

It starts with a minimum duration the probe won't reach, so it never
commits a fixed base; the receiver is left in rover mode, as after any
cancel.

Usage:
    uv run python tools/probe_correction_delivery.py --url http://test-base:8080 \\
        --source earthscope
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, cast

MAX_FRAME_AGE_S = 5.0
MAX_SAMPLE_INTERVAL_S = 1.5
MIN_RECEIVED_RATIO = 0.9
MIN_USED_RATIO = 0.9


def _obj(value: Any) -> dict[str, Any]:
    """``value`` as a JSON object (an empty one when it's missing)."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def verdict(progress: dict[str, Any]) -> list[str]:
    """Why the link is red; an empty list when it's green."""
    reasons: list[str] = []
    written = progress.get("corrections_written") or 0
    dropped = progress.get("corrections_dropped") or 0
    failed = progress.get("correction_write_failures") or 0
    received = progress.get("receiver_rtcm3_messages") or 0
    diagnostics = _obj(progress.get("diagnostics"))
    if written == 0:
        reasons.append("no correction Frames were written")
    if dropped:
        share = dropped / max(1, written + dropped)
        reasons.append(f"{dropped} Frames dropped ({share:.0%}): the pump fell behind")
    if failed:
        reasons.append(f"{failed} Frames lost to failed writes")
    age = _obj(diagnostics.get("frame_age_s")).get("p95")
    if age is not None and age > MAX_FRAME_AGE_S:
        reasons.append(f"Frames were {age:.1f} s old when written (p95)")
    interval = _obj(diagnostics.get("sample_interval_s")).get("p95")
    if interval is not None and interval > MAX_SAMPLE_INTERVAL_S:
        reasons.append(f"the survey sampled every {interval:.1f} s (p95), not 1 s")
    # The receiver's count is read every 30 s: compare it with what had been
    # written by then, not with the live count.
    compared = diagnostics.get("written_at_receiver_read") or 0
    if compared and received < MIN_RECEIVED_RATIO * compared:
        reasons.append(f"the receiver counted {received} RTCM 3 of {compared} written")
    # The receiver's own verdicts (a sample): "unknown" counts as neither.
    rtcm = _obj(_obj(diagnostics.get("driver")).get("rtcm"))
    msm = [_obj(use) for kind, use in rtcm.items() if 1071 <= int(kind) <= 1137]
    used = sum(use.get("used", 0) for use in msm)
    judged = used + sum(use.get("not_used", 0) for use in msm)
    if judged and used < MIN_USED_RATIO * judged:
        reasons.append(f"the receiver used {used} of {judged} MSM it judged")
    reference = [_obj(rtcm.get(kind)) for kind in ("1005", "1006") if kind in rtcm]
    if msm and reference and not sum(use.get("used", 0) for use in reference):
        reasons.append("the receiver never used the base position (1005/1006)")
    return reasons


def _request(
    url: str, body: dict[str, Any] | None = None, timeout: float = 30.0
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


def _spread(spread: dict[str, Any] | None, unit: str, digits: int = 1) -> str:
    if not spread:
        return "—"
    return (
        f"p50 {spread['p50']:.{digits}f}{unit}, p95 {spread['p95']:.{digits}f}{unit}, "
        f"max {spread['max']:.{digits}f}{unit} (n={spread['count']})"
    )


def report(progress: dict[str, Any], seconds: float) -> None:
    """What the link did, one measurement per line."""
    d = _obj(progress.get("diagnostics"))
    driver = _obj(d.get("driver"))
    written = progress.get("corrections_written") or 0
    dropped = progress.get("corrections_dropped") or 0
    print(f"## Correction delivery over {seconds:.0f} s")
    print(
        f"- Frames written {written}, dropped {dropped}, failed "
        f"{progress.get('correction_write_failures')}; delivered "
        f"{written / max(1, written + dropped):.0%}"
    )
    print(
        f"- Receiver: {progress.get('receiver_rtcm3_messages')} RTCM 3 parsed, "
        f"{progress.get('receiver_skipped_bytes')} B skipped, "
        f"{progress.get('receiver_overrun_errors')} overruns; buffers tx/rx pending "
        f"{d.get('receiver_tx_pending')}/{d.get('receiver_rx_pending')} B, peak "
        f"{d.get('receiver_tx_peak_usage')}/{d.get('receiver_rx_peak_usage')} %"
    )
    print(
        f"- RTK {progress.get('rtk_status')}, correction age bucket "
        f"{driver.get('correction_age_bucket')} (u-blox lastCorrectionAge)"
    )
    print(f"- Frame age when written: {_spread(d.get('frame_age_s'), ' s')}")
    print(f"- Frames per write: {_spread(d.get('batch_frames'), '', 0)}")
    print(f"- Survey sample interval: {_spread(d.get('sample_interval_s'), ' s', 2)}")
    print(f"- Survey position read: {_spread(d.get('position_read_s'), ' s', 2)}")
    for op, timing in sorted(_obj(driver.get("lock")).items()):
        print(
            f"- Lock {op}: wait {_spread(timing.get('wait_ms'), ' ms', 0)}; "
            f"hold {_spread(timing.get('hold_ms'), ' ms', 0)}"
        )
    for name, spread in sorted(_obj(driver.get("poll_ms")).items()):
        print(f"- Poll {name}: {_spread(spread, ' ms', 0)}")
    print(
        f"- Write: {_spread(driver.get('write_ms'), ' ms', 1)}; "
        f"host tx backlog after: {_spread(driver.get('tx_backlog_bytes'), ' B', 0)}"
    )
    for kind, use in sorted(
        _obj(driver.get("rtcm")).items(), key=lambda kv: int(kv[0])
    ):
        print(
            f"- RTCM {kind}: received {use['received']}, used {use['used']}, "
            f"not used {use['not_used']}, CRC failed {use['crc_failed']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8080", help="the station")
    parser.add_argument("--source", required=True, help="saved Correction source")
    parser.add_argument("--seconds", type=float, default=90.0, help="how long to watch")
    parser.add_argument("--json", help="also save the final progress to this file")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    try:
        _request(
            f"{base}/api/device/configure/corrected-survey-in",
            {
                "correction_source": args.source,
                "min_duration_seconds": 86400,  # never completes during the probe
                "accuracy_limit_mm": 1000,
            },
        )
    except urllib.error.HTTPError as exc:
        print(f"Start refused ({exc.code}): {exc.read().decode(errors='replace')}")
        return 2
    started = time.monotonic()
    progress: dict[str, Any] = {}
    try:
        while time.monotonic() - started < args.seconds:
            time.sleep(5.0)
            try:
                progress = _request(f"{base}/api/device/survey-in")
            except (OSError, ValueError) as exc:
                print(f"poll failed: {exc}")
                continue
            if progress.get("outcome") not in (None, "running"):
                break
    finally:
        if progress.get("outcome") in (None, "running"):
            try:
                _request(f"{base}/api/device/cancel-survey-in", {})
            except (OSError, ValueError) as exc:
                print(f"Cancel failed: {exc}; cancel it from the Survey page")
        else:
            print(f"The survey had already ended: {progress.get('outcome')}")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(progress, handle, indent=2)
    report(progress, time.monotonic() - started)
    reasons = verdict(progress)
    if reasons:
        print("\nRED:\n" + "\n".join(f"- {reason}" for reason in reasons))
        return 1
    print("\nGREEN: every Frame written fresh, and the survey sampling at 1 Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
