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
  python3 loadgen.py mem  --seconds 900 --mem-frac 0.8
  python3 loadgen.py io   --seconds 900 --path /var/tmp
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
    cores = os.cpu_count() or 1
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


def run_io(seconds, path):
    deadline = time.time() + seconds
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["job", "mem", "io", "net-recv", "net-send"])
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--mem-frac", type=float, default=None)
    ap.add_argument("--path", default="/var/tmp")
    ap.add_argument("--target")
    ap.add_argument("--port", type=int, default=5201)
    a = ap.parse_args()
    print(f"{socket.gethostname()} {a.mode} for {a.seconds}s", flush=True)
    if a.mode == "job":
        run_job(a.seconds, a.mem_frac if a.mem_frac is not None else 0.2)
    elif a.mode == "mem":
        run_mem(a.seconds, a.mem_frac if a.mem_frac is not None else 0.8)
    elif a.mode == "io":
        run_io(a.seconds, a.path)
    elif a.mode == "net-recv":
        run_net_recv(a.seconds, a.port)
    elif a.mode == "net-send":
        if not a.target:
            raise SystemExit("--target is required for net-send")
        run_net_send(a.seconds, a.target, a.port)
    print(f"{socket.gethostname()} done", flush=True)


if __name__ == "__main__":
    main()
