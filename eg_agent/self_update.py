"""
Helpers for keeping the installed `eg-agent` package up to date.

This is primarily used by build entrypoints so that running a build command
in a virtualenv automatically upgrades the `eg-agent` distribution and then
re-executes the process to run on the updated code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution, version
from typing import Optional


_DEFAULT_DIST_NAME = "eg-agent"
_REEXEC_GUARD_ENV = "EG_AGENT_SELF_UPDATE_DONE"
_ENABLED_ENV = "EG_AGENT_SELF_UPDATE"
_PIP_SOURCE_URL_ENV = "EG_AGENT_PIP_SOURCE_URL"


def _in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _get_installed_version(dist_name: str) -> Optional[str]:
    try:
        return version(dist_name)
    except PackageNotFoundError:
        return None


def _get_vcs_install_url(dist_name: str) -> Optional[str]:
    """
    If this dist was installed from a VCS URL (PEP 610), return a pip-installable
    spec such as: git+ssh://git@github.com/org/repo.git

    Returns None for non-VCS installs (e.g. from PyPI).
    """
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return None

    direct_url_text = None
    try:
        direct_url_text = dist.read_text("direct_url.json")
    except Exception:
        direct_url_text = None

    if not direct_url_text:
        return None

    try:
        data = json.loads(direct_url_text)
    except Exception:
        return None

    vcs_info = data.get("vcs_info") or {}
    if vcs_info.get("vcs") != "git":
        return None

    url = (data.get("url") or "").strip()
    if not url:
        return None

    # Normalize to a pip VCS URL. PEP 610 urls are often "ssh://..." or "https://..."
    return url if url.startswith("git+") else f"git+{url}"


def ensure_latest_installed(
    *,
    dist_name: Optional[str] = None,
) -> None:
    """
    Ensure the installed `eg-agent` distribution is upgraded in the current
    environment, then re-exec the process if it changed.

    This intentionally runs *before* importing other build modules so an
    updated package version is actually used for the build.

    Environment variables:
    - EG_AGENT_SELF_UPDATE: if "false", skip self-update (default: "true")
    - EG_AGENT_PIP_DIST: override the dist name (default: "eg-agent")
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

    source_url = os.environ.get(_PIP_SOURCE_URL_ENV)
    if source_url and source_url.strip():
        effective_spec = source_url.strip()
    else:
        effective_spec = _get_vcs_install_url(effective_dist) or effective_dist

    allow_global = (
        os.environ.get("EG_AGENT_ALLOW_GLOBAL_PIP", "false").lower() == "true"
    )
    if not _in_virtualenv() and not allow_global:
        raise RuntimeError(
            "Refusing to self-update outside a virtualenv. Activate your venv "
            "or set EG_AGENT_ALLOW_GLOBAL_PIP=true to override."
        )

    before = _get_installed_version(effective_dist)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        effective_spec,
    ]
    subprocess.run(cmd, check=True)

    after = _get_installed_version(effective_dist)
    if before is None:
        # Fresh install; re-exec to ensure imports resolve from site-packages.
        should_reexec = True
    else:
        should_reexec = before != after

    if not should_reexec:
        return

    os.environ[_REEXEC_GUARD_ENV] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
