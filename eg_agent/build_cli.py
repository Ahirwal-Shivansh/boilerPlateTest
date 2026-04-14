"""
Console-facing build entry for EG-Agent installers.

This module is installed as the `eg-agent-build` console script so that
*any* project which has `eg-agent` installed can run:

    eg-agent-build windows
    eg-agent-build macos

from its own project root and get:

- `dist/eg-agent` + `dist/eg-agent-installer-helper`
- `build/windows/EG-Agent.msi` (on Windows with WiX installed)
- `build/macos/EG-Agent.pkg` (on macOS with pkgbuild installed)
"""

from __future__ import annotations

import argparse
import importlib
from typing import List, Optional

from eg_agent.self_update import ensure_latest_installed


def main(argv: Optional[List[str]] = None) -> int:
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
        module = importlib.import_module("eg_agent.build_windows_installer")
        module.main()
    elif args.target == "macos":
        module = importlib.import_module("eg_agent.build_macos_installer")
        module.main()

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
