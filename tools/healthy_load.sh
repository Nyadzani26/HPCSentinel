#!/bin/bash
# Builds healthy training data on a cluster that is otherwise idle.
#
# Half of the nodes run an ordinary CPU-and-memory job for 90 minutes while the other half stay
# idle, then the other half does the same. At every moment about 24 nodes are loaded and 24 idle,
# so each node learns both states and always has plenty of peers in the same state.
#
# Usage: tools/healthy_load.sh [minutes_per_phase]    (default 90)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN="${1:-90}"
SEC=$((MIN * 60))
GAP=$((MIN + 15))
JOB="python3 $HERE/loadgen.py job --seconds $SEC"
# Slurm confines a job to the cores it asks for, so ask for 14 of the 16 (the rest stay free for fault jobs)
# and reserve a quarter of the node memory (the job itself uses at most about 18 percent).
MEM_MB=$(scontrol show node compute01 | grep -o 'RealMemory=[0-9]*' | cut -d= -f2)
RES=$((MEM_MB * 25 / 100))

submit() {  # name nodelist begin
  sbatch --parsable -p batch -J "$1" -N 12 --ntasks-per-node=1 --cpus-per-task=14 --mem="$RES" -w "$2" --time=$((MIN + 10)) \
         ${3:+--begin="$3"} --wrap "srun --cpus-per-task=14 --mem=0 $JOB" >/dev/null
  echo "submitted $1 on $2 ${3:+starting $3}"
}

submit healthy-A "compute[01-12]"
submit healthy-B "compute[13-24]"
submit healthy-C "compute[25-36]" "now+${GAP}minutes"
submit healthy-D "compute[37-48]" "now+${GAP}minutes"
date
echo "Phase 1 (A,B) runs now for ${MIN} minutes; phase 2 (C,D) starts in ${GAP} minutes. Check with: squeue -u $USER"
