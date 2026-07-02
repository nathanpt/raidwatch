#!/usr/bin/env python3
"""Dump candidate processes + cmdlines to fill headless_cmdline_pattern (D4).

Run this on the host with SPT + Fika running to discover the exact process names
and command-line arguments. Look for EscapeFromTarkov.exe instances with Fika
headless args and fill the ``processes.headless_cmdline_pattern`` in config.yaml.

Usage:
    python scripts/discover_processes.py
    python scripts/discover_processes.py --watch   # refresh every 5s
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil

# Common SPT/Fika/WATCHDOG process names to highlight.
GAME_NAMES = {"spt.server.exe", "escapefromtarkov.exe", "watchdog.exe"}


def discover() -> list[dict]:
    """Enumerate all processes, returning dicts with pid/name/cmdline."""
    results: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            cmdline = info.get("cmdline") or []
            is_game = name in GAME_NAMES
            results.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name", "?"),
                    "cmdline": " ".join(cmdline),
                    "cpu": info.get("cpu_percent", 0),
                    "rss_mb": (info.get("memory_info").rss / 1024 / 1024)
                    if info.get("memory_info")
                    else 0,
                    "is_game": is_game,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return results


def print_results(results: list[dict]) -> None:
    game_procs = [p for p in results if p["is_game"]]
    other_procs = sorted(
        [p for p in results if not p["is_game"] and p["cpu"] > 0.1],
        key=lambda x: x["cpu"],
        reverse=True,
    )[:10]

    print(f"\n{'=' * 80}")
    print(f"  Process Discovery @ {time.strftime('%H:%M:%S')}  ({len(results)} total processes)")
    print(f"{'=' * 80}")

    if game_procs:
        print("\n  🎮 Game/Fika Processes (fill these into config.yaml):")
        print(f"  {'PID':<8} {'Name':<30} {'CPU%':>6} {'RSS MB':>8}  Cmdline")
        print(f"  {'-' * 75}")
        for p in game_procs:
            print(
                f"  {p['pid']:<8} {p['name']:<30} {p['cpu']:>6.1f} {p['rss_mb']:>8.1f}  {p['cmdline'][:60]}"
            )
    else:
        print(
            "\n  ⚠ No game processes found. Make sure SPT.Server and headless clients are running."
        )

    print("\n  📊 Top CPU Consumers (non-game):")
    print(f"  {'PID':<8} {'Name':<30} {'CPU%':>6} {'RSS MB':>8}")
    print(f"  {'-' * 60}")
    for p in other_procs:
        print(f"  {p['pid']:<8} {p['name']:<30} {p['cpu']:>6.1f} {p['rss_mb']:>8.1f}")

    # Suggest config values
    spt = [p for p in game_procs if "spt" in p["name"].lower()]
    eft = [p for p in game_procs if "escapefromtarkov" in p["name"].lower()]
    print("\n  💡 Config suggestions (config.yaml → processes:):")
    if spt:
        print(f'    spt_server_process_name: "{spt[0]["name"]}"')
    if eft:
        print(f'    headless_process_name: "{eft[0]["name"]}"')
        # Look for cmdline patterns unique to headless instances
        for p in eft:
            if "--" in p["cmdline"]:
                args = [a for a in p["cmdline"].split() if a.startswith("--")]
                if args:
                    print(f"    # PID {p['pid']} args: {' '.join(args)}")
                    print("    # → candidate headless_cmdline_pattern (pick the Fika-specific arg)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover SPT/Fika processes for config (D4)")
    parser.add_argument("--watch", action="store_true", help="Refresh every 5s")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                print_results(discover())
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print_results(discover())

    return 0


if __name__ == "__main__":
    sys.exit(main())
