#!/usr/bin/env bash
# Research only: reproduce the X20P correction-telemetry evidence offline.
# Never opens hardware, serial ports or the network. Writes only JSON and
# run.log in this directory.
#
# Usage, from the repo root:
#   bash docs/research/assets/x20p-correction-telemetry/run_all.sh \
#     [MANUAL_DIR] [LOCKED_PY] [CANDIDATE_PY]
#
# MANUAL_DIR holds full Firecrawl extractions 2.00.md 2.02.md 2.10.md 2.11.md
# of the official X20 HPG interface descriptions; they are not committed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
S=/home/ubuntu/.hermes/cache/scratch
MANUAL_DIR="${1:-$S/x20p-manual-extracts}"
LOCKED_PY="${2:-$S/x20p-parser-locked/bin/python}"
CANDIDATE_PY="${3:-$S/x20p-parser-candidate/bin/python}"
LOG="$HERE/run.log"
: > "$LOG"
run() { echo "\$ $*" | tee -a "$LOG"; "$@" 2>&1 | tee -a "$LOG"; }
run python3 "$HERE/manual_layouts.py" "$MANUAL_DIR"
(cd "$HERE" && run "$LOCKED_PY" offline_parse.py locked)
(cd "$HERE" && run "$CANDIDATE_PY" offline_parse.py candidate)
run python3 "$HERE/contract_model.py" "$REPO"
run python3 "$HERE/aggregate.py"
echo "all steps exited 0" | tee -a "$LOG"
