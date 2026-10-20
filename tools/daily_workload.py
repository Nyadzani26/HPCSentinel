#!/usr/bin/env python3
"""Schedules one day of NORMAL, varied workloads on the cluster so HPCSentinel can learn what normal looks like.

Why: a detector that has only ever seen an idle cluster, or one uniform job, flags every real job as abnormal.
Real simulations stress different things (processor, memory, disk, network), so each day this script plans a
random but reproducible mix of those profiles across random nodes, with idle gaps, and submits it to Slurm.

  - It never loads more than --max-loaded of the nodes at once, so idle nodes always remain as peers.
  - It plans only inside the window in which the cluster is switched on (default: until 16:30 local time).
  - It injects NO faults. Faults are separate (tools/run_faults.sh) and are kept out of baseline days.
  - It writes the plan (baseline-plan-DATE.csv) and, as each job finishes, the actual start and end times
    (baseline-workload-DATE.csv), so every learning period can be documented and audited.

Run it once a day after the nodes have booted, for example from cron on the head node:

    25 10 * * * /home/gift/HPCSentinel/tools/daily_workload.py --until 16:30 >> /home/gift/HPCSentinel/daily_workload.log 2>&1

Use --dry-run to see the plan without submitting anything.
"""
import argparse
import datetime as dt
import os
import random
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOADGEN = os.path.join(HERE, "loadgen.py")
PROFILE_WEIGHTS = {"cpu": 30, "memory": 20, "io": 20, "comm": 15, "mixed": 15}
MEMORY_RESERVE = {"memory": 50}   # percent of node memory a job reserves; others reserve 25
CORES = 14                        # of 16: leave two free for monitoring and ad-hoc work


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def available_nodes(dry_run):
    out = run(["sinfo", "-h", "-p", "batch", "-t", "idle,mix,alloc", "-o", "%n"]) if not dry_run else ""
    nodes = sorted({n.strip() for n in out.split() if re.fullmatch(r"compute\d+", n.strip())})
    return nodes or [f"compute{i:02d}" for i in range(1, 49)]


def node_memory_mb(node, dry_run):
    out = run(["scontrol", "show", "node", node]) if not dry_run else ""
    m = re.search(r"RealMemory=(\d+)", out)
    return int(m.group(1)) if m else 128000


def hhmm(value, base):
    h, m = value.split(":")
    return base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def plan(nodes, start, until, rng, max_loaded):
    """Return [(profile, [nodes], begin, minutes)] for the window."""
    cap_total = max(2, int(len(nodes) * max_loaded))
    free_at = {n: start for n in nodes}
    jobs, cursor = [], start
    while cursor < until - dt.timedelta(minutes=30):
        loaded_now = sum(1 for n in nodes if free_at[n] > cursor)
        room = cap_total - loaded_now
        if room < 2:
            cursor += dt.timedelta(minutes=10)
            continue
        profile = rng.choices(list(PROFILE_WEIGHTS), weights=list(PROFILE_WEIGHTS.values()))[0]
        minutes = rng.choice(range(30, 95, 5))
        minutes = min(minutes, int((until - cursor).total_seconds() // 60))
        if minutes < 25:
            break
        idle = [n for n in nodes if free_at[n] <= cursor]
        rng.shuffle(idle)
        if profile == "comm":
            pairs = rng.randint(1, max(1, min(4, room // 2)))
            if len(idle) < 2 * pairs:
                cursor += dt.timedelta(minutes=10)
                continue
            for i in range(pairs):
                chosen = idle[2 * i:2 * i + 2]
                jobs.append((profile, chosen, cursor, minutes))
                for n in chosen:
                    free_at[n] = cursor + dt.timedelta(minutes=minutes + 5)
        else:
            k = rng.randint(2, max(2, min(12, room)))
            chosen = idle[:k]
            if len(chosen) < 2:
                cursor += dt.timedelta(minutes=10)
                continue
            jobs.append((profile, chosen, cursor, minutes))
            for n in chosen:
                free_at[n] = cursor + dt.timedelta(minutes=minutes + 5)
        cursor += dt.timedelta(minutes=rng.randint(5, 25))
    return jobs


def submit(job, mem_mb, log, dry_run):
    profile, nodes, begin, minutes = job
    seconds = minutes * 60
    reserve = mem_mb * MEMORY_RESERVE.get(profile, 25) // 100
    flags = ["sbatch", "--parsable", "-p", "batch", "-J", f"base-{profile}", "--cpus-per-task", str(CORES),
             "--mem", str(reserve), "--time", str(minutes + 5), "--begin", begin.strftime("%Y-%m-%dT%H:%M:%S"),
             "-w", ",".join(nodes), "-N", str(len(nodes)), "--ntasks-per-node", "1"]
    base = f"python3 {shlex.quote(LOADGEN)} workload --profile {profile} --seconds {seconds} --log {shlex.quote(log)}"
    if profile == "comm":
        a, b = nodes
        wrap = (f"srun --cpus-per-task={CORES} --mem=0 -N1 -n1 -w {a} {base} --peer {b} & "
                f"srun --cpus-per-task={CORES} --mem=0 -N1 -n1 -w {b} {base} --peer {a} & wait")
    else:
        wrap = f"srun --cpus-per-task={CORES} --mem=0 {base}"
    flags += ["--wrap", wrap]
    if dry_run:
        return "dry-run"
    return subprocess.run(flags, capture_output=True, text=True, check=False).stdout.strip() or "FAILED"


def wait_for_nodes(minimum, minutes):
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        out = run(["sinfo", "-h", "-p", "batch", "-t", "idle", "-o", "%D"])
        if sum(int(x) for x in out.split() if x.isdigit()) >= minimum:
            return
        time.sleep(30)
    print("Fewer nodes than expected are up; planning with those that are.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="HH:MM local, default: 10 minutes from now")
    ap.add_argument("--until", default="16:30", help="HH:MM local, last moment a job may end (default 16:30)")
    ap.add_argument("--seed", type=int, help="default: the date, so a day's plan can be reproduced")
    ap.add_argument("--max-loaded", type=float, default=0.6, help="largest share of nodes loaded at once (default 0.6)")
    ap.add_argument("--wait-minutes", type=int, default=20, help="wait this long for the nodes to boot (default 20)")
    ap.add_argument("--outdir", default=os.path.expanduser("~/HPCSentinel"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="plan again even if today already has a plan")
    a = ap.parse_args()

    now = dt.datetime.now()
    day = now.strftime("%Y%m%d")
    plan_file = os.path.join(a.outdir, f"baseline-plan-{day}.csv")
    log_file = os.path.join(a.outdir, f"baseline-workload-{day}.csv")
    if os.path.exists(plan_file) and not (a.force or a.dry_run):
        print(f"Today already has a plan ({plan_file}); use --force to replace it.")
        return 0
    if not a.dry_run:
        wait_for_nodes(40, a.wait_minutes)
    nodes = available_nodes(a.dry_run)
    start = hhmm(a.start, now) if a.start else now + dt.timedelta(minutes=10)
    until = hhmm(a.until, now)
    if until <= start + dt.timedelta(minutes=40):
        print("Too late in the day to plan a useful mix.")
        return 0
    rng = random.Random(a.seed if a.seed is not None else int(day))
    jobs = plan(nodes, start, until, rng, a.max_loaded)
    mem_mb = node_memory_mb(nodes[0], a.dry_run)
    if not a.dry_run:
        os.makedirs(a.outdir, exist_ok=True)
        if not os.path.exists(log_file):
            open(log_file, "w").write("")
    rows = ["profile,nodes,begin_local,minutes,slurm_job"]
    for job in jobs:
        jid = submit(job, mem_mb, log_file, a.dry_run)
        rows.append(f"{job[0]},{' '.join(job[1])},{job[2]:%Y-%m-%dT%H:%M},{job[3]},{jid}")
        print(f"{job[2]:%H:%M} {job[3]:>3} min  {job[0]:<7} {len(job[1]):>2} nodes  job {jid}")
    if not a.dry_run:
        open(plan_file, "w").write("\n".join(rows) + "\n")
    by = {}
    for p, ns, _, m in jobs:
        by[p] = by.get(p, 0) + len(ns) * m
    print(f"{len(jobs)} jobs planned on {len(nodes)} nodes; node-minutes by profile: {by}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
