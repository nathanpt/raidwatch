"""Directly exercise WHEA collection to capture the real exception.

The collector catches and logs "WHEA query failed" (D8 isolation), so the
traceback doesn't surface in the snapshot. This script calls the function
directly so any exception prints to the console for diagnosis.

Run with the venv python:

    .\\.venv\\Scripts\\python.exe scripts\\probe_whea.py
"""

from __future__ import annotations

import sys
import traceback

from raidwatch.modules.system import (
    _WIN32_AVAILABLE,
    _query_whea_events,
    gather_whea,
)


def main() -> int:
    print(f"platform           : {sys.platform}")
    print(f"_WIN32_AVAILABLE   : {_WIN32_AVAILABLE}")
    if not _WIN32_AVAILABLE:
        print("pywin32 not importable -- run scripts/install_win_deps.ps1")
        return 1

    print("\n-- calling _query_whea_events(2.0) directly --")
    try:
        events = _query_whea_events(2.0)
        print(f"OK -- {len(events)} WHEA event(s) in the last 2h")
        for e in events[:5]:
            print("  ", e)
    except Exception:
        print("FAILED -- traceback:")
        traceback.print_exc()

    print("\n-- calling gather_whea() (the collector's wrapper) --")
    try:
        result = gather_whea(2.0)
        print("OK --", result)
    except Exception:
        print("FAILED -- traceback:")
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
