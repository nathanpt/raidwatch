#!/usr/bin/env python3
"""Simulate load to exercise gates (D10).

Spawns CPU- and memory-intensive workloads that push metrics past gate
thresholds, so you can verify gates trigger and recover without needing a real
raid. Run alongside the RaidWatch dashboard and watch the gauges + banners.

Usage:
    python scripts/stress_test_sim.py              # 60s mixed load
    python scripts/stress_test_sim.py --cpu 300     # 5min CPU burn
    python scripts/stress_test_sim.py --ram 4       # allocate 4GB RAM
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
import time
from pathlib import Path


def cpu_burn(duration: int) -> None:
    """Burn CPU for ``duration`` seconds."""
    end = time.time() + duration
    while time.time() < end:
        # Busy-wait: pure CPU spin
        _ = sum(i * i for i in range(10000))


def ram_alloc(gb: float, duration: int) -> None:
    """Allocate and hold ``gb`` GB of RAM for ``duration`` seconds."""
    print(f"  Allocating {gb}GB RAM...")
    chunks = []
    chunk_size = 100 * 1024 * 1024  # 100MB chunks
    total_bytes = int(gb * 1024 * 1024 * 1024)
    allocated = 0
    while allocated < total_bytes:
        chunk = bytearray(chunk_size)
        # Touch every page to ensure real allocation
        for i in range(0, len(chunk), 4096):
            chunk[i] = 1
        chunks.append(chunk)
        allocated += chunk_size
    print(f"  Holding {gb}GB for {duration}s...")
    time.sleep(duration)
    del chunks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stress-test load simulator for gate testing (D10)"
    )
    parser.add_argument(
        "--cpu", type=int, default=0, metavar="SECONDS", help="CPU burn duration (seconds)"
    )
    parser.add_argument("--ram", type=float, default=0, metavar="GB", help="RAM to allocate (GB)")
    parser.add_argument(
        "--duration", type=int, default=60, metavar="SECONDS", help="Total duration (default 60)"
    )
    parser.add_argument(
        "--cores", type=int, default=0, metavar="N", help="CPU cores to burn (0=all)"
    )
    args = parser.parse_args()

    duration = args.duration
    n_cores = args.cores or multiprocessing.cpu_count()

    print(f"\n{'=' * 60}")
    print("  RaidWatch Stress Test Simulator (D10)")
    print(f"  Duration: {duration}s | CPU: {n_cores} cores | RAM: {args.ram}GB")
    print(f"{'=' * 60}\n")

    procs: list[multiprocessing.Process] = []

    # CPU load
    if args.cpu or duration > 0:
        cpu_dur = args.cpu if args.cpu else duration
        for _ in range(n_cores):
            p = multiprocessing.Process(target=cpu_burn, args=(cpu_dur,))
            p.start()
            procs.append(p)
        print(f"  Started {n_cores} CPU burn processes ({cpu_dur}s)")

    # RAM load
    if args.ram > 0:
        ram_dur = duration if not args.cpu else min(duration, args.cpu)
        p = multiprocessing.Process(target=ram_alloc, args=(args.ram, ram_dur))
        p.start()
        procs.append(p)

    if not procs:
        print("  No load selected. Use --cpu or --ram.")
        return 1

    print("\n  Watch the RaidWatch dashboard for gate triggers.\n")

    try:
        for p in procs:
            p.join(timeout=duration + 10)
    except KeyboardInterrupt:
        print("\n  Stopping...")
        for p in procs:
            p.terminate()

    print("  Done.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
