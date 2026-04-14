"""
Unified build entrypoint for EG-Agent installers.

Usage:

    # Build Windows MSI (on Windows)
    python build.py windows

    # Build macOS PKG (on macOS)
    python build.py macos

This script is intentionally thin; all heavy lifting is delegated to:
- `build_windows_installer.py`
- `build_macos_installer.py`
"""

from __future__ import annotations

import argparse
import importlib

from eg_build_self_update import ensure_latest_installed


def main(argv: list[str] | None = None) -> int:
    ensure_latest_installed()

    parser = argparse.ArgumentParser(
        description="Build EG-Agent installers (Windows MSI / macOS PKG)."
    )
    parser.add_argument(
        "target",
        choices=["windows", "macos"],
        help="Target platform to build for.",
    )

    args = parser.parse_args(argv)

    if args.target == "windows":
        module = importlib.import_module("build_windows_installer")
        module.main()
    elif args.target == "macos":
        module = importlib.import_module("build_macos_installer")
        module.main()
    else:  # pragma: no cover - guarded by argparse choices
        raise SystemExit(f"Unknown target: {args.target}")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
