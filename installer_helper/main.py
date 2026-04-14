"""
Simple installer helper for MSI/PKG installers.

This module is compiled into a small binary using PyInstaller and invoked by:
- Windows MSI custom actions
- macOS PKG postinstall scripts

It provides a simple hooks-based system. Developers create `installer_hooks.py`
in their project with functions like:

    def pre_install(install_root, data_root, is_upgrade):
        '''Called before installation.'''
        pass

    def post_install(install_root, data_root, is_upgrade):
        '''Called after installation.'''
        pass

The helper will call these functions if they exist.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def _log(msg: str) -> None:
    """Simple logging for installer output."""
    print(f"[installer-helper] {msg}")


def _run_cmd(args: list[str], check: bool = True) -> int:
    """Run a subprocess command."""
    _log(f"Running: {' '.join(args)}")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.stdout:
        _log(result.stdout.strip())
    if result.stderr:
        _log(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {args}")
    return result.returncode


def _detect_paths() -> tuple[str, str]:
    """Detect default install_root and data_root paths."""
    # install_root: where the binary lives
    exe_path = Path(sys.argv[0]).resolve()
    install_root = str(exe_path.parent)

    # data_root: platform-specific app data directory
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        data_root = os.path.join(base, "EG-Agent")
    elif sys.platform == "darwin":
        base = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support"
        )
        data_root = os.path.join(base, "EG-Agent")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
        data_root = os.path.join(base, "eg-agent")

    return install_root, data_root


def _load_hooks() -> Dict[str, Callable]:
    """
    Try to load installer_hooks module and return available hook functions.

    Hooks are loaded from:
    1. installer_hooks module (if bundled by PyInstaller)
    2. installer_hooks.py next to the helper executable
    """
    hooks: Dict[str, Callable] = {}

    # Try importing bundled module first
    try:
        import installer_hooks
        _log("Loaded installer_hooks module")

        for name in ["pre_install", "post_install", "on_start", "on_stop"]:
            if hasattr(installer_hooks, name):
                func = getattr(installer_hooks, name)
                if callable(func):
                    hooks[name] = func
                    _log(f"  Found hook: {name}")

        return hooks
    except ImportError:
        pass

    # Try loading from file next to executable
    exe_dir = Path(sys.argv[0]).resolve().parent
    hooks_file = exe_dir / "installer_hooks.py"

    if hooks_file.exists():
        _log(f"Loading hooks from: {hooks_file}")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "installer_hooks", hooks_file
        )
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            for name in ["pre_install", "post_install", "on_start", "on_stop"]:
                if hasattr(module, name):
                    func = getattr(module, name)
                    if callable(func):
                        hooks[name] = func
                        _log(f"  Found hook: {name}")

    return hooks


def _call_hook(
    hooks: Dict[str, Callable],
    name: str,
    **kwargs: Any
) -> None:
    """Call a hook function if it exists."""
    if name in hooks:
        _log(f"Calling hook: {name}")
        try:
            hooks[name](**kwargs)
            _log(f"Hook {name} completed successfully")
        except Exception as e:
            _log(f"Hook {name} failed: {e}")
            raise


def stop_agent(agent_cli: str) -> None:
    """Stop the running agent if it exists."""
    _log("Stopping agent...")
    try:
        # Try to stop gracefully - don't fail if agent isn't running
        _run_cmd([agent_cli, "--stop"], check=False)
    except FileNotFoundError:
        _log("Agent not found (first install)")
    except Exception as e:
        _log(f"Could not stop agent: {e}")


def run_migrations(agent_cli: str) -> None:
    """Run database migrations."""
    _log("Running database migrations...")
    _run_cmd([agent_cli, "migrate"], check=True)


def start_agent(agent_cli: str) -> None:
    """Start the agent."""
    _log("Starting agent...")
    _run_cmd([agent_cli, "serve"], check=True)


def ensure_data_dir(data_root: str) -> None:
    """Ensure the data directory exists."""
    os.makedirs(data_root, exist_ok=True)
    _log(f"Data directory ready: {data_root}")


def run_install(
    agent_cli: str,
    install_root: str,
    data_root: str,
    is_upgrade: bool,
) -> None:
    """
    Run the complete installation flow.

    1. Stop existing agent (if upgrading)
    2. Call pre_install hook
    3. Ensure data directory exists
    4. Run database migrations
    5. Call post_install hook
    6. Start agent
    """
    hooks = _load_hooks()

    # 1. Stop existing agent
    if is_upgrade:
        stop_agent(agent_cli)

    # 2. Pre-install hook
    _call_hook(hooks, "pre_install",
               install_root=install_root,
               data_root=data_root,
               is_upgrade=is_upgrade)

    # 3. Ensure data directory
    ensure_data_dir(data_root)

    # 4. Database migrations
    run_migrations(agent_cli)

    # 5. Post-install hook
    _call_hook(hooks, "post_install",
               install_root=install_root,
               data_root=data_root,
               is_upgrade=is_upgrade)

    # 6. Start agent
    start_agent(agent_cli)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for the installer helper."""
    parser = argparse.ArgumentParser(
        description="EG-Agent installer helper"
    )
    parser.add_argument(
        "--agent-cli",
        default="eg-agent",
        help="Path to the agent CLI executable"
    )
    parser.add_argument(
        "--full-install",
        action="store_true",
        help="Run full installation flow"
    )
    parser.add_argument(
        "--install-root",
        help="Override install root directory"
    )
    parser.add_argument(
        "--data-root",
        help="Override data root directory"
    )
    parser.add_argument(
        "--is-upgrade",
        action="store_true",
        help="This is an upgrade, not a fresh install"
    )

    args = parser.parse_args(argv)

    # Detect paths
    default_install_root, default_data_root = _detect_paths()
    install_root = args.install_root or default_install_root
    data_root = args.data_root or default_data_root

    try:
        if args.full_install:
            run_install(
                agent_cli=args.agent_cli,
                install_root=install_root,
                data_root=data_root,
                is_upgrade=args.is_upgrade,
            )
        else:
            parser.error("No operation specified. Use --full-install.")
    except Exception as e:
        _log(f"INSTALLATION FAILED: {e}")
        return 1

    _log("Installation completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
