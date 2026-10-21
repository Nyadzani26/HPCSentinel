#!/bin/bash
# Schedules only the two I/O faults of run_faults.sh, for when they have to be repeated.
#
# Usage: tools/rerun_io_faults.sh HH:MM     (today, cluster time; both faults start at that time and last 15 minutes)
#
# compute12 is an idle node and compute40 a loaded one. Each fault logs its real start and end to the same
# experiments file as run_faults.sh, and logs nothing if the writer could not start.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T0="${1:?give the start time as HH:MM, for example 18:45}"
LOG="$HOME/HPCSentinel/experiments-$(date +%Y%m%d).csv"
DUR=900
MEM_MB=$(scontrol show node compute01 | grep -o 'RealMemory=[0-9]*' | cut -d= -f2)
SMALL=$((MEM_MB * 5 / 100))

[ -f "$LOG" ] || echo "node,anomaly_type,start,end,intensity" > "$LOG"
for node in compute12 compute40; do
  sbatch --parsable -p batch -J "fault-IO-$node" -N 1 -w "$node" --mem="$SMALL" --time=25 --begin="$T0" \
    --wrap "$HERE/fault_run.sh IO $DUR $LOG" >/dev/null
  echo "$T0  IO on $node"
done
echo "Experiment log: $LOG"
