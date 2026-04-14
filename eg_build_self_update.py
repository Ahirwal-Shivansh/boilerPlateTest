"""
Self-update helper for the repository-level `build.py`.

This module intentionally does *not* import `eg_agent` so it can run even
before project dependencies are installed in the current environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Optional


_DEFAULT_DIST_NAME = "eg-agent"
_REEXEC_GUARD_ENV = "EG_AGENT_SELF_UPDATE_DONE"
_ENABLED_ENV = "EG_AGENT_SELF_UPDATE"


def _in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _get_installed_version(dist_name: str) -> Optional[str]:
    try:
        return version(dist_name)
    except PackageNotFoundError:
        return None


def ensure_latest_installed(
    *,
    dist_name: Optional[str] = None,
) -> None:
    """
    Upgrade the dist in the active venv, then re-exec if it changed.

    Env vars:
    - EG_AGENT_SELF_UPDATE: if "false", skip self-update (default: "true")
    - EG_AGENT_PIP_DIST: override dist name (default: "eg-agent")
    - EG_AGENT_ALLOW_GLOBAL_PIP: if "true", allow running outside a venv
    - EG_AGENT_SELF_UPDATE_DONE: internal guard to prevent infinite re-exec
    """
    if os.environ.get(_ENABLED_ENV, "true").lower() == "false":
        return

    if os.environ.get(_REEXEC_GUARD_ENV) == "1":
        return

    effective_dist = (
        dist_name
        or os.environ.get("EG_AGENT_PIP_DIST")
        or _DEFAULT_DIST_NAME
    )

    allow_global = (
        os.environ.get("EG_AGENT_ALLOW_GLOBAL_PIP", "false").lower() == "true"
    )
    if not _in_virtualenv() and not allow_global:
        raise RuntimeError(
            "Refusing to self-update outside a virtualenv. Activate your venv "
            "or set EG_AGENT_ALLOW_GLOBAL_PIP=true to override."
        )

    before = _get_installed_version(effective_dist)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            effective_dist,
        ],
        check=True,
    )

    after = _get_installed_version(effective_dist)
    should_reexec = before is None or before != after
    if not should_reexec:
        return

    os.environ[_REEXEC_GUARD_ENV] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
