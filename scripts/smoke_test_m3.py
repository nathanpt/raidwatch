"""Smoke test for M3: full frontend rendering with live SSE data.

Starts the server, fetches the dashboard HTML, verifies all key elements are
present, and confirms the SSE stream + REST pre-fill work.
"""

import json
import subprocess
import sys
import time

import httpx


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "raidwatch.main:app", "--port", "8092"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(5)

        # Login
        r = httpx.post(
            "http://localhost:8092/login",
            data={"token": "testtoken123456789012345678901234567890"},
            follow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 303
        cookie = r.headers["set-cookie"].split(";")[0]
        headers = {"Cookie": cookie}

        # Dashboard HTML
        r = httpx.get("http://localhost:8092/", headers=headers, timeout=10)
        html = r.text
        print("=== Dashboard HTML checks ===")
        checks = [
            ("RaidWatch title", "RaidWatch" in html),
            ("CPU card", "card-cpu" in html),
            ("RAM card", "card-ram" in html),
            ("Temp card", "card-temp" in html),
            ("Storage card", "card-storage-free" in html),
            ("CPU gauge", "gauge-cpu" in html),
            ("RAM gauge", "gauge-ram" in html),
            ("CPU chart canvas", "chart-cpu" in html),
            ("RAM chart canvas", "chart-ram" in html),
            ("Disk chart", "chart-disk" in html),
            ("Network chart", "chart-net" in html),
            ("Process table", "process-table-body" in html),
            ("Events feed", "events-feed" in html),
            ("Status pill", "status-pill" in html),
            ("Help modal", "help-modal" in html),
            ("Keyboard hint", "Keyboard Shortcuts" in html),
            ("Range selector", "range-select" in html),
            ("app.js included", "app.js" in html),
            ("Tailwind CSS", "tailwind.css" in html),
            ("Chart.js", "chart.umd.min.js" in html),
        ]
        all_ok = True
        for name, ok in checks:
            print(f"  {'✅' if ok else '❌'} {name}")
            if not ok:
                all_ok = False

        # REST pre-fill
        print("\n=== REST pre-fill (history) ===")
        r = httpx.get(
            "http://localhost:8092/api/metrics/history?minutes=60", headers=headers, timeout=10
        )
        hist = r.json()
        print(f"  rows: {len(hist.get('data', []))}")
        assert hist["ok"]

        # SSE stream
        print("\n=== SSE stream ===")
        events = 0
        with httpx.stream(
            "GET", "http://localhost:8092/api/stream", headers=headers, timeout=15
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    events += 1
                    sys_m = data.get("system", {})
                    print(
                        f"  event {events}: CPU={sys_m.get('cpu_total_percent', '?')}%  "
                        f"RAM={sys_m.get('ram_percent', '?')}%  "
                        f"procs={len(data.get('process', {}).get('top', []))}"
                    )
                    if events >= 2:
                        break
        assert events >= 2, "Expected at least 2 SSE events"

        # Static assets exist
        print("\n=== Static assets ===")
        for path in [
            "/static/vendor/tailwind.css",
            "/static/vendor/chart.umd.min.js",
            "/static/app.js",
        ]:
            r = httpx.get(f"http://localhost:8092{path}", timeout=10)
            print(
                f"  {'✅' if r.status_code == 200 else '❌'} {path} ({r.status_code}, {len(r.content) // 1024}KB)"
            )
            if r.status_code != 200:
                all_ok = False

        if all_ok:
            print("\n✅ M3 frontend smoke test passed!")
            return 0
        else:
            print("\n❌ Some checks failed")
            return 1

    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        import traceback

        traceback.print_exc()
        out = proc.stdout.read1(8192) if proc.stdout else b""
        print(out.decode(errors="replace"))
        return 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
