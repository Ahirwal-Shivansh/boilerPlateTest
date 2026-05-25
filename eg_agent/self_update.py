"""
Re-export of :mod:`eg_build_self_update` for ``eg-agent-build`` and other
package entrypoints. Implementation lives in ``eg_build_self_update`` so
repository ``build.py`` can self-update without importing ``eg_agent``.
"""

from eg_build_self_update import ensure_latest_installed

__all__ = ["ensure_latest_installed"]
