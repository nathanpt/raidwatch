#!/usr/bin/env python3
"""Standalone cross-platform metrics dump for local smoke testing.

Runs the psutil subset of the system metrics gatherer and prints a formatted
summary. Useful for verifying that system metrics work on the current platform
without starting the full web app.

Usage:
    python scripts/collect_once.py            # one-shot dump
    python scripts/collect_once.py --watch    # repeat every 5s until Ctrl-C
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running as a script (add repo root to path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raidwatch.modules import system as system_mod


def format_bytes(b: int | float | None) -> str:
    if b is None:
        return "N/A"
    if b == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def format_rate(bps: int | None) -> str:
    if bps is None:
        return "N/A"
    return format_bytes(bps) + "/s"


def print_metrics(metrics: dict) -> None:
    sys_m = metrics
    print(f"\n{'=' * 60}")
    print(f"  RaidWatch metrics dump @ {time.strftime('%H:%M:%S')}")
    print(f"{'=' * 60}")

    # CPU
    cpu = sys_m.get("cpu_total_percent")
    cores = sys_m.get("cpu_per_core_percent", [])
    print(f"\n  CPU Total:     {cpu:.1f}%" if cpu else "  CPU Total:     N/A")
    if cores:
        per_core = "  ".join(f"{c:5.1f}" for c in cores)
        print(f"  Per-core:      {per_core}")

    # RAM
    print(
        f"\n  RAM Used:      {format_bytes(sys_m.get('ram_used_bytes'))} "
        f"({sys_m.get('ram_percent', 0):.1f}%)"
    )
    print(f"  RAM Avail:     {format_bytes(sys_m.get('ram_available_bytes'))}")
    print(
        f"  Swap Used:     {format_bytes(sys_m.get('swap_used_bytes'))} "
        f"({sys_m.get('swap_percent', 0):.1f}%)"
    )

    # Disk
    print(f"\n  Disk Read:     {format_rate(sys_m.get('disk_read_bps'))}")
    print(f"  Disk Write:    {format_rate(sys_m.get('disk_write_bps'))}")
    print(f"  Disk Queue:    {sys_m.get('disk_queue_length', 'N/A')}")
    volumes = sys_m.get("disk_volumes", [])
    if volumes:
        print(f"  Volumes ({len(volumes)}):")
        for v in volumes:
            pct = (v.free_bytes / v.total_bytes * 100) if v.total_bytes else 0
            print(
                f"    {v.mount:20s} {format_bytes(v.free_bytes)} free "
                f"of {format_bytes(v.total_bytes)} ({pct:.0f}%)"
            )

    # Network
    nics = sys_m.get("net_by_nic", {})
    if nics:
        print(f"\n  Network ({len(nics)} NICs):")
        for name, stats in list(nics.items())[:5]:
            print(f"    {name:20s} ↑{format_rate(stats.recv_bps)} ↓{format_rate(stats.sent_bps)}")

    # Windows-only metrics
    print(f"\n  Pages/sec:     {sys_m.get('pages_per_sec', 'N/A')}")
    print(f"  Disk latency:  {sys_m.get('disk_avg_sec_per_transfer', 'N/A')}")
    print(f"  WHEA (2h):     {sys_m.get('whea_count_2h', 'N/A')}")
    print("  CPU Temp:      N/A (temps module not loaded)")

    # Platform note
    if sys.platform != "win32":
        print("\n  [Linux dev mode — pywin32 metrics (queue/WHEA/pages) are None]")


def main() -> int:
    parser = argparse.ArgumentParser(description="RaidWatch standalone metrics dump")
    parser.add_argument("--watch", action="store_true", help="Repeat every 5s until Ctrl-C")
    parser.add_argument("--interval", type=float, default=5.0, help="Watch interval (seconds)")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                print_metrics(system_mod.gather())
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print_metrics(system_mod.gather())

    return 0


if __name__ == "__main__":
    sys.exit(main())
