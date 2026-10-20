#!/bin/bash
# Schedules the validation faults and a background healthy load, all through Slurm.
#
# Usage: tools/run_faults.sh HH:MM        (today, local time of the cluster; the first fault starts 10 minutes later)
#
# Background: compute25-48 run a normal job for the whole evaluation window, so faults are tested
# on both idle nodes (compute01-24) and loaded nodes (compute25-48). Faults last 15 minutes.
# Each fault is a separate Slurm job that logs its real start and end to the CSV printed at the end.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T0="${1:?give the window start as HH:MM, for example 17:30}"
LOG="$HOME/HPCSentinel/experiments-$(date +%Y%m%d).csv"
DUR=900   # seconds per fault

[ -f "$LOG" ] || echo "node,anomaly_type,start,end,intensity" > "$LOG"

at() {  # offset-minutes -> a Slurm --begin value (HH:MM) computed from T0
  date -d "@$(( $(date -d "$T0" +%s) + $1 * 60 ))" +%H:%M
}
bg() {  # background healthy job on a block of nodes
  sbatch --parsable -p batch -J "bg-$1" -N 12 --ntasks-per-node=1 -w "$2" --time=170 --begin="$T0" \
    --wrap "srun python3 $HERE/loadgen.py job --seconds 9600" >/dev/null
}
single() {  # offset type node
  sbatch --parsable -p batch -J "fault-$2-$3" -N 1 -w "$3" --oversubscribe --time=25 --begin="$(at "$1")" \
    --wrap "$HERE/fault_run.sh $2 $DUR $LOG" >/dev/null
  echo "$(at "$1")  $2 on $3"
}
pair() {  # offset sender peer
  sbatch --parsable -p batch -J "fault-NETWORK-$2" -N 2 -w "$2,$3" --oversubscribe --time=25 --begin="$(at "$1")" \
    --wrap "$HERE/fault_run.sh NETWORK $DUR $LOG $3" >/dev/null
  echo "$(at "$1")  NETWORK $2 -> $3"
}

bg C "compute[25-36]"
bg D "compute[37-48]"
echo "background load on compute25-48 from $T0"

single 10  MEMORY  compute03     # idle node
single 10  MEMORY  compute30     # loaded node
pair   40  compute07 compute08   # idle pair
pair   40  compute33 compute34   # loaded pair
single 70  IO      compute12     # idle node
single 70  IO      compute40     # loaded node
single 100 MEMORY  compute18     # idle node
single 100 MEMORY  compute45     # loaded node

echo
echo "All faults scheduled. Experiment log: $LOG"
echo "Last fault ends about $(at 120). Check with: squeue -u $USER"
