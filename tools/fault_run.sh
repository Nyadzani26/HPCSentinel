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
    python3 "$HERE/loadgen.py" mem --seconds "$SECS" --mem-frac 0.5
    END="$(now)"
    log "$ME" MEMORY "$START" "$END" "50 percent of memory held and touched"
    ;;
  IO)
    START="$(now)"
    python3 "$HERE/loadgen.py" io --seconds "$SECS" --path /var/tmp
    END="$(now)"
    log "$ME" IO "$START" "$END" "continuous 1 GiB writes with fsync"
    ;;
  NETWORK)
    [ -n "$PEER" ] || { echo "network fault needs a peer node" >&2; exit 2; }
    srun -N1 -n1 -w "$PEER" python3 "$HERE/loadgen.py" net-recv --seconds "$((SECS + 20))" --port 5201 &
    RECV=$!
    sleep 5
    START="$(now)"
    python3 "$HERE/loadgen.py" net-send --seconds "$SECS" --target "$PEER" --port 5201
    END="$(now)"
    kill "$RECV" 2>/dev/null; wait "$RECV" 2>/dev/null
    log "$ME" NETWORK "$START" "$END" "bulk TCP stream to $PEER"
    log "$PEER" NETWORK "$START" "$END" "bulk TCP stream from $ME"
    ;;
  *) echo "unknown fault type $TYPE" >&2; exit 2 ;;
esac
