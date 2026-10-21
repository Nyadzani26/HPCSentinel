#!/bin/bash
# Runs ONE injected fault on this node and appends its real start and end (UTC) to the experiment log.
# Called by run_faults.sh through Slurm; can also be run by hand.
#
#   fault_run.sh MEMORY  <seconds> <log.csv>
#   fault_run.sh IO      <seconds> <log.csv>
#   fault_run.sh NETWORK <seconds> <log.csv> <peer-node>     (this node sends to the peer)
#
# The log has the columns node, anomaly_type, start, end, intensity, which is exactly what the
# platform's experiment upload expects. A network fault is logged for both ends.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TYPE="$1"; SECS="$2"; LOG="$3"; PEER="${4:-}"
ME="$(hostname -s)"
now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() {  # node type start end intensity
  ( flock 9; echo "$1,$2,$3,$4,$5" >> "$LOG" ) 9>>"$LOG.lock"
}

case "$TYPE" in
  MEMORY)
    START="$(now)"
    if python3 "$HERE/loadgen.py" mem --seconds "$SECS" --mem-frac 0.5; then
      END="$(now)"
      log "$ME" MEMORY "$START" "$END" "50 percent of memory held and touched"
    else
      echo "MEMORY fault failed on $ME; nothing logged" >&2
    fi
    ;;
  IO)
    # The root file system of the compute nodes is read-only, so the writes go to the shared home folder: heavy traffic to the
    # head node, which is what an I/O problem is on a cluster built this way. 20% duty keeps the head node's link usable.
    START="$(now)"
    if python3 "$HERE/loadgen.py" io --seconds "$SECS" --path "$HOME/.loadgen" --duty 0.2; then
      END="$(now)"
      log "$ME" IO "$START" "$END" "256 MiB writes with fsync to the shared home folder, 20% of the time"
    else
      echo "IO fault failed on $ME; nothing logged" >&2
    fi
    ;;
  NETWORK)
    [ -n "$PEER" ] || { echo "network fault needs a peer node" >&2; exit 2; }
    srun -N1 -n1 -w "$PEER" python3 "$HERE/loadgen.py" net-recv --seconds "$((SECS + 20))" --port 5201 &
    RECV=$!
    sleep 5
    START="$(now)"
    if python3 "$HERE/loadgen.py" net-send --seconds "$SECS" --target "$PEER" --port 5201; then
      END="$(now)"
      kill "$RECV" 2>/dev/null; wait "$RECV" 2>/dev/null
      log "$ME" NETWORK "$START" "$END" "bulk TCP stream to $PEER"
      log "$PEER" NETWORK "$START" "$END" "bulk TCP stream from $ME"
    else
      kill "$RECV" 2>/dev/null; wait "$RECV" 2>/dev/null
      echo "NETWORK fault failed on $ME; nothing logged" >&2
    fi
    ;;
  *) echo "unknown fault type $TYPE" >&2; exit 2 ;;
esac
