"""Smoke test for M2: supervisor, /health (D35), and SSE stream (D25/D28).

Starts the server, verifies health + SSE, then shuts down.
"""

import json
import subprocess
import sys
import time

import httpx


def main() -> int:
    # Start the server
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "raidwatch.main:app", "--port", "8091"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # Wait for server to be ready
        time.sleep(5)

        # Test /health
        r = httpx.get("http://localhost:8091/health", timeout=10)
        health = r.json()
        print("=== /health (D35) ===")
        print(f"  status: {health['status']}")
        print(f"  version: {health['version']}")
        print(f"  collector.age: {health['collector']['last_tick_age_seconds']:.1f}s")
        print(f"  collector.cycle_ms: {health['collector']['last_cycle_ms']:.0f}")
        print(f"  collector.failures: {health['collector']['consecutive_failures']}")
        print(f"  sse_subscribers: {health['sse_subscribers']}")
        print(f"  db_size_mb: {health['db_size_mb']:.3f}")
        assert health["status"] in ("operational", "degraded")

        # Login
        r = httpx.post(
            "http://localhost:8091/login",
            data={"token": "testtoken123456789012345678901234567890"},
            follow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 303
        cookie = r.headers["set-cookie"].split(";")[0]
        print(f"\n=== Login ===\n  status: {r.status_code}, cookie set")

        # Test SSE stream — read 2 events
        print("\n=== SSE stream (D25/D28) ===")
        with httpx.stream(
            "GET",
            "http://localhost:8091/api/stream",
            headers={"Cookie": cookie},
            timeout=15,
        ) as resp:
            events = 0
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    events += 1
                    cpu = data.get("system", {}).get("cpu_total_percent", "?")
                    print(f"  event {events}: ts={data['ts']}, CPU={cpu}%")
                    if events >= 2:
                        break
            print(f"  Received {events} SSE events ✓")

        # Final health
        r = httpx.get("http://localhost:8091/health", timeout=10)
        health = r.json()
        print(
            f"\n=== Final /health ===\n  status: {health['status']}, subs: {health['sse_subscribers']}"
        )

        print("\n✅ M2 smoke test passed!")
        return 0

    except Exception as e:
        print(f"❌ M2 smoke test FAILED: {e}")
        import traceback

        traceback.print_exc()
        # Print server output for debugging
        print("\n=== Server output ===")
        out = proc.stdout.read1(8192) if proc.stdout else b""
        print(out.decode(errors="replace"))
        return 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
