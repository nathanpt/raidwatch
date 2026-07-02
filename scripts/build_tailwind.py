#!/usr/bin/env python3
"""Build the vendored Tailwind CSS from source (authoring-time only; D29).

Downloads the standalone Tailwind CLI if not present, then compiles
``static/src/tailwind.css`` → ``static/vendor/tailwind.css --minify``.

This script runs ONLY when the design changes (adding new utility classes), not
at deploy time. The compiled ``static/vendor/tailwind.css`` is committed.

Usage:
    python scripts/build_tailwind.py
"""

from __future__ import annotations

import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_VERSION = "3.4.17"
CLI_BASENAME = {
    ("Linux", "x86_64"): "tailwindcss-linux-x64",
    ("Linux", "aarch64"): "tailwindcss-linux-arm64",
    ("Darwin", "x86_64"): "tailwindcss-macos-x64",
    ("Darwin", "arm64"): "tailwindcss-macos-arm64",
    ("Windows", "AMD64"): "tailwindcss-windows-x64.exe",
}
DOWNLOAD_URL = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{CLI_VERSION}"


def get_cli() -> Path:
    """Return the path to the Tailwind standalone CLI, downloading if needed."""
    machine = platform.machine()
    system = platform.system()
    arch = (
        "aarch64"
        if machine in ("arm64", "aarch64")
        else "x86_64"
        if machine in ("x86_64", "AMD64")
        else machine
    )

    basename = CLI_BASENAME.get((system, arch))
    if basename is None:
        print(f"Unsupported platform: {system}/{arch}. Download manually from Tailwind releases.")
        sys.exit(1)

    cli_path = REPO_ROOT / ".cache" / basename
    if not cli_path.exists():
        cli_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{DOWNLOAD_URL}/{basename}"
        print(f"Downloading Tailwind CLI v{CLI_VERSION} → {cli_path}")
        urllib.request.urlretrieve(url, cli_path)
        cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)
        print("Downloaded.")

    return cli_path


def main() -> int:
    cli = get_cli()
    src = REPO_ROOT / "static" / "src" / "tailwind.css"
    out = REPO_ROOT / "static" / "vendor" / "tailwind.css"

    print(f"Compiling {src.relative_to(REPO_ROOT)} → {out.relative_to(REPO_ROOT)} (minified)")
    result = subprocess.run(
        [str(cli), "-i", str(src), "-o", str(out), "--minify"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Tailwind CLI failed:\n{result.stderr}")
        return 1
    print(result.stdout.strip())
    print(f"Done: {out.stat().st_size // 1024}KB → {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
