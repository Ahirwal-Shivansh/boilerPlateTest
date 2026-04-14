from pathlib import Path
from platformdirs import user_data_dir
import os

APP_NAME = "eg-agent"
APP_AUTHOR = "Eventgraphia"


def get_app_path(filename: str) -> Path:
    """Get the path to a file in the application's data directory.

    Args:
        filename: The name of the file

    Returns:
        Path object pointing to the file in the app's data directory
    """
    # Primary location follows platformdirs, but it may be root-owned on Macs
    # where the PKG installer ran before any user session existed.
    data_dir = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # If the directory is not writable by the current user, fall back.
        if os.access(data_dir, os.W_OK):
            return data_dir / filename
    except Exception:
        # We'll fall back below.
        pass

    # Writable fallback for logs/db even if the primary Application Support
    # directory is root-owned.
    fallback_dir = Path.home() / "Library" / "Logs" / APP_NAME
    try:
        fallback_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Last resort: per-process writable temp-ish location in home.
        fallback_dir = Path.home() / ".eg-agent"
        fallback_dir.mkdir(parents=True, exist_ok=True)

    return fallback_dir / filename
