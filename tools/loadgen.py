#!/usr/bin/env python3
"""Load generator for validating HPCSentinel on a cluster that has no stress tools installed.

Standard library only, so it runs on any node with python3. It produces two kinds of behaviour:

  job       healthy workload: CPU on every core (with a little per-node variation) plus moderate memory.
  mem       fault: holds and keeps touching a large share of the memory, with little CPU.
  io        fault: heavy disk writes with fsync.
  net-recv  fault (receiving end): accepts a connection and discards what it receives.
  net-send  fault (sending end): streams data to the receiving node.

Examples:
  python3 loadgen.py job  --seconds 5400
  python3 loadgen.py mem  --seconds 900 --mem-frac 0.6
  python3 loadgen.py io   --seconds 900 --path ~/.loadgen --duty 0.2
  python3 loadgen.py net-recv --seconds 900 --port 5201
  python3 loadgen.py net-send --seconds 900 --target compute02 --port 5201

It only ever uses the node it runs on (and one peer for the network mode), and stops by itself.
"""
import argparse
import multiprocessing as mp
import os
import random
import socket
import time
import zlib


def mem_total_bytes():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 8 << 30


def mem_available_bytes():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return mem_total_bytes() // 2


def spin(deadline, duty):
    """Busy loop for `duty` of every 100 ms slot until the deadline."""
    x = 1.0001
    while time.time() < deadline:
        slot_end = time.time() + 0.1 * duty
        while time.time() < slot_end:
            for _ in range(2000):
                x = x * 1.0000001 % 7.0
        rest = 0.1 * (1 - duty)
        if rest > 0:
            time.sleep(rest)


def hold_memory(deadline, size, touch_every=2.0):
    block = bytearray(size)
    page = 4096
    while time.time() < deadline:
        stamp = int(time.time()) & 0xFF
        for i in range(0, size, page):
            block[i] = stamp
        time.sleep(touch_every)
    del block


def run_job(seconds, mem_frac):
    deadline = time.time() + seconds
    rng = random.Random(zlib.crc32(socket.gethostname().encode()))
    duty = rng.uniform(0.75, 1.0)
    cores = len(os.sched_getaffinity(0))
    procs = [mp.Process(target=spin, args=(deadline, duty)) for _ in range(cores)]
    size = int(mem_total_bytes() * mem_frac * rng.uniform(0.8, 1.2))
    procs.append(mp.Process(target=hold_memory, args=(deadline, size)))
    for p in procs:
        p.start()
    for p in procs:
        p.join()


def run_mem(seconds, mem_frac):
    deadline = time.time() + seconds
    size = int(min(mem_available_bytes() * mem_frac, mem_total_bytes() * mem_frac))
    hold_memory(deadline, size, touch_every=0.5)


def ensure_writable_dir(path):
    """Make sure the folder exists and can be written, or stop with a clear message.

    On a cluster whose root file system is read-only, a write to /var/tmp used to kill the writer without a word
    and the job carried on as if nothing was wrong.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".probe-{os.getpid()}")
        with open(probe, "wb") as f:
            f.write(b"x")
        os.remove(probe)
    except OSError as e:
        raise SystemExit(f"cannot write to {path}: {e}")


def run_io(seconds, path, duty=1.0):
    deadline = time.time() + seconds
    if duty < 1.0:                      # write in bursts, resting between them, instead of continuously
        io_duty_loop(deadline, duty, path)
        return
    name = os.path.join(path, f"loadgen-{os.getpid()}.bin")
    chunk = os.urandom(1 << 20)
    try:
        while time.time() < deadline:
            with open(name, "wb") as f:
                for _ in range(1024):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            with open(name, "rb") as f:
                while f.read(1 << 20):
                    pass
    finally:
        if os.path.exists(name):
            os.remove(name)


def run_net_recv(seconds, port):
    deadline = time.time() + seconds
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", port))
    server.listen(1)
    server.settimeout(max(1, seconds))
    conn, _ = server.accept()
    conn.settimeout(5)
    while time.time() < deadline:
        try:
            if not conn.recv(1 << 20):
                break
        except socket.timeout:
            continue
    conn.close()
    server.close()


def run_net_send(seconds, target, port):
    deadline = time.time() + seconds
    payload = b"\0" * (1 << 20)
    for _ in range(60):
        try:
            sock = socket.create_connection((target, port), timeout=5)
            break
        except OSError:
            time.sleep(2)
    else:
        raise SystemExit(f"could not connect to {target}:{port}")
    while time.time() < deadline:
        sock.sendall(payload)
    sock.close()


# ---- normal workload profiles for baseline collection -------------------------------------------
# Real jobs differ in what they stress. Each profile is a plausible NORMAL job: one resource is the main
# one and the others are used lightly. They are never faults; they exist so the platform can learn
# several kinds of normal.
PROFILES = {
    #            cores  cpu duty  memory share  io duty  net duty
    "cpu":      (1.00, 0.90, 0.10, 0.00, 0.00),
    "memory":   (0.50, 0.35, 0.40, 0.00, 0.00),
    "io":       (0.50, 0.40, 0.10, 0.60, 0.00),   # processor use must stay above the idle threshold, or its nodes read as idle
    "comm":     (0.50, 0.40, 0.10, 0.00, 0.60),
    "mixed":    (0.75, 0.60, 0.20, 0.20, 0.00),
}


def io_duty_loop(deadline, duty, path):
    name = os.path.join(path, f"loadgen-{os.getpid()}.bin")
    chunk = os.urandom(1 << 20)
    try:
        while time.time() < deadline:
            t0 = time.time()
            with open(name, "wb") as f:
                for _ in range(256):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            busy = time.time() - t0
            rest = busy * (1 - duty) / max(duty, 0.01)
            time.sleep(min(rest, max(0.0, deadline - time.time())))
    finally:
        if os.path.exists(name):
            os.remove(name)


def net_duty_loop(deadline, duty, peer, port):
    """Exchange traffic with a peer in both directions: listen for it and send to it."""
    import threading

    def listen():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", port))
        srv.listen(1)
        srv.settimeout(max(1.0, deadline - time.time() + 30))
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.settimeout(5)
        while time.time() < deadline:
            try:
                if not conn.recv(1 << 20):
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        conn.close()
        srv.close()

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    payload = b"\0" * (1 << 20)
    sock = None
    for _ in range(90):
        try:
            sock = socket.create_connection((peer, port), timeout=5)
            break
        except OSError:
            time.sleep(2)
    if sock is None:
        return
    try:
        while time.time() < deadline:
            t0 = time.time()
            for _ in range(64):
                sock.sendall(payload)
            busy = time.time() - t0
            time.sleep(min(busy * (1 - duty) / max(duty, 0.01), max(0.0, deadline - time.time())))
    except OSError:
        pass  # the peer finished a few seconds earlier than this node
    finally:
        sock.close()
    t.join(timeout=10)


def run_workload(seconds, profile, peer, port, path, log):
    cores_share, cpu_duty, mem_share, io_duty, net_duty = PROFILES[profile]
    rng = random.Random(zlib.crc32(f"{socket.gethostname()}{time.time():.0f}".encode()))
    cpu_duty = min(1.0, cpu_duty * rng.uniform(0.85, 1.1))
    start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    deadline = time.time() + seconds
    cores = max(1, int(len(os.sched_getaffinity(0)) * cores_share))
    procs = [mp.Process(target=spin, args=(deadline, cpu_duty)) for _ in range(cores)]
    if mem_share:
        procs.append(mp.Process(target=hold_memory, args=(deadline, int(mem_total_bytes() * mem_share * rng.uniform(0.85, 1.1)), 1.0)))
    if io_duty:
        ensure_writable_dir(path)
        procs.append(mp.Process(target=io_duty_loop, args=(deadline, io_duty, path)))
    if net_duty:
        if not peer:
            raise SystemExit("the comm profile needs --peer")
        procs.append(mp.Process(target=net_duty_loop, args=(deadline, net_duty, peer, port)))
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    if log:
        import fcntl
        with open(log, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(f"{socket.gethostname().split('.')[0]},{profile},{start},{end}\n")
            fcntl.flock(f, fcntl.LOCK_UN)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["job", "mem", "io", "net-recv", "net-send", "workload"])
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--mem-frac", type=float, default=None)
    ap.add_argument("--path", default="/tmp")
    ap.add_argument("--duty", type=float, default=1.0, help="io mode: share of the time spent writing (1 = continuously)")
    ap.add_argument("--target")
    ap.add_argument("--port", type=int, default=5201)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="cpu")
    ap.add_argument("--peer")
    ap.add_argument("--log")
    a = ap.parse_args()
    print(f"{socket.gethostname()} {a.mode} for {a.seconds}s", flush=True)
    if a.mode == "workload":
        run_workload(a.seconds, a.profile, a.peer, a.port, a.path, a.log)
    elif a.mode == "job":
        run_job(a.seconds, a.mem_frac if a.mem_frac is not None else 0.15)
    elif a.mode == "mem":
        run_mem(a.seconds, a.mem_frac if a.mem_frac is not None else 0.5)
    elif a.mode == "io":
        ensure_writable_dir(a.path)
        run_io(a.seconds, a.path, a.duty)
    elif a.mode == "net-recv":
        run_net_recv(a.seconds, a.port)
    elif a.mode == "net-send":
        if not a.target:
            raise SystemExit("--target is required for net-send")
        run_net_send(a.seconds, a.target, a.port)
    print(f"{socket.gethostname()} done", flush=True)


if __name__ == "__main__":
    main()
