import os
import threading
import pystray
from PIL import Image, ImageDraw
import sys
from eg_agent.log_config import logger as base_logger
from eg_agent.server import refresh_websocket_connections


logger = base_logger.getChild("tray")


def _resolve_tray_icon_path():
    """
    Resolve the tray icon file path from EG_AGENT_TRAY_ICON env var.
    When running as a frozen PyInstaller binary, relative paths are
    resolved against the bundle root (sys._MEIPASS).
    """
    path = os.environ.get("EG_AGENT_TRAY_ICON")
    if not path or not (path := path.strip()):
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        if base:
            full = os.path.join(base, path)
            if os.path.isfile(full):
                return full
    # Development: relative to cwd
    full = os.path.normpath(os.path.join(os.getcwd(), path))
    return full if os.path.isfile(full) else None


def _create_icon():
    # Prefer icon from file if EG_AGENT_TRAY_ICON is set in .env
    icon_path = _resolve_tray_icon_path()
    if icon_path:
        try:
            img = Image.open(icon_path)
            if img.mode != "RGB":
                img = img.convert("RGB")
            return img
        except Exception as e:
            logger.warning(
                "Failed to load tray icon from %s: %s", icon_path, e)
    # Fallback: simple tray icon image
    img = Image.new("RGB", (64, 64), color="black")
    draw = ImageDraw.Draw(img)
    draw.rectangle((16, 16, 48, 48), fill="white")
    return img


def on_quit(icon, item):
    logger.info("Quit selected from tray. Stopping icon and exiting.")
    icon.stop()
    sys.exit(0)


def on_refresh_ws(icon, item):
    # Trigger server-side refresh of websocket connections
    ok = refresh_websocket_connections()
    if ok:
        logger.info("Triggered WebSocket refresh via tray")
    else:
        logger.warning("WebSocket refresh requested,"
                       +
                       " but server loop unavailable")


def start_tray_icon(blocking: bool = False):
    """
    Start the tray icon.

    On macOS, AppKit UI objects must be created on the main thread.
    Set blocking=True to run the tray on the main thread.
    """
    def _run():
        logger.info("Starting tray icon")
        icon = pystray.Icon(
            "eg-agent",
            _create_icon(),
            title="EG Agent",
            menu=pystray.Menu(
                pystray.MenuItem("Refresh WebSocket", on_refresh_ws),
                pystray.MenuItem("Quit", on_quit)
            )
        )
        icon.run()

    if blocking:
        # Run on the current (main) thread
        _run()
    else:
        # Run in background thread (allowed on non-mac platforms)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logger.info("Tray icon thread started")
